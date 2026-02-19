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
import logging

from db.database import get_pending_jobs, get_chat, get_items, update_job
from agent.walmart import WalmartAgent, BotChallengeError

logger = logging.getLogger(__name__)

_callbacks: dict[str, object] = {}

_EMPTY_RESULT: dict = {"cart_url": None, "added": [], "failed": [], "screenshot": None}


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

    async with WalmartAgent(postal_code=postal_code) as agent:
        try:
            result = await agent.build_cart(items, job_id=job_id)

            await update_job(job_id, "done", result_url=result["cart_url"])
            logger.info(
                "Job done",
                extra={
                    "job_id":  job_id,
                    "added":   len(result["added"]),
                    "failed":  len(result["failed"]),
                    "cart_url": result["cart_url"],
                },
            )
            await _fire(job_id, chat_id, result["cart_url"], "done", result=result)

        except BotChallengeError as exc:
            screenshot = agent.last_screenshot
            msg = "Walmart needs verification"
            logger.warning("Bot challenge", extra={"job_id": job_id, "error": str(exc)})
            await update_job(job_id, "needs_user", error=msg, screenshot=screenshot)
            result = {**_EMPTY_RESULT, "screenshot": screenshot}
            await _fire(job_id, chat_id, None, "needs_user", msg, result)

        except Exception as exc:
            screenshot = agent.last_screenshot
            msg = str(exc)
            logger.error("Job failed", extra={"job_id": job_id, "error": msg}, exc_info=True)
            await update_job(job_id, "failed", error=msg, screenshot=screenshot)
            result = {**_EMPTY_RESULT, "screenshot": screenshot}
            await _fire(job_id, chat_id, None, "failed", msg, result)


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


async def worker_loop() -> None:
    logger.info("Job worker started")
    while True:
        try:
            for job in await get_pending_jobs():
                jid = job["job_id"]
                if jid not in _running:
                    _running.add(jid)

                    async def _run(j: dict) -> None:
                        try:
                            await process_job(j)
                        finally:
                            _running.discard(j["job_id"])

                    asyncio.create_task(_run(job))
        except Exception as exc:
            logger.error("worker_loop error", extra={"error": str(exc)}, exc_info=True)

        await asyncio.sleep(3)
