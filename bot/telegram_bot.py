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
    /screenshot <job_id>    send the failure screenshot
    /resume <job_id>        open headful browser to solve bot challenge
    /continue <job_id>      verification done; resume cart build
    /link [confirm]         link your Walmart account (headful browser)
    /link_done              confirm login is complete and save session
"""

import asyncio
import logging
import os
import uuid
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from agent.parser import parse_items
from agent.walmart import WalmartLinker, WalmartResumeSession
from bot.ai_handler import handle_message as ai_handle
from db.database import (
    add_items,
    clear_items,
    create_job,
    get_chat,
    get_items,
    get_job,
    update_job,
    upsert_chat,
)
from workers.job_worker import process_job, register_callback

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
SESSION_PATH: str = os.getenv("SESSION_PATH", "sessions/walmart_session.json")

# In-memory map of chat_id → active WalmartLinker (for /link flow)
_link_sessions: dict[int, WalmartLinker] = {}

# In-memory map of job_id → active WalmartResumeSession (for /resume flow)
_resume_sessions: dict[str, WalmartResumeSession] = {}

# ── Inline keyboard helpers ────────────────────────────────────────────────────

def _challenge_keyboard(job_id: str) -> InlineKeyboardMarkup:
    """Two-button keyboard shown when Walmart triggers a bot challenge."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔓 Open Browser to Solve", callback_data=f"resume:{job_id}")],
        [InlineKeyboardButton("✅ Done — Resume Cart Build", callback_data=f"continue:{job_id}")],
    ])


# ── Help text ──────────────────────────────────────────────────────────────────

_HELP = """
*Caleb Shopping Agent* 🛒

*Commands*
/set `<postal>` `[delivery|pickup]` - set postal code & mode
  _e.g. /set M5V3A1 delivery_

/add `<items>` - add items (comma or newline separated)
  Supports qty, brand, max price:
  _2x bread_, _indomie chicken x2_
  _milk 2% 4L (max $8)_, _noodles brand:indomie x3_

/list - show current list
/clear - clear current list

/run - build your Walmart.ca cart
/status `<job_id>` - check a job
/screenshot `<job_id>` - see the failure screenshot

*If Walmart shows a bot challenge:*
/resume `<job_id>` - open browser so you can verify
/continue `<job_id>` - done verifying; resume cart build

*Account linking*
/link - link your Walmart account (opens visible browser)
/link confirm - overwrite existing session
`/link_done` - save session after logging in

/help - show this message
""".strip()


# ── /help & /start ─────────────────────────────────────────────────────────────

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_HELP, parse_mode="Markdown")


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
            "Usage: /add <items>\n"
            "Examples:\n"
            "  /add milk, 2x eggs, bread\n"
            "  /add indomie chicken x2\n"
            "  /add milk 2% 4L (max $8)\n"
            "  /add noodles brand:indomie x3"
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

    lines = []
    for item in items:
        line = f"• {item['qty']}x {item['name']}"
        extras = []
        if item.get("brand"):
            extras.append(f"brand: {item['brand']}")
        if item.get("max_price") is not None:
            extras.append(f"max ${item['max_price']:.2f}")
        if extras:
            line += f"  _({', '.join(extras)})_"
        lines.append(line)

    await update.message.reply_text(
        "Added:\n" + "\n".join(lines),
        parse_mode="Markdown",
    )


# ── /list ──────────────────────────────────────────────────────────────────────

async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    rows = await get_items(chat_id)

    if not rows:
        await update.message.reply_text("Your list is empty. Use /add to add items.")
        return

    lines = []
    for r in rows:
        line = f"• {r['qty']}x {r['text']}"
        extras = []
        if r.get("brand"):
            extras.append(f"brand: {r['brand']}")
        if r.get("max_price") is not None:
            extras.append(f"max ${r['max_price']:.2f}")
        if extras:
            line += f"  _({', '.join(extras)})_"
        lines.append(line)

    chat = await get_chat(chat_id)
    postal = chat["postal_code"] if chat else "not set"
    mode = chat["mode"] if chat else "delivery"

    await update.message.reply_text(
        f"*Shopping List*\n" + "\n".join(lines) + f"\n\nPostal: `{postal}` | Mode: `{mode}`",
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
        f"Building your Walmart.ca cart... 🛒\nJob ID: `{job_id}`\n"
        "I'll send you the link when it's ready.",
        parse_mode="Markdown",
    )

    async def on_done(
        cid: int,
        url: str | None,
        status: str,
        error: str | None = None,
        result: dict | None = None,
    ) -> None:
        res = result or {}
        added: list[str] = res.get("added", [])
        failed: list[str] = res.get("failed", [])
        total = len(added) + len(failed)

        if status == "done" and url:
            if failed:
                summary = (
                    f"⚠️ {len(added)}/{total} items added\n"
                    + (("✅ " + ", ".join(added) + "\n") if added else "")
                    + "❌ Failed: " + ", ".join(failed)
                )
            else:
                summary = f"✅ All {total} item(s) added"

            await context.bot.send_message(
                chat_id=cid,
                text=(
                    f"Cart ready! 🎉\n{summary}\n\n"
                    f"🛒 {url}\n\n"
                    "_Make sure you're logged into your Walmart account in your browser before opening the link._\n\n"
                    f"Job: `{job_id}`"
                ),
                parse_mode="Markdown",
            )
        elif status == "needs_user":
            await context.bot.send_message(
                chat_id=cid,
                text=(
                    "Walmart needs a quick verification check 🤖\n"
                    "Tap *Open Browser* to solve it on your Mac, "
                    "then tap *Done* when finished."
                ),
                parse_mode="Markdown",
                reply_markup=_challenge_keyboard(job_id),
            )
        else:
            fail_note = ""
            if failed:
                fail_note = "\nFailed items: " + ", ".join(failed)
            await context.bot.send_message(
                chat_id=cid,
                text=f"Cart build failed ❌\nJob: `{job_id}`\nError: {error or 'Unknown'}{fail_note}",
                parse_mode="Markdown",
            )

    register_callback(job_id, on_done)
    job = await get_job(job_id)
    asyncio.create_task(process_job(job))


# ── /screenshot ────────────────────────────────────────────────────────────────

async def screenshot_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /screenshot <job_id>")
        return

    job_id = args[0]
    path = Path(f"storage/screenshots/{job_id}.png")

    if not path.exists():
        await update.message.reply_text(
            f"No screenshot found for job `{job_id}`.", parse_mode="Markdown"
        )
        return

    await update.message.reply_photo(
        photo=open(path, "rb"),
        caption=f"Screenshot for job `{job_id}`",
        parse_mode="Markdown",
    )


# ── /resume ────────────────────────────────────────────────────────────────────

async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /resume <job_id>  — open a headful browser so the user can solve the
    bot challenge, then send /continue <job_id> when done.
    """
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /resume <job_id>")
        return

    job_id = args[0]
    job = await get_job(job_id)
    if not job:
        await update.message.reply_text(
            f"Job `{job_id}` not found.", parse_mode="Markdown"
        )
        return

    if job["status"] != "needs_user":
        await update.message.reply_text(
            f"Job `{job_id}` is `{job['status']}` — only `needs_user` jobs can be resumed.",
            parse_mode="Markdown",
        )
        return

    # Close any stale session for this job
    await _close_resume_session(job_id)

    chat = await get_chat(job["chat_id"])
    postal_code = chat["postal_code"] if chat else ""

    await update.message.reply_text(
        f"Opening Walmart.ca in a browser on your machine...\n"
        f"Complete any CAPTCHA or verification, then send `/continue {job_id}` when done.",
        parse_mode="Markdown",
    )

    session = WalmartResumeSession(postal_code=postal_code)
    _resume_sessions[job_id] = session
    try:
        await session.start()
    except Exception as exc:
        _resume_sessions.pop(job_id, None)
        logger.error("Failed to open resume browser", extra={"job_id": job_id, "error": str(exc)})
        await update.message.reply_text(f"Failed to open browser: {exc}")


# ── /continue ──────────────────────────────────────────────────────────────────

async def continue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /continue <job_id>  — called after the user has solved the bot challenge
    in the headful browser opened by /resume.  Saves the fresh session,
    closes the browser, resets the job, and re-runs the cart build.
    """
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /continue <job_id>")
        return

    job_id = args[0]
    session = _resume_sessions.pop(job_id, None)
    if session is None:
        await update.message.reply_text(
            f"No active resume session for `{job_id}`.\n"
            "Send /resume first.",
            parse_mode="Markdown",
        )
        return

    try:
        await session.save_session()
        await session.close()
    except Exception as exc:
        logger.warning("Error closing resume session", extra={"job_id": job_id, "error": str(exc)})

    job = await get_job(job_id)
    if not job:
        await update.message.reply_text(
            f"Job `{job_id}` not found.", parse_mode="Markdown"
        )
        return

    chat_id = job["chat_id"]
    await update.message.reply_text(
        f"Verification saved! Resuming cart build for job `{job_id}`... 🛒",
        parse_mode="Markdown",
    )

    # Reset job to pending so process_job() will run it again
    await update_job(job_id, "pending", error=None, screenshot=None)

    async def on_done(
        cid: int,
        url: str | None,
        status: str,
        error: str | None = None,
        result: dict | None = None,
    ) -> None:
        res = result or {}
        added: list[str] = res.get("added", [])
        failed: list[str] = res.get("failed", [])
        total = len(added) + len(failed)

        if status == "done" and url:
            if failed:
                summary = (
                    f"⚠️ {len(added)}/{total} items added\n"
                    + (("✅ " + ", ".join(added) + "\n") if added else "")
                    + "❌ Failed: " + ", ".join(failed)
                )
            else:
                summary = f"✅ All {total} item(s) added"
            await context.bot.send_message(
                chat_id=cid,
                text=f"Cart ready! 🎉\n{summary}\n\n🛒 {url}\n\nJob: `{job_id}`",
                parse_mode="Markdown",
            )
        elif status == "needs_user":
            await context.bot.send_message(
                chat_id=cid,
                text=(
                    "Walmart needs another verification check 🤖\n"
                    "Tap *Open Browser* to try again."
                ),
                parse_mode="Markdown",
                reply_markup=_challenge_keyboard(job_id),
            )
        else:
            fail_note = ("\nFailed items: " + ", ".join(failed)) if failed else ""
            await context.bot.send_message(
                chat_id=cid,
                text=f"Cart build failed ❌\nJob: `{job_id}`\nError: {error or 'Unknown'}{fail_note}",
                parse_mode="Markdown",
            )

    register_callback(job_id, on_done)
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

    if session_file.exists() and not confirming:
        await update.message.reply_text(
            "A Walmart session already exists.\n"
            "Send `/link confirm` to overwrite it.",
            parse_mode="Markdown",
        )
        return

    await _close_linker(chat_id)

    await update.message.reply_text(
        "Opening Walmart.ca in a browser on your machine...\n"
        "Log in manually, then send /link_done when finished."
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
                "Complete the login in the browser window, then send /link_done again."
            )
            return

        await update.message.reply_text(
            "Login detected — saving your session, please wait a moment..."
        )
        await linker.save_session(SESSION_PATH)
        await linker.close()
        _link_sessions.pop(chat_id, None)

        await update.message.reply_text(
            "Walmart linked ✅\n"
            "Your session is saved. Send /run whenever you're ready to shop."
        )

    except Exception as exc:
        logger.error("Error in /link_done: %s", exc, exc_info=True)
        await update.message.reply_text(f"Error saving session: {exc}")
        await _close_linker(chat_id)


# ── AI conversational handler (catches all non-command messages) ───────────────

async def ai_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    text    = (update.message.text or "").strip()
    if not text:
        return

    async def send(msg: str) -> None:
        await context.bot.send_message(chat_id=chat_id, text=msg)

    async def trigger_job(job_id: str) -> None:
        async def on_done(
            cid: int,
            url: str | None,
            status: str,
            error: str | None = None,
            result: dict | None = None,
        ) -> None:
            res    = result or {}
            added  = res.get("added",  [])
            failed = res.get("failed", [])
            total  = len(added) + len(failed)

            if status == "done" and url:
                summary = (
                    (
                        f"⚠️ {len(added)}/{total} items added\n"
                        + (("✅ " + ", ".join(added) + "\n") if added else "")
                        + "❌ Failed: " + ", ".join(failed)
                    ) if failed else f"✅ All {total} item(s) added"
                )
                await context.bot.send_message(
                    chat_id=cid,
                    text=(
                        f"Cart ready! 🎉\n{summary}\n\n"
                        f"🛒 {url}\n\n"
                        "_Make sure you're logged into Walmart in your browser before opening._\n\n"
                        f"Job: `{job_id}`"
                    ),
                    parse_mode="Markdown",
                )
            elif status == "needs_user":
                await context.bot.send_message(
                    chat_id=cid,
                    text=(
                        "Walmart needs a quick verification check 🤖\n"
                        "Tap *Open Browser* to solve it on your Mac, "
                        "then tap *Done* when finished."
                    ),
                    parse_mode="Markdown",
                    reply_markup=_challenge_keyboard(job_id),
                )
            else:
                fail_note = ("\nFailed: " + ", ".join(failed)) if failed else ""
                await context.bot.send_message(
                    chat_id=cid,
                    text=f"Cart build failed ❌\nError: {error or 'Unknown'}{fail_note}\nJob: `{job_id}`",
                    parse_mode="Markdown",
                )

        register_callback(job_id, on_done)
        job = await get_job(job_id)
        asyncio.create_task(process_job(job))

    await ai_handle(chat_id, text, send, trigger_job)


# ── Inline button handler ──────────────────────────────────────────────────────

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles taps on the inline keyboard sent with bot-challenge messages.

    callback_data format:
        "resume:<job_id>"   — open headful browser so user can solve CAPTCHA
        "continue:<job_id>" — verification done; save session and re-run job
    """
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    if ":" not in data:
        return

    action, job_id = data.split(":", 1)
    chat_id = update.effective_chat.id

    # ── "Open Browser" tapped ──────────────────────────────────────────────────
    if action == "resume":
        job = await get_job(job_id)
        if not job:
            await query.edit_message_text(f"Job {job_id} not found.")
            return
        if job["status"] != "needs_user":
            await query.edit_message_text(
                f"Job is already `{job['status']}` — nothing to resume.",
                parse_mode="Markdown",
            )
            return

        # Update message: remove "Open Browser" button while browser opens,
        # keep "Done" button so user can tap it when finished.
        await query.edit_message_text(
            "Browser opened on your Mac 🖥️\n"
            "Complete the Walmart verification check, then tap *Done* below.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "✅ Done — Resume Cart Build",
                    callback_data=f"continue:{job_id}",
                )
            ]]),
        )

        await _close_resume_session(job_id)
        chat = await get_chat(job["chat_id"])
        postal_code = chat["postal_code"] if chat else ""
        session = WalmartResumeSession(postal_code=postal_code)
        _resume_sessions[job_id] = session
        try:
            await session.start()
        except Exception as exc:
            _resume_sessions.pop(job_id, None)
            logger.error("Failed to open resume browser", extra={"job_id": job_id, "error": str(exc)})
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"Couldn't open browser: {exc}",
            )

    # ── "Done" tapped ─────────────────────────────────────────────────────────
    elif action == "continue":
        session = _resume_sessions.pop(job_id, None)
        if session is None:
            # Might have already been closed or never opened
            await query.edit_message_text(
                "No active browser session found.\nTap *Open Browser* first.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "🔓 Open Browser to Solve",
                        callback_data=f"resume:{job_id}",
                    )
                ]]),
            )
            return

        await query.edit_message_text("Saving verification and resuming cart build... 🛒")

        try:
            await session.save_session()
            await session.close()
        except Exception as exc:
            logger.warning("Error closing resume session",
                           extra={"job_id": job_id, "error": str(exc)})

        job = await get_job(job_id)
        if not job:
            await context.bot.send_message(chat_id=chat_id, text=f"Job {job_id} not found.")
            return

        job_chat_id = job["chat_id"]
        await update_job(job_id, "pending", error=None, screenshot=None)

        async def on_done(
            cid: int,
            url: str | None,
            status: str,
            error: str | None = None,
            result: dict | None = None,
        ) -> None:
            res    = result or {}
            added  = res.get("added",  [])
            failed = res.get("failed", [])
            total  = len(added) + len(failed)

            if status == "done" and url:
                summary = (
                    (
                        f"⚠️ {len(added)}/{total} items added\n"
                        + (("✅ " + ", ".join(added) + "\n") if added else "")
                        + "❌ Failed: " + ", ".join(failed)
                    ) if failed else f"✅ All {total} item(s) added"
                )
                await context.bot.send_message(
                    chat_id=cid,
                    text=(
                        f"Cart ready! 🎉\n{summary}\n\n"
                        f"🛒 {url}\n\n"
                        "_Make sure you're logged into Walmart in your browser._\n\n"
                        f"Job: `{job_id}`"
                    ),
                    parse_mode="Markdown",
                )
            elif status == "needs_user":
                await context.bot.send_message(
                    chat_id=cid,
                    text=(
                        "Walmart needs another verification check 🤖\n"
                        "Tap *Open Browser* to try again."
                    ),
                    parse_mode="Markdown",
                    reply_markup=_challenge_keyboard(job_id),
                )
            else:
                fail_note = ("\nFailed: " + ", ".join(failed)) if failed else ""
                await context.bot.send_message(
                    chat_id=cid,
                    text=(
                        f"Cart build failed ❌\n"
                        f"Error: {error or 'Unknown'}{fail_note}\n"
                        f"Job: `{job_id}`"
                    ),
                    parse_mode="Markdown",
                )

        register_callback(job_id, on_done)
        asyncio.create_task(process_job(job))


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _close_linker(chat_id: int) -> None:
    linker = _link_sessions.pop(chat_id, None)
    if linker:
        try:
            await linker.close()
        except Exception:
            pass


async def _close_resume_session(job_id: str) -> None:
    session = _resume_sessions.pop(job_id, None)
    if session:
        try:
            await session.close()
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
    app.add_handler(CommandHandler("screenshot", screenshot_command))
    app.add_handler(CommandHandler("resume", resume_command))
    app.add_handler(CommandHandler("continue", continue_command))
    app.add_handler(CommandHandler("link", link_command))
    app.add_handler(CommandHandler("link_done", link_done_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ai_message))

    return app
