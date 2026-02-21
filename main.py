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
import socket
import uuid
from contextlib import asynccontextmanager
from textwrap import dedent

import uvicorn
from dotenv import load_dotenv

load_dotenv()  # must run before any module-level os.getenv() calls

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
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


# ── Phone-solve flow ───────────────────────────────────────────────────────────

def _local_ip() -> str:
    """Return the Mac's local network IP (e.g. 192.168.x.x)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


def get_phone_solve_base() -> str:
    """
    Returns the base URL for the phone-solve page.
    Priority:
      1. PHONE_SOLVE_URL env var (e.g. an ngrok HTTPS URL)
      2. Mac's local IP at port 8000 (works when phone is on same WiFi)
    """
    return os.getenv("PHONE_SOLVE_URL", f"http://{_local_ip()}:8000").rstrip("/")


@app.get("/phone-solve/{job_id}", response_class=HTMLResponse)
async def phone_solve_page(job_id: str):
    """
    Mobile-friendly page that guides the user through sending their Walmart
    Akamai cookies to the bot.  The JavaScript snippet downloads the cookies
    as a text file — a data: URL, which bypasses all cross-origin and
    mixed-content restrictions.
    """
    # Compact JS that filters Akamai cookies and triggers a file download
    js = (
        "javascript:void((function(){"
        "var c=document.cookie.split(';')"
        ".filter(function(x){var n=x.trim().split('=')[0];"
        "return['_abck','ak_bmsc','bm_sv','bm_sz','bm_so'].indexOf(n)>=0;})"
        ".join(';');"
        "var a=document.createElement('a');"
        "a.href='data:text/plain;charset=utf-8,'+encodeURIComponent(c);"
        "a.download='wm_cookies.txt';"
        "document.body.appendChild(a);a.click();document.body.removeChild(a);"
        "alert('wm_cookies.txt downloaded! Send it to the Telegram bot.');"
        "})())"
    )

    html = dedent(f"""\
        <!DOCTYPE html>
        <html lang="en">
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <title>Solve Walmart Verification</title>
          <style>
            body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
                  padding:20px;max-width:480px;margin:0 auto;color:#1c1c1e;background:#f2f2f7}}
            h2{{color:#0071ce;margin-top:0}}
            .card{{background:#fff;border-radius:12px;padding:16px;margin:12px 0;
                   box-shadow:0 1px 3px rgba(0,0,0,.08)}}
            .card strong{{display:block;font-size:15px;margin-bottom:6px}}
            code{{display:block;word-break:break-all;font-size:11px;
                  background:#1c1c1e;color:#e5e5ea;padding:12px;
                  border-radius:8px;margin:8px 0;user-select:all;-webkit-user-select:all}}
            .btn{{display:block;width:100%;padding:15px;font-size:16px;font-weight:600;
                  border:none;border-radius:12px;cursor:pointer;margin-top:8px;
                  text-align:center;text-decoration:none}}
            .blue{{background:#007aff;color:#fff}}
            .green{{background:#34c759;color:#fff}}
            #msg{{text-align:center;color:#8e8e93;margin-top:12px;font-size:14px}}
          </style>
        </head>
        <body>
          <h2>📱 Solve Walmart Bot Check</h2>

          <div class="card">
            <strong>Step 1 — Open Walmart</strong>
            Open <a href="https://www.walmart.ca/en" target="_blank">walmart.ca</a>
            in another tab. Browse until the page loads without any "blocked" message.
          </div>

          <div class="card">
            <strong>Step 2 — Copy this code</strong>
            <code id="jscode">{js}</code>
            <button class="btn blue" onclick="copyCode()">📋 Copy Code</button>
          </div>

          <div class="card">
            <strong>Step 3 — Run it on Walmart</strong>
            Go to your Walmart tab. Tap the <b>address bar</b>, select all the text,
            paste the copied code, then tap <b>Go&nbsp;/&nbsp;Return</b>.<br><br>
            A file called <b>wm_cookies.txt</b> will download automatically.
          </div>

          <div class="card">
            <strong>Step 4 — Send the file to the bot</strong>
            Open <b>Telegram</b>, go to this bot's chat, and
            <b>send the wm_cookies.txt file</b> as an attachment.
            The bot will automatically import it and retry your cart build.
          </div>

          <p id="msg"></p>

          <script>
            function copyCode(){{
              var text = document.getElementById('jscode').innerText;
              if(navigator.clipboard){{
                navigator.clipboard.writeText(text).then(function(){{
                  document.getElementById('msg').innerText = '✅ Copied! Now go to Walmart.ca and paste in the URL bar.';
                }});
              }} else {{
                var r=document.createRange();
                r.selectNode(document.getElementById('jscode'));
                window.getSelection().removeAllRanges();
                window.getSelection().addRange(r);
                document.execCommand('copy');
                document.getElementById('msg').innerText = '✅ Copied!';
              }}
            }}
          </script>
        </body>
        </html>
    """)
    return HTMLResponse(html)


@app.post("/api/phone-cookies/{job_id}")
async def receive_phone_cookies(job_id: str, request: Request):
    """Receive Akamai cookie string POSTed from the phone-solve page."""
    body = await request.body()
    cookie_string = body.decode("utf-8", errors="ignore")
    from agent.walmart import merge_phone_cookies
    updated = merge_phone_cookies(cookie_string)
    logger.info("Phone cookies received", extra={"job_id": job_id, "updated": updated})
    return {"ok": True, "updated": updated}


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=False,
        log_config=None,   # let our handler take over
    )
