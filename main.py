"""
Caleb Shopping Agent — main entry point.

Starts:
    • SQLite DB init
    • Job worker (background asyncio task)
    • Telegram bot (long polling)
    • FastAPI HTTP server (for /jobs REST API)
"""

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager

import uvicorn
from dotenv import load_dotenv

load_dotenv()  # must run before any module-level os.getenv() calls

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from db.database import create_job, get_items, get_job, init_db
from bot.telegram_bot import create_bot_app
from workers.job_worker import process_job, worker_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ── Lifespan ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Init DB
    await init_db()
    logger.info("Database ready")

    # Start background job worker
    worker_task = asyncio.create_task(worker_loop())

    # Start Telegram bot (long polling)
    bot = create_bot_app()
    await bot.initialize()
    await bot.start()
    await bot.updater.start_polling(drop_pending_updates=True)
    logger.info("Telegram bot polling started")

    yield

    # Graceful shutdown
    logger.info("Shutting down...")
    await bot.updater.stop()
    await bot.stop()
    await bot.shutdown()
    worker_task.cancel()


# ── FastAPI app ────────────────────────────────────────────────────────────────

app = FastAPI(title="Caleb Shopping Agent", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


class CreateJobRequest(BaseModel):
    chat_id: int


@app.post("/jobs", status_code=201)
async def api_create_job(req: CreateJobRequest):
    items = await get_items(req.chat_id)
    if not items:
        raise HTTPException(status_code=400, detail="No items found for this chat_id")

    job_id = str(uuid.uuid4())[:8]
    job = await create_job(job_id, req.chat_id)
    asyncio.create_task(process_job(job))

    return {"job_id": job_id, "status": "pending"}


@app.get("/jobs/{job_id}")
async def api_get_job(job_id: str):
    job = await get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=False,
    )
