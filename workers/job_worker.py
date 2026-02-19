"""
Background job worker.

- worker_loop()       polls SQLite for 'pending' jobs every 3 s and runs them.
- process_job()       runs a single job (Walmart cart build) and fires a callback.
- register_callback() lets the bot register a coroutine to call on completion.
"""

import asyncio
import logging

from db.database import get_pending_jobs, get_chat, get_items, update_job
from agent.walmart import WalmartAgent, BotChallengeError

logger = logging.getLogger(__name__)

# job_id → async callable(chat_id, url, status, error)
_callbacks: dict[str, any] = {}


def register_callback(job_id: str, cb) -> None:
    _callbacks[job_id] = cb


async def process_job(job: dict) -> None:
    job_id = job["job_id"]
    chat_id = job["chat_id"]

    await update_job(job_id, "running")
    logger.info("Processing job %s for chat %d", job_id, chat_id)

    try:
        chat = await get_chat(chat_id)
        postal_code = chat["postal_code"] if chat else ""

        item_rows = await get_items(chat_id)
        if not item_rows:
            await update_job(job_id, "failed", error="No items in list")
            await _fire(job_id, chat_id, None, "failed", "No items in list")
            return

        # Build item dicts for the agent — include brand for smarter search
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
            cart_url = await agent.build_cart(items)

        await update_job(job_id, "done", result_url=cart_url)
        await _fire(job_id, chat_id, cart_url, "done")

    except BotChallengeError as exc:
        msg = str(exc)
        logger.warning("Job %s — bot challenge: %s", job_id, msg)
        await update_job(job_id, "needs_user", error=msg)
        await _fire(job_id, chat_id, None, "needs_user", msg)

    except Exception as exc:
        msg = str(exc)
        logger.error("Job %s failed: %s", job_id, msg, exc_info=True)
        await update_job(job_id, "failed", error=msg)
        await _fire(job_id, chat_id, None, "failed", msg)


async def _fire(
    job_id: str,
    chat_id: int,
    url: str | None,
    status: str,
    error: str | None = None,
) -> None:
    cb = _callbacks.pop(job_id, None)
    if cb:
        try:
            await cb(chat_id, url, status, error)
        except Exception as exc:
            logger.error("Callback error for job %s: %s", job_id, exc)


_running: set[str] = set()


async def worker_loop() -> None:
    """Continuously poll for pending jobs and launch them as async tasks."""
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
            logger.error("worker_loop error: %s", exc, exc_info=True)

        await asyncio.sleep(3)
