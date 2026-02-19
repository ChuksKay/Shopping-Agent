"""
Item parser — converts free-form text into structured item dicts.

Supported input examples:
    "indomie chicken x2"             → {name: "indomie chicken", qty: 2}
    "milk 2% 4L (max $8)"            → {name: "milk 2% 4L", max_price: 8.0}
    "eggs 12 pack"                   → {name: "eggs 12 pack", qty: 1}
    "3x bread"                       → {name: "bread", qty: 3}
    "noodles brand:indomie x3"       → {name: "noodles", brand: "indomie", qty: 3}
    "orange juice (max $5) x2"       → {name: "orange juice", max_price: 5.0, qty: 2}
"""

import re

# Unit words that follow a number as a descriptor, NOT a quantity multiplier
_UNIT_WORDS = {
    "pack", "pk", "ct", "count", "piece", "pcs", "set",
    "kg", "g", "mg", "lb", "lbs", "oz",
    "l", "ml", "litre", "liter", "liters", "litres",
    "dozen", "box", "bag", "can", "bottle", "roll", "rolls",
    "sheet", "sheets", "pod", "pods", "tab", "tabs",
}

# Price constraint pattern: (max $8), max $8, under $10, up to $5, at most $3
_PRICE_PAT = re.compile(
    r"\(?\s*(?:max(?:imum)?|under|up\s+to|at\s+most)\s*:?\s*\$?([\d.]+)\s*\)?",
    re.IGNORECASE,
)

# Explicit brand pattern: brand:indomie  or  (brand: Dempster's)
_BRAND_PAT = re.compile(
    r"\(?\s*brand\s*:\s*([^\s),]+)\s*\)?",
    re.IGNORECASE,
)

# Qty suffix: " x2", " X3", " ×4" at end of string
_QTY_SUFFIX = re.compile(r"\s+[xX×](\d+)$")

# Qty prefix: "2x ", "3X ", "2 x " at start
_QTY_PREFIX = re.compile(r"^(\d+)\s*[xX×]\s+(.+)$")

# Bare trailing integer: "milk 2" — only treated as qty if not preceded by a unit word
_QTY_BARE = re.compile(r"\s+(\d+)$")


def parse_item(raw: str) -> dict:
    """
    Parse a single item line into a structured dict.

    Returns:
        {
            "name":      str,
            "qty":       int   (default 1),
            "max_price": float | None,
            "brand":     str   | None,
        }
    """
    s = raw.strip()
    qty = 1
    max_price = None
    brand = None

    # ── 1. Extract price constraint ───────────────────────────────────────────
    # Replace the matched span with a single space so adjacent tokens don't merge
    m = _PRICE_PAT.search(s)
    if m:
        max_price = float(m.group(1))
        s = re.sub(r"\s+", " ", s[: m.start()] + " " + s[m.end() :]).strip()

    # ── 2. Extract explicit brand ─────────────────────────────────────────────
    m = _BRAND_PAT.search(s)
    if m:
        brand = m.group(1).strip()
        s = re.sub(r"\s+", " ", s[: m.start()] + " " + s[m.end() :]).strip()

    # ── 3. Extract quantity ───────────────────────────────────────────────────
    # Priority: "x2" suffix > "2x" prefix > bare trailing integer

    m = _QTY_SUFFIX.search(s)
    if m:
        qty = int(m.group(1))
        s = s[: m.start()].strip()

    elif m := _QTY_PREFIX.match(s):
        qty = int(m.group(1))
        s = m.group(2).strip()

    else:
        m = _QTY_BARE.search(s)
        if m:
            # Only treat as qty if the preceding word isn't a unit descriptor
            preceding = s[: m.start()].strip()
            last_word = preceding.split()[-1].lower().rstrip("s") if preceding else ""
            if last_word not in _UNIT_WORDS:
                qty = int(m.group(1))
                s = preceding

    # ── 4. Normalise name ─────────────────────────────────────────────────────
    name = re.sub(r"\s+", " ", s).strip(" ,()[]")

    return {
        "name": name,
        "qty": max(1, qty),
        "max_price": max_price,
        "brand": brand or None,
    }


def parse_items(text: str) -> list[dict]:
    """
    Split *text* by newlines or commas, parse each token with parse_item().
    Skips blank lines and lines starting with '/'.
    """
    items = []
    for raw in re.split(r"[\n,]+", text):
        raw = raw.strip()
        if not raw or raw.startswith("/"):
            continue
        parsed = parse_item(raw)
        if parsed["name"]:
            items.append(parsed)
    return items
