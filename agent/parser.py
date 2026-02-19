import re


def parse_items(text: str) -> list[tuple[str, int]]:
    """
    Parse a free-form grocery list into (item_name, qty) tuples.

    Supported formats (per line or comma-separated):
        milk
        2x milk
        2 milk
        milk x2
        milk, 3x eggs, bread
    """
    items: list[tuple[str, int]] = []

    for raw in re.split(r"[\n,]+", text):
        line = raw.strip()
        if not line or line.startswith("/"):
            continue

        qty = 1
        item = line

        # "2x milk" or "2 milk"
        m = re.match(r"^(\d+)\s*[xX]?\s+(.+)$", line)
        if m:
            qty = int(m.group(1))
            item = m.group(2).strip()
        else:
            # "milk x2" or "milk 2"
            m = re.match(r"^(.+?)\s+[xX]?(\d+)$", line)
            if m:
                item = m.group(1).strip()
                qty = int(m.group(2))

        if item:
            items.append((item, qty))

    return items
