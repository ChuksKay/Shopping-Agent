"""
Telegram bot — long-polling entry point.

Commands:
    /start | /help          show help
    /set <postal> [mode]    save postal code + delivery|pickup
    /add <items>            add items to your list
    /list                   show current list
    /clear                  clear the list
    /run                    build the Walmart.ca cart
    /status <job_id>        check a job
    /link [confirm]         link your Walmart account (headful browser)
    /link_done              confirm login is complete and save session
"""

import asyncio
import logging
import os
import uuid
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from agent.parser import parse_items
from agent.walmart import WalmartLinker
from db.database import (
    add_items,
    clear_items,
    create_job,
    get_chat,
    get_items,
    get_job,
    upsert_chat,
)
from workers.job_worker import process_job, register_callback

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
SESSION_PATH: str = os.getenv("SESSION_PATH", "sessions/walmart_session.json")

# In-memory map of chat_id → active WalmartLinker (for /link flow)
_link_sessions: dict[int, WalmartLinker] = {}

# ── Help text ──────────────────────────────────────────────────────────────────

_HELP = """
*Caleb Shopping Agent* 🛒

*Commands*
/set `<postal>` `[delivery|pickup]` — set postal code & fulfilment mode
  _e.g. /set M5V3A1 delivery_

/add `<items>` — add items (comma or newline separated, qty prefix ok)
  _e.g. /add milk, 2x eggs, bread_

/list — show current list
/clear — clear current list

/run — build your Walmart\.ca cart
/status `<job_id>` — check a job

/link — link your Walmart account \(opens visible browser\)
/link confirm — overwrite existing session
/link\_done — save session after logging in

/help — show this message
""".strip()


# ── /help & /start ─────────────────────────────────────────────────────────────

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_HELP, parse_mode="MarkdownV2")


# ── /set ───────────────────────────────────────────────────────────────────────

async def set_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    args = context.args

    if not args:
        await update.message.reply_text(
            "Usage: /set <postal_code> [delivery|pickup]\n"
            "Example: /set M5V3A1 delivery"
        )
        return

    mode = "delivery"
    if args[-1].lower() in ("delivery", "pickup"):
        mode = args[-1].lower()
        postal_parts = args[:-1]
    else:
        postal_parts = args

    postal_code = " ".join(postal_parts).upper()

    existing = await get_chat(chat_id)
    store = existing["store"] if existing else ""
    await upsert_chat(chat_id, mode=mode, postal_code=postal_code, store=store)

    await update.message.reply_text(
        f"Saved — postal: `{postal_code}` | mode: `{mode}`",
        parse_mode="Markdown",
    )


# ── /add ───────────────────────────────────────────────────────────────────────

async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    text = update.message.text or ""
    parts = text.split(None, 1)

    if len(parts) < 2:
        await update.message.reply_text(
            "Usage: /add <items>\nExample: /add milk, 2x eggs, bread"
        )
        return

    items = parse_items(parts[1])
    if not items:
        await update.message.reply_text(
            "Couldn't parse any items. Try: /add milk, 2x eggs, bread"
        )
        return

    # Ensure chat row exists
    if not await get_chat(chat_id):
        await upsert_chat(chat_id)

    await add_items(chat_id, items)

    lines = "\n".join(f"• {qty}x {name}" for name, qty in items)
    await update.message.reply_text(f"Added:\n{lines}")


# ── /list ──────────────────────────────────────────────────────────────────────

async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    rows = await get_items(chat_id)

    if not rows:
        await update.message.reply_text("Your list is empty. Use /add to add items.")
        return

    lines = "\n".join(f"• {r['qty']}x {r['text']}" for r in rows)
    chat = await get_chat(chat_id)
    postal = chat["postal_code"] if chat else "not set"
    mode = chat["mode"] if chat else "delivery"

    await update.message.reply_text(
        f"*Shopping List*\n{lines}\n\n"
        f"Postal: `{postal}` | Mode: `{mode}`",
        parse_mode="Markdown",
    )


# ── /clear ─────────────────────────────────────────────────────────────────────

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await clear_items(update.effective_chat.id)
    await update.message.reply_text("List cleared.")


# ── /status ────────────────────────────────────────────────────────────────────

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /status <job_id>")
        return

    job = await get_job(args[0])
    if not job:
        await update.message.reply_text(f"Job `{args[0]}` not found.", parse_mode="Markdown")
        return

    status = job["status"]
    msg = f"Job `{job['job_id']}`\nStatus: *{status}*"

    if status == "done" and job["result_url"]:
        msg += f"\n\nCart URL:\n{job['result_url']}"
    elif status in ("failed", "needs_user") and job["error"]:
        msg += f"\nError: {job['error']}"
        if status == "needs_user":
            msg += (
                "\n\nWalmart showed a bot/CAPTCHA challenge. "
                "Use /link to re-authenticate, then /run again."
            )

    await update.message.reply_text(msg, parse_mode="Markdown")


# ── /run ───────────────────────────────────────────────────────────────────────

async def run_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    rows = await get_items(chat_id)

    if not rows:
        await update.message.reply_text("Your list is empty. Use /add first.")
        return

    job_id = str(uuid.uuid4())[:8]
    await create_job(job_id, chat_id)

    await update.message.reply_text(
        f"Building your Walmart\.ca cart\.\.\. 🛒\nJob ID: `{job_id}`\n"
        "I'll send you the link when it's ready\.",
        parse_mode="MarkdownV2",
    )

    async def on_done(cid: int, url: str | None, status: str, error: str | None) -> None:
        if status == "done" and url:
            await context.bot.send_message(
                chat_id=cid,
                text=f"Cart ready! 🎉\n{url}\n\nJob: `{job_id}`",
                parse_mode="Markdown",
            )
        elif status == "needs_user":
            await context.bot.send_message(
                chat_id=cid,
                text=(
                    f"Bot challenge detected 🤖\n"
                    "Use /link to re-authenticate your Walmart account, then /run again.\n"
                    f"Job: `{job_id}`"
                ),
                parse_mode="Markdown",
            )
        else:
            await context.bot.send_message(
                chat_id=cid,
                text=f"Cart build failed ❌\nJob: `{job_id}`\nError: {error or 'Unknown'}",
                parse_mode="Markdown",
            )

    register_callback(job_id, on_done)
    job = await get_job(job_id)
    asyncio.create_task(process_job(job))


# ── /link ──────────────────────────────────────────────────────────────────────

async def link_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /link          — start the Walmart account-linking flow.
    /link confirm  — overwrite an existing session without re-asking.
    """
    chat_id = update.effective_chat.id
    args = context.args
    confirming = bool(args and args[0].lower() == "confirm")

    session_file = Path(SESSION_PATH)

    # If a session already exists and user hasn't confirmed, ask first
    if session_file.exists() and not confirming:
        await update.message.reply_text(
            "A Walmart session already exists.\n"
            "Send `/link confirm` to overwrite it.",
            parse_mode="Markdown",
        )
        return

    # Close any stale linker for this chat
    await _close_linker(chat_id)

    await update.message.reply_text(
        "Opening Walmart.ca in a browser on your machine...\n"
        "Log in manually, then send /link\\_done when finished.",
        parse_mode="MarkdownV2",
    )

    linker = WalmartLinker()
    _link_sessions[chat_id] = linker

    try:
        await linker.start()
    except Exception as exc:
        _link_sessions.pop(chat_id, None)
        logger.error("Failed to open browser for /link: %s", exc, exc_info=True)
        await update.message.reply_text(f"Failed to open browser: {exc}")


async def link_done_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /link_done — called after the user has logged in at the browser.
    Checks login state, saves session, and closes the browser.
    """
    chat_id = update.effective_chat.id
    linker = _link_sessions.get(chat_id)

    if linker is None:
        await update.message.reply_text(
            "No active linking session. Send /link to start."
        )
        return

    try:
        if not await linker.is_logged_in():
            await update.message.reply_text(
                "Doesn't look like you're logged in yet.\n"
                "Complete the login in the browser window, then send /link\\_done again.",
                parse_mode="MarkdownV2",
            )
            return

        await linker.save_session(SESSION_PATH)
        await linker.close()
        _link_sessions.pop(chat_id, None)

        await update.message.reply_text("Walmart linked ✅")

    except Exception as exc:
        logger.error("Error in /link_done: %s", exc, exc_info=True)
        await update.message.reply_text(f"Error saving session: {exc}")
        await _close_linker(chat_id)


# ── Fallback ───────────────────────────────────────────────────────────────────

async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Unknown command. Use /help to see available commands.")


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _close_linker(chat_id: int) -> None:
    linker = _link_sessions.pop(chat_id, None)
    if linker:
        try:
            await linker.close()
        except Exception:
            pass


# ── App factory ────────────────────────────────────────────────────────────────

def create_bot_app() -> Application:
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set in environment")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", help_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("set", set_command))
    app.add_handler(CommandHandler("add", add_command))
    app.add_handler(CommandHandler("list", list_command))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(CommandHandler("run", run_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("link", link_command))
    app.add_handler(CommandHandler("link_done", link_done_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback))

    return app
