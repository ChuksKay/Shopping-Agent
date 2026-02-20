"""
Conversational AI layer — routes free-form Telegram messages through Claude.

Claude uses tool calls to interact with the shopping list and trigger cart builds,
so the user can chat naturally instead of memorising commands.
"""

import asyncio
import json
import logging
import os
import re
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
You are a grocery shopping assistant for Walmart.ca Canada. \
Your job is to manage the user's grocery list and build their cart.

STRICT RULES — follow every one of these exactly:
1. ONLY add items the user EXPLICITLY names. NEVER add items they didn't mention, \
   even if they seem related or complementary. If the user says "milk", add only milk — \
   not butter, not cream.
2. If the user's message is vague or unclear, ask ONE short clarifying question \
   before calling add_to_list.
3. Pass quantities exactly as the user stated them — do NOT convert: \
   "3 packs of indomie", "a dozen eggs", "2 bags of rice". \
   The system will handle conversion.
4. Trigger build_cart when the user says: "go", "shop", "build cart", "order", \
   "checkout", "run it", "do it", "start shopping".
5. Trigger clear_list when the user says: "clear", "reset", "start over", "new list", \
   "remove everything".
6. Trigger set_location when the user mentions a postal code or city.
7. Keep replies SHORT — 1 to 3 sentences max.
8. Never invent prices, availability, or product details.
9. Do NOT suggest adding extra items unless the user explicitly asks for recommendations.
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


async def _ai_parse_items(raw_items: list[str]) -> list[dict]:
    """
    Use Claude Haiku to parse raw item strings into structured dicts.

    Handles natural language quantities ("a dozen", "3 packs of", "half a dozen")
    that the regex parser cannot. Falls back to regex on any failure.
    """
    if not raw_items:
        return []

    prompt = (
        "Parse these grocery shopping items into structured data.\n"
        "Return ONLY a JSON array — no explanation, no markdown.\n\n"
        "Items to parse: " + json.dumps(raw_items) + "\n\n"
        "For each item, output an object with:\n"
        '  "name": concise product name good for searching on Walmart.ca (string)\n'
        '  "qty": integer quantity — convert language: '
        '"a dozen"=12, "half dozen"=6, "a"=1, "a pack"=1, "a bag"=1, '
        '"a couple"=2, "a few"=3; default=1\n'
        '  "brand": brand name if explicitly stated, otherwise null\n'
        '  "max_price": numeric price cap if stated (e.g. "max $5" → 5.0), otherwise null\n\n'
        "IMPORTANT: Only include items that appear in the input. Do not add extras.\n"
        "Example input: [\"3 packs indomie chicken\", \"a dozen eggs\", \"2L milk max $5\"]\n"
        'Example output: [{"name":"indomie chicken noodles","qty":3,"brand":"Indomie","max_price":null},'
        '{"name":"eggs","qty":12,"brand":null,"max_price":null},'
        '{"name":"milk 2L","qty":1,"brand":null,"max_price":5.0}]\n\n'
        "Now parse:"
    )

    try:
        resp = await _client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        # Strip markdown code fences if present
        text = re.sub(r"^```[a-z]*\n?", "", text).rstrip("`").strip()
        parsed = json.loads(text)

        result = []
        for item in parsed:
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            brand = item.get("brand") or None
            max_p = item.get("max_price")
            result.append({
                "name":      name,
                "qty":       max(1, int(float(item.get("qty", 1)))),
                "brand":     brand,
                "max_price": float(max_p) if max_p is not None else None,
                "text":      name,
            })
        return result

    except Exception as exc:
        logger.warning("AI item parse failed, falling back to regex",
                       extra={"error": str(exc)})
        result = []
        for raw in raw_items:
            result.extend(parse_items(raw))
        return result


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
            # Use AI parser — understands "3 packs of X", "a dozen", etc.
            parsed = await _ai_parse_items(raw_items)
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
