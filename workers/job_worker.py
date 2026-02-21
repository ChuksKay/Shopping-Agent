"""
Background job worker.

- worker_loop()       polls SQLite for 'pending' jobs every 3 s.
- process_job()       runs a single Walmart cart build and fires a callback.
- register_callback() lets the Telegram bot register a coroutine on completion.

Callback signature:
    async def cb(chat_id, cart_url, status, error, result) -> None

result dict (always present, even on failure):
    {
        "cart_url":   str | None,
        "added":      list[str],   # labels of items added
        "failed":     list[str],   # labels of items that failed all retries
        "screenshot": str | None,  # local path to failure screenshot
    }
"""

import asyncio
import json
import logging
import os
import re

import anthropic

from db.database import get_pending_jobs, get_chat, get_items, update_job
from agent.walmart import WalmartAgent, BotChallengeError

logger = logging.getLogger(__name__)

_callbacks: dict[str, object] = {}

_EMPTY_RESULT: dict = {"cart_url": None, "added": [], "failed": [], "screenshot": None}


_ai_client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))


async def _optimize_search_queries(items: list[dict]) -> list[dict]:
    """
    Use Claude Haiku to convert stored item names into optimal Walmart.ca Canada
    search queries. Called once per cart build before the item loop.

    For example:
        "indomie"          → "Indomie instant noodles"
        "eggs"             → "eggs large"
        "bread"            → "bread"
        "3 packs indomie"  → "Indomie instant noodles"  (qty already extracted)
    """
    if not items:
        return items

    entries = []
    for item in items:
        label = f"{item['brand']} {item['name']}".strip() if item.get("brand") else item["name"]
        entries.append(label)

    prompt = (
        "You are helping build a Walmart.ca Canada grocery cart. "
        "Convert these item names into the best possible Walmart.ca search queries.\n"
        "Return ONLY a JSON array of search query strings in the same order. No explanation.\n\n"
        "Items: " + json.dumps(entries) + "\n\n"
        "Rules:\n"
        "- Use standard grocery product names Walmart Canada would carry\n"
        "- Include brand if provided (e.g. 'Indomie instant noodles')\n"
        "- Keep queries concise: 2-5 words max\n"
        "- Strip any leftover quantity words (e.g. '3 packs of', 'a dozen')\n"
        "- Examples: 'indomie' → 'Indomie instant noodles', "
        "'eggs' → 'large eggs', 'bread' → 'bread', "
        "'rice' → 'long grain white rice', 'chicken' → 'chicken breast'\n"
        "- If already a good query, keep as-is\n"
        "Output:"
    )

    try:
        resp = await _ai_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        text = re.sub(r"^```[a-z]*\n?", "", text).rstrip("`").strip()
        queries = json.loads(text)

        result = []
        for i, item in enumerate(items):
            if i < len(queries) and isinstance(queries[i], str) and queries[i].strip():
                optimized_query = queries[i].strip()
                new_item = dict(item)
                # Store optimized query in name; clear brand so it isn't double-prepended
                new_item["name"] = optimized_query
                new_item["brand"] = None
                result.append(new_item)
            else:
                result.append(item)

        logger.info("Search queries optimized", extra={"count": len(result),
                    "queries": [r["name"] for r in result]})
        return result

    except Exception as exc:
        logger.warning("Search query optimization failed, using originals",
                       extra={"error": str(exc)})
        return items


def register_callback(job_id: str, cb) -> None:
    _callbacks[job_id] = cb


async def process_job(job: dict) -> None:
    job_id = job["job_id"]
    chat_id = job["chat_id"]

    await update_job(job_id, "running")
    logger.info("Job started", extra={"job_id": job_id, "chat_id": chat_id})

    chat = await get_chat(chat_id)
    postal_code = chat["postal_code"] if chat else ""

    item_rows = await get_items(chat_id)
    if not item_rows:
        await update_job(job_id, "failed", error="No items in list")
        await _fire(job_id, chat_id, None, "failed", "No items in list", _EMPTY_RESULT)
        return

    items = [
        {
            "name":      row["text"],
            "qty":       row["qty"],
            "max_price": row.get("max_price"),
            "brand":     row.get("brand"),
        }
        for row in item_rows
    ]

    # Try up to 2 attempts:
    #   Attempt 1 — normal headless run
    #   Attempt 2 — after silent headful session refresh (if attempt 1 was blocked)
    for attempt in range(1, 3):
        items_run = await _optimize_search_queries(items)

        async with WalmartAgent(postal_code=postal_code) as agent:
            try:
                result = await agent.build_cart(items_run, job_id=job_id)

                await update_job(job_id, "done", result_url=result["cart_url"])
                logger.info(
                    "Job done",
                    extra={
                        "job_id":   job_id,
                        "attempt":  attempt,
                        "added":    len(result["added"]),
                        "failed":   len(result["failed"]),
                        "cart_url": result["cart_url"],
                    },
                )
                await _fire(job_id, chat_id, result["cart_url"], "done", result=result)
                return  # success — stop here

            except BotChallengeError as exc:
                screenshot = agent.last_screenshot
                logger.warning(
                    "Bot challenge on attempt %d/%d",
                    attempt, 2,
                    extra={"job_id": job_id, "error": str(exc)},
                )

                if attempt == 1:
                    # First block — silently open headful Firefox to get fresh
                    # Akamai cookies, then loop and retry headlessly.
                    logger.info(
                        "Auto-refreshing session with headful Firefox...",
                        extra={"job_id": job_id},
                    )
                    from agent.walmart import refresh_session_headful
                    refreshed = await refresh_session_headful()
                    if refreshed:
                        logger.info(
                            "Session refreshed — retrying cart build",
                            extra={"job_id": job_id},
                        )
                        continue  # go to attempt 2

                # Attempt 2 also blocked, or headful refresh itself failed
                msg = "Walmart needs verification"
                await update_job(job_id, "needs_user", error=msg, screenshot=screenshot)
                result_data = {**_EMPTY_RESULT, "screenshot": screenshot}
                await _fire(job_id, chat_id, None, "needs_user", msg, result_data)
                return

            except Exception as exc:
                screenshot = agent.last_screenshot
                msg = str(exc)
                logger.error(
                    "Job failed", extra={"job_id": job_id, "error": msg}, exc_info=True
                )
                await update_job(job_id, "failed", error=msg, screenshot=screenshot)
                result_data = {**_EMPTY_RESULT, "screenshot": screenshot}
                await _fire(job_id, chat_id, None, "failed", msg, result_data)
                return


async def _fire(
    job_id: str,
    chat_id: int,
    cart_url: str | None,
    status: str,
    error: str | None = None,
    result: dict | None = None,
) -> None:
    cb = _callbacks.pop(job_id, None)
    if cb:
        try:
            await cb(chat_id, cart_url, status, error, result or _EMPTY_RESULT)
        except Exception as exc:
            logger.error("Callback error", extra={"job_id": job_id, "error": str(exc)})


_running: set[str] = set()


def schedule_job(job: dict) -> None:
    """Start a job task, guarded by _running to prevent duplicate execution."""
    jid = job["job_id"]
    if jid in _running:
        return
    _running.add(jid)

    async def _run() -> None:
        try:
            await process_job(job)
        finally:
            _running.discard(jid)

    asyncio.create_task(_run())


async def worker_loop() -> None:
    logger.info("Job worker started")
    while True:
        try:
            for job in await get_pending_jobs():
                schedule_job(job)
        except Exception as exc:
            logger.error("worker_loop error", extra={"error": str(exc)}, exc_info=True)

        await asyncio.sleep(3)
