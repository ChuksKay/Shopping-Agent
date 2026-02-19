"""
Caleb Shopping Agent — main entry point.

Starts:
    • Structured JSON logging
    • SQLite DB init
    • Job worker (background asyncio task)
    • Telegram bot (long polling)
    • FastAPI HTTP server (for /jobs REST API)
"""

import asyncio
import json
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


# ── Structured JSON logging ────────────────────────────────────────────────────

class _JSONFormatter(logging.Formatter):
    """One JSON object per log line — machine-readable and grep-friendly."""

    _SKIP = frozenset({
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "taskName",
    })

    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        out: dict = {
            "ts":     self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level":  record.levelname,
            "logger": record.name,
            "msg":    record.message,
        }
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        # Bubble up any extra= fields passed by callers
        for k, v in record.__dict__.items():
            if k not in self._SKIP:
                out[k] = v
        return json.dumps(out, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(_JSONFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # Keep uvicorn access logs readable but structured
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True


setup_logging(os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)


# ── Lifespan ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("Database ready")

    worker_task = asyncio.create_task(worker_loop())

    bot = create_bot_app()
    await bot.initialize()
    await bot.start()
    await bot.updater.start_polling(drop_pending_updates=True)
    logger.info("Telegram bot polling started")

    yield

    logger.info("Shutting down")
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
        log_config=None,   # let our handler take over
    )
