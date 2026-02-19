"""
Conversational AI layer — routes free-form Telegram messages through Claude.

Claude uses tool calls to interact with the shopping list and trigger cart builds,
so the user can chat naturally instead of memorising commands.
"""

import asyncio
import logging
import os
import uuid

import anthropic

from agent.parser import parse_items
from db.database import (
    add_items as db_add_items,
    clear_items as db_clear_items,
    create_job,
    get_chat,
    get_items,
    upsert_chat,
)

logger = logging.getLogger(__name__)

_client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))

# Per-chat conversation history (in-memory; resets on server restart)
_histories: dict[int, list] = {}
_MAX_HISTORY = 20

_SYSTEM = """\
You are a friendly, casual shopping assistant for Walmart.ca. \
You help the user manage their grocery list and build their cart. \
Think of yourself as a smart friend who handles their grocery runs.

Rules:
- Keep replies SHORT — 1 to 3 sentences unless you're listing items.
- When the user mentions items to buy, call add_to_list right away.
- When they say things like "go", "shop", "build my cart", "checkout", "order now", call build_cart.
- When they say "clear", "reset", "new list", or "start over", call clear_list.
- If they mention a postal code or location, call set_location.
- Never invent items, prices, or availability.
- If something is ambiguous, ask ONE short question.
- You can use emojis sparingly to be friendly.
"""

_TOOLS = [
    {
        "name": "add_to_list",
        "description": (
            "Add one or more items to the shopping list. "
            "Include quantities, brands, and max prices as the user said them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Item strings exactly as the user wrote them, "
                        "e.g. ['2x milk', 'eggs', 'wonder bread', 'indomie x4 (max $6)']"
                    ),
                }
            },
            "required": ["items"],
        },
    },
    {
        "name": "get_list",
        "description": "Fetch the user's current shopping list.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "clear_list",
        "description": "Remove all items from the shopping list.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "build_cart",
        "description": (
            "Build the Walmart.ca cart from the current list. "
            "Call this when the user is ready to shop."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_location",
        "description": "Save the user's postal code and delivery/pickup preference.",
        "input_schema": {
            "type": "object",
            "properties": {
                "postal_code": {
                    "type": "string",
                    "description": "Canadian postal code, e.g. M5V3A1",
                },
                "mode": {
                    "type": "string",
                    "enum": ["delivery", "pickup"],
                },
            },
            "required": ["postal_code"],
        },
    },
]


async def handle_message(
    chat_id: int,
    text: str,
    send_fn,        # async (text: str) -> None
    trigger_job_fn, # async (job_id: str) -> None
) -> None:
    """
    Process a free-form message and reply conversationally.

    send_fn       — sends a text reply to the user.
    trigger_job_fn — called with a new job_id when the user wants to build the cart.
    """
    history = _histories.setdefault(chat_id, [])
    history.append({"role": "user", "content": text})

    # Build context-aware system prompt
    chat = await get_chat(chat_id)
    postal = chat["postal_code"] if chat else "not set"
    mode   = chat["mode"]        if chat else "delivery"
    rows   = await get_items(chat_id)
    list_summary = (
        ", ".join(f"{r['qty']}x {r['text']}" for r in rows) if rows else "empty"
    )
    system = (
        f"{_SYSTEM}\n\n"
        f"User's current list: {list_summary}\n"
        f"Postal code: {postal} | Mode: {mode}"
    )

    messages = list(history)

    # Agentic tool loop
    while True:
        response = await _client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=system,
            tools=_TOOLS,
            messages=messages,
        )

        tool_calls  = [b for b in response.content if b.type == "tool_use"]
        text_blocks = [b.text for b in response.content if hasattr(b, "text") and b.text]

        if not tool_calls:
            reply = " ".join(text_blocks).strip()
            if reply:
                await send_fn(reply)
            history.append({"role": "assistant", "content": response.content})
            break

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for tc in tool_calls:
            result = await _run_tool(tc.name, tc.input, chat_id, trigger_job_fn)
            logger.info("Tool called", extra={"tool": tc.name, "result": result[:120]})
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tc.id,
                "content": result,
            })

        messages.append({"role": "user", "content": tool_results})

    _histories[chat_id] = messages[-_MAX_HISTORY:]


async def _run_tool(
    name: str,
    inputs: dict,
    chat_id: int,
    trigger_job_fn,
) -> str:
    try:
        if name == "get_list":
            rows = await get_items(chat_id)
            if not rows:
                return "Shopping list is empty."
            lines = []
            for r in rows:
                line = f"{r['qty']}x {r['text']}"
                if r.get("brand"):
                    line += f" (brand: {r['brand']})"
                if r.get("max_price") is not None:
                    line += f" (max ${r['max_price']:.2f})"
                lines.append(line)
            return "Current list:\n" + "\n".join(lines)

        elif name == "add_to_list":
            raw_items = inputs.get("items", [])
            parsed = []
            for raw in raw_items:
                parsed.extend(parse_items(raw))
            if not parsed:
                return "Couldn't parse any items from the input."
            if not await get_chat(chat_id):
                await upsert_chat(chat_id)
            await db_add_items(chat_id, parsed)
            labels = [f"{p['qty']}x {p['name']}" for p in parsed]
            return f"Added: {', '.join(labels)}"

        elif name == "clear_list":
            await db_clear_items(chat_id)
            return "Shopping list cleared."

        elif name == "build_cart":
            rows = await get_items(chat_id)
            if not rows:
                return "The shopping list is empty — nothing to build."
            job_id = str(uuid.uuid4())[:8]
            await create_job(job_id, chat_id)
            await trigger_job_fn(job_id)
            return f"Cart build started (job {job_id}). I'll message you when it's ready."

        elif name == "set_location":
            postal = inputs.get("postal_code", "").upper().replace(" ", "")
            mode   = inputs.get("mode", "delivery")
            existing = await get_chat(chat_id)
            store    = existing["store"] if existing else ""
            await upsert_chat(chat_id, mode=mode, postal_code=postal, store=store)
            return f"Location saved: {postal}, {mode}."

        else:
            return f"Unknown tool: {name}"

    except Exception as exc:
        logger.error("Tool error", extra={"tool": name, "error": str(exc)}, exc_info=True)
        return f"Error: {exc}"
