"""
Walmart.ca browser automation via Playwright.

Classes:
    WalmartAgent   – headless cart-building worker (used by job worker)
    WalmartLinker  – headful manual-login helper (used by /link command)
"""

import asyncio
import json
import logging
import os
import random
from pathlib import Path

import anthropic
from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PWTimeout,
)

logger = logging.getLogger(__name__)

_ai_client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))

SESSION_PATH: str = os.getenv("SESSION_PATH", "sessions/walmart_session.json")
HEADLESS: bool = os.getenv("HEADLESS", "true").lower() == "true"
WALMART_BASE = "https://www.walmart.ca"
SCREENSHOT_DIR = Path("storage/screenshots")

# Akamai / PerimeterX cookies that track visitor identity and get flagged.
# Stripping them before each headless run lets Akamai see a fresh visitor
# while keeping the Walmart auth cookies intact.
_TRACKING_COOKIES = {
    "_abck", "ak_bmsc", "bm_sv", "bm_sz",   # Akamai Bot Manager
    "pxcts", "_pxvid", "__pxvid",            # PerimeterX
    "akavpau_p1", "akavpau_p2",             # Akamai edge tokens
    "rxvt", "rxVisitor",                     # Rx visitor tracking
}


def _clean_session(path: str) -> dict:
    """
    Load a saved Playwright storage_state JSON and strip Akamai/PerimeterX
    tracking cookies so each headless run starts with a fresh bot-detection
    identity while preserving Walmart auth cookies.
    """
    import json as _json
    data = _json.loads(Path(path).read_text())
    before = len(data.get("cookies", []))
    data["cookies"] = [
        c for c in data.get("cookies", [])
        if c.get("name") not in _TRACKING_COOKIES
    ]
    after = len(data["cookies"])
    if before != after:
        logger.info(
            "Stripped tracking cookies from session",
            extra={"removed": before - after, "kept": after},
        )
    return data

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:133.0) "
    "Gecko/20100101 Firefox/133.0"
)

_STEALTH_ARGS: list[str] = []   # Firefox ignores Chrome-specific flags

_STEALTH_SCRIPT = """
// ── navigator.webdriver ───────────────────────────────────────────────────────
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// ── Language / locale ─────────────────────────────────────────────────────────
Object.defineProperty(navigator, 'languages', { get: () => ['en-CA', 'en-US', 'en'] });
Object.defineProperty(navigator, 'language',  { get: () => 'en-CA' });

// ── Platform / hardware ───────────────────────────────────────────────────────
Object.defineProperty(navigator, 'platform',            { get: () => 'MacIntel' });
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
Object.defineProperty(navigator, 'maxTouchPoints',      { get: () => 0 });

// ── Screen dimensions (consistent with 1280×900 viewport) ────────────────────
Object.defineProperty(screen, 'width',       { get: () => 1280 });
Object.defineProperty(screen, 'height',      { get: () => 900 });
Object.defineProperty(screen, 'availWidth',  { get: () => 1280 });
Object.defineProperty(screen, 'availHeight', { get: () => 860 });
Object.defineProperty(screen, 'colorDepth',  { get: () => 24 });
Object.defineProperty(screen, 'pixelDepth',  { get: () => 24 });
Object.defineProperty(window, 'outerWidth',  { get: () => 1280 });
Object.defineProperty(window, 'outerHeight', { get: () => 900 });

// ── Permissions ───────────────────────────────────────────────────────────────
const _origPermQuery = navigator.permissions.query.bind(navigator.permissions);
navigator.permissions.query = (params) => {
    if (params.name === 'notifications')
        return Promise.resolve({ state: typeof Notification !== 'undefined' ? Notification.permission : 'default' });
    return _origPermQuery(params).catch(() => Promise.resolve({ state: 'prompt' }));
};

// ── WebGL vendor/renderer ─────────────────────────────────────────────────────
try {
    const _getParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(p) {
        if (p === 37445) return 'Intel Inc.';
        if (p === 37446) return 'Intel Iris Pro OpenGL Engine';
        return _getParam.call(this, p);
    };
} catch(e) {}
try {
    const _getParam2 = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = function(p) {
        if (p === 37445) return 'Intel Inc.';
        if (p === 37446) return 'Intel Iris Pro OpenGL Engine';
        return _getParam2.call(this, p);
    };
} catch(e) {}

// ── Canvas noise ──────────────────────────────────────────────────────────────
try {
    const _origGetContext = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function(type, ...args) {
        const ctx = _origGetContext.call(this, type, ...args);
        if (ctx && type === '2d') {
            const _fill = ctx.fillText.bind(ctx);
            ctx.fillText = (t, x, y, ...r) => _fill(t, x + Math.random() * 0.05, y, ...r);
        }
        return ctx;
    };
} catch(e) {}
"""

_ADD_TO_CART = [
    '[data-automation-id="add-to-cart-btn"]',
    'button[aria-label*="Add to cart"]',
    'button[aria-label*="add to cart"]',
    'button:has-text("Add to cart")',
    'button:has-text("Add to Cart")',
]

_LOGGED_IN_INDICATORS = [
    '[data-automation-id="account-menu"]',
    'a[href*="/account/"]',
    'a[href*="sign-out"]',
    'a[href*="logout"]',
    'button[aria-label*="My Account"]',
]


class BotChallengeError(Exception):
    """Raised when Walmart presents a bot-detection or CAPTCHA page."""


# ── Human-like timing helpers ──────────────────────────────────────────────────

async def _delay(lo: float = 0.8, hi: float = 2.2) -> None:
    """Random sleep between lo and hi seconds."""
    await asyncio.sleep(random.uniform(lo, hi))


async def _short_delay() -> None:
    await asyncio.sleep(random.uniform(0.15, 0.45))


async def _human_mouse_move(page: Page, x: float, y: float) -> None:
    """Move mouse to (x, y) via a random intermediate waypoint."""
    mid_x = random.uniform(200, 900)
    mid_y = random.uniform(150, 600)
    await page.mouse.move(mid_x, mid_y, steps=random.randint(4, 8))
    await asyncio.sleep(random.uniform(0.05, 0.15))
    await page.mouse.move(x, y, steps=random.randint(6, 14))


async def _human_click(page: Page, locator) -> None:
    """Scroll element into view, move mouse naturally, then click."""
    try:
        await locator.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass
    box = None
    try:
        box = await locator.bounding_box()
    except Exception:
        pass
    if box:
        x = box["x"] + box["width"] * random.uniform(0.25, 0.75)
        y = box["y"] + box["height"] * random.uniform(0.25, 0.75)
        await _human_mouse_move(page, x, y)
        await asyncio.sleep(random.uniform(0.05, 0.18))
        await page.mouse.click(x, y)
    else:
        await locator.click()


async def _human_type(page: Page, locator, text: str) -> None:
    """Click field and type each character with randomised inter-key delay."""
    await _human_click(page, locator)
    await asyncio.sleep(random.uniform(0.2, 0.5))
    for char in text:
        await page.keyboard.type(char, delay=random.randint(60, 160))


async def _random_scroll(page: Page) -> None:
    """Scroll down a little, pause, scroll back — mimics a human scanning the page."""
    dist = random.randint(200, 500)
    await page.mouse.wheel(0, dist)
    await asyncio.sleep(random.uniform(0.4, 1.0))
    await page.mouse.wheel(0, -random.randint(50, 150))
    await asyncio.sleep(random.uniform(0.2, 0.5))


# ── Shared helpers ─────────────────────────────────────────────────────────────

async def _check_bot_challenge(page: Page) -> None:
    await asyncio.sleep(0.4)
    title = (await page.title()).lower()
    url = page.url.lower()
    try:
        snippet = (await page.content())[:3000].lower()
    except Exception:
        snippet = ""

    triggers = [
        "access denied", "robot check", "captcha", "unusual traffic",
        "verify you are human", "checking your browser", "ddos-guard",
        "please enable cookies", "sorry, you have been blocked",
        "403 forbidden", "just a moment",
    ]
    combined = f"{title} {url} {snippet}"
    for t in triggers:
        if t in combined:
            raise BotChallengeError(f"Bot challenge detected ({t!r}) at {page.url}")

    for sel in ['iframe[src*="captcha"]', 'iframe[src*="recaptcha"]', '[id*="captcha"]']:
        try:
            if await page.locator(sel).count() > 0:
                raise BotChallengeError(f"CAPTCHA element found ({sel})")
        except BotChallengeError:
            raise
        except Exception:
            pass


async def _dismiss_overlays(page: Page) -> None:
    for sel in [
        'button[aria-label="Close"]',
        'button[aria-label="close"]',
        '[data-automation-id="close-modal"]',
        'button:has-text("Continue shopping")',
        'button:has-text("No thanks")',
        '[data-automation-id="modal-close-btn"]',
    ]:
        try:
            el = page.locator(sel).first
            if await el.is_visible():
                await _human_click(page, el)
                await asyncio.sleep(random.uniform(0.3, 0.6))
        except Exception:
            pass


# ── Product selection helpers ──────────────────────────────────────────────────

async def _extract_search_results(page: Page) -> list[dict]:
    """
    Extract product info from a Walmart.ca search results page.

    Tries __NEXT_DATA__ JSON first (fastest, most complete), then falls
    back to DOM scraping.  Returns up to 12 products with:
        title, brand, price (float|None), badges (list[str]), url, sponsored (bool)
    """
    # ── 1. __NEXT_DATA__ (Next.js SSR payload) ─────────────────────────────────
    try:
        products = await page.evaluate("""
            () => {
                try {
                    const el = document.getElementById('__NEXT_DATA__');
                    if (!el) return null;
                    const data = JSON.parse(el.textContent);
                    // Common paths for Walmart.ca search result items
                    const stacks =
                        data?.props?.pageProps?.initialData?.searchResult?.itemStacks ||
                        data?.props?.pageProps?.initialData?.contentLayout?.modules;
                    if (!stacks || !stacks.length) return null;
                    const out = [];
                    for (const stack of stacks) {
                        const items = stack.items || stack.products || [];
                        for (const raw of items) {
                            const p = raw.item || raw;
                            if (!p || !p.name) continue;
                            const price =
                                p.priceInfo?.currentPrice?.price ??
                                p.salePrice ?? p.price ?? null;
                            const badges = [];
                            for (const f of (p.badges?.flags || [])) {
                                if (f.text) badges.push(f.text);
                            }
                            out.push({
                                title:     p.name,
                                brand:     p.brand || '',
                                price:     typeof price === 'number' ? price : null,
                                badges:    badges,
                                url:       p.canonicalUrl || '',
                                sponsored: !!(p.sponsoredProduct || p.isAd),
                            });
                            if (out.length >= 12) break;
                        }
                        if (out.length >= 12) break;
                    }
                    return out.length ? out : null;
                } catch(e) { return null; }
            }
        """)
        if products and len(products) > 0:
            return products
    except Exception:
        pass

    # ── 2. DOM fallback ────────────────────────────────────────────────────────
    try:
        products = await page.evaluate("""
            () => {
                const cards = Array.from(document.querySelectorAll(
                    '[data-item-id], [data-testid="list-view"], [class*="product-tile"]'
                )).slice(0, 12);
                return cards.map((card) => {
                    const titleEl =
                        card.querySelector('[data-automation-id="product-title"]') ||
                        card.querySelector('[class*="product-title"]');
                    const priceEl =
                        card.querySelector('[itemprop="price"]') ||
                        card.querySelector('[data-automation-id*="price"]') ||
                        card.querySelector('[class*="price-main"]');
                    const badgeEl =
                        card.querySelector('[data-automation-id*="badge"]') ||
                        card.querySelector('[class*="badge"]') ||
                        card.querySelector('[class*="flag"]');
                    const link = card.querySelector('a[href*="/en/ip/"], a[href*="/ip/"]');
                    const rawPrice = priceEl?.getAttribute('content') || priceEl?.innerText || '';
                    const numPrice = parseFloat(rawPrice.replace(/[^0-9.]/g, '')) || null;
                    return {
                        title:     titleEl?.innerText?.trim() || '',
                        brand:     '',
                        price:     numPrice,
                        badges:    badgeEl ? [badgeEl.innerText.trim()] : [],
                        url:       link?.href || '',
                        sponsored: false,
                    };
                }).filter(p => p.title && p.url);
            }
        """)
        if products and len(products) > 0:
            return products
    except Exception:
        pass

    return []


async def _ai_select_product(
    products: list[dict],
    item_name: str,
    brand: str | None,
    max_price: float | None,
) -> int | None:
    """
    Use Claude Haiku to pick the best product index from search results.

    Selection priority:
        1. Brand match (if brand specified)
        2. Price ≤ max_price (if specified)
        3. Best seller / popular badge
        4. Lowest price among qualifying products

    Returns 0-based index, or None if nothing qualifies.
    """
    if not products:
        return None

    constraints = []
    if brand:
        constraints.append(f'Brand must match: "{brand}"')
    if max_price is not None:
        constraints.append(f"Price must be ≤ ${max_price:.2f}")
    if not constraints:
        constraints.append("Pick the most affordable OR best-seller product")

    display = [
        {
            "index":     i,
            "title":     p.get("title", "")[:80],
            "brand":     p.get("brand", ""),
            "price":     p.get("price"),
            "badges":    p.get("badges", []),
            "sponsored": p.get("sponsored", False),
        }
        for i, p in enumerate(products)
    ]

    prompt = (
        f'Walmart.ca Canada search results for: "{item_name}"\n'
        f'Requirements: {"; ".join(constraints)}\n\n'
        f'Products:\n{json.dumps(display, indent=2)}\n\n'
        'Rules:\n'
        '1. If brand is required, only consider products whose title or brand field '
        'contains that brand name\n'
        '2. Exclude any product priced above the max price\n'
        '3. Among qualifying products prefer: '
        'best seller badge > popular badge > lowest price\n'
        '4. Avoid sponsored products unless they are the only option\n'
        '5. Reply with ONLY the integer index of the best product, '
        'or -1 if no product qualifies\n'
        '\nAnswer:'
    )

    try:
        resp = await _ai_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=8,
            messages=[{"role": "user", "content": prompt}],
        )
        idx = int(resp.content[0].text.strip().split()[0])
        if 0 <= idx < len(products):
            return idx
    except Exception as exc:
        logger.warning("AI product selection failed", extra={"error": str(exc)})

    return None


# ── WalmartAgent ───────────────────────────────────────────────────────────────

class WalmartAgent:
    """
    Headless Playwright agent that builds a Walmart.ca cart.

        async with WalmartAgent(postal_code="M5V3A1") as agent:
            result = await agent.build_cart(items, job_id="abc123")
            # result = {"cart_url": ..., "added": [...], "failed": [...], "screenshot": ...}
    """

    def __init__(self, postal_code: str = "", headless: bool = HEADLESS):
        self.postal_code = postal_code
        self.headless = headless
        self._pw = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None
        # Set by build_cart if a screenshot is taken on failure
        self.last_screenshot: str | None = None

    async def __aenter__(self) -> "WalmartAgent":
        self._pw = await async_playwright().start()
        self._browser = await self._pw.firefox.launch(
            headless=self.headless,
            # Firefox: different TLS fingerprint that Akamai doesn't block
            args=_STEALTH_ARGS,
        )
        ctx_kwargs: dict = {
            "viewport": {"width": 1280, "height": 900},
            "user_agent": _USER_AGENT,
            "locale": "en-CA",
            "timezone_id": "America/Toronto",
            "extra_http_headers": {
                "Accept-Language": "en-CA,en-US;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
            },
        }
        session = Path(SESSION_PATH)
        if session.exists():
            # Load session with Akamai/PerimeterX tracking cookies stripped
            # so each run starts with a fresh bot-detection identity.
            ctx_kwargs["storage_state"] = _clean_session(SESSION_PATH)
            logger.info("Loaded Walmart session (tracking cookies stripped)",
                        extra={"session": SESSION_PATH})

        self._context = await self._browser.new_context(**ctx_kwargs)
        await self._context.add_init_script(_STEALTH_SCRIPT)
        self.page = await self._context.new_page()
        return self

    async def __aexit__(self, *_) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    # ── Private helpers ────────────────────────────────────────────────────────

    async def _check(self) -> None:
        await _check_bot_challenge(self.page)

    async def _dismiss(self) -> None:
        await _dismiss_overlays(self.page)

    async def _safe_screenshot(self, job_id: str) -> str | None:
        """Capture current page to storage/screenshots/{job_id}.png. Never raises."""
        try:
            SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
            path = SCREENSHOT_DIR / f"{job_id}.png"
            await self.page.screenshot(path=str(path), full_page=False)
            logger.info("Screenshot saved", extra={"path": str(path), "job_id": job_id})
            return str(path)
        except Exception as exc:
            logger.warning("Screenshot failed", extra={"job_id": job_id, "error": str(exc)})
            return None

    async def _set_postal_code(self) -> None:
        if not self.postal_code:
            return
        try:
            location_btns = [
                '[data-automation-id="store-finder-link"]',
                'button[aria-label*="location"]',
                'button[aria-label*="store"]',
                'button:has-text("Find a store")',
                '[data-automation="store-selector"]',
            ]
            clicked = False
            for sel in location_btns:
                try:
                    el = self.page.locator(sel).first
                    if await el.is_visible(timeout=2000):
                        await _human_click(self.page, el)
                        clicked = True
                        await _delay(0.8, 1.5)
                        break
                except Exception:
                    pass

            if not clicked:
                logger.warning("Store/location button not found; skipping postal code")
                return

            postal_inputs = [
                'input[placeholder*="postal"]',
                'input[placeholder*="Postal"]',
                'input[name*="postal"]',
                'input[id*="postal"]',
                'input[type="text"]',
            ]
            for sel in postal_inputs:
                try:
                    el = self.page.locator(sel).first
                    if await el.is_visible(timeout=2000):
                        await _human_type(self.page, el, self.postal_code)
                        await _short_delay()
                        await self.page.keyboard.press("Enter")
                        await _delay(1.5, 2.5)
                        logger.info("Postal code set", extra={"postal_code": self.postal_code})
                        break
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("Could not set postal code", extra={"error": str(exc)})

    async def _try_add_from_page(self) -> bool:
        for sel in _ADD_TO_CART:
            btns = self.page.locator(sel)
            if await btns.count() > 0:
                try:
                    await _human_click(self.page, btns.first)
                    await _delay(1.2, 2.5)
                    await self._dismiss()
                    return True
                except Exception as exc:
                    logger.debug("Click failed", extra={"selector": sel, "error": str(exc)})
        return False

    async def _update_cart_qty(self, qty: int) -> None:
        try:
            await self.page.goto(
                f"{WALMART_BASE}/en/cart", wait_until="domcontentloaded", timeout=20000
            )
            await asyncio.sleep(1)
            for sel in [
                'input[aria-label*="Quantity"]',
                'input[aria-label*="quantity"]',
                'input[name="quantity"]',
                '[data-automation-id="quantity-input"]',
            ]:
                inputs = self.page.locator(sel)
                n = await inputs.count()
                if n > 0:
                    last = inputs.nth(n - 1)
                    if await last.input_value() != str(qty):
                        await last.triple_click()
                        await last.fill(str(qty))
                        await last.press("Enter")
                        await asyncio.sleep(1)
                    break
        except Exception as exc:
            logger.debug("Could not update qty", extra={"qty": qty, "error": str(exc)})

    # ── Public API ─────────────────────────────────────────────────────────────

    async def search_and_add(
        self,
        name: str,
        qty: int,
        brand: str | None = None,
        max_price: float | None = None,
    ) -> bool:
        """
        Search Walmart.ca, use AI to select the best matching product
        (respecting brand and price constraints), then add it to cart.
        """
        query = f"{brand} {name}".strip() if brand else name
        label = query

        try:
            encoded = query.replace(" ", "+")
            await self.page.goto(
                f"{WALMART_BASE}/en/search?q={encoded}",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await _delay(1.5, 3.0)
            await self._check()
            await _random_scroll(self.page)

            # ── AI-powered product selection ───────────────────────────────────
            products = await _extract_search_results(self.page)
            if products:
                selected_idx = await _ai_select_product(products, name, brand, max_price)
                if selected_idx is not None:
                    product = products[selected_idx]
                    product_url = product.get("url", "")
                    if product_url and not product_url.startswith("http"):
                        product_url = WALMART_BASE + product_url

                    logger.info(
                        "AI selected product",
                        extra={
                            "item":    label,
                            "chosen":  product.get("title", "")[:60],
                            "price":   product.get("price"),
                            "badges":  product.get("badges", []),
                        },
                    )

                    if product_url:
                        await _short_delay()
                        await self.page.goto(
                            product_url, wait_until="domcontentloaded", timeout=30000
                        )
                        await _delay(1.5, 2.8)
                        await self._check()
                        await _random_scroll(self.page)
                        if await self._try_add_from_page():
                            logger.info("Added AI-selected product", extra={"item": label})
                            if qty > 1:
                                await self._update_cart_qty(qty)
                            return True

            # ── Fallback 1: add-to-cart button visible on search results ───────
            if await self._try_add_from_page():
                logger.info("Added from search results (fallback)", extra={"item": label})
                if qty > 1:
                    await self._update_cart_qty(qty)
                return True

            # ── Fallback 2: open first product link ────────────────────────────
            for sel in [
                '[data-automation-id="product-title"] a',
                'a[data-automation-id="product-link"]',
                'a[href*="/en/ip/"]',
                'a[href*="/ip/"]',
            ]:
                links = self.page.locator(sel)
                if await links.count() > 0:
                    href = await links.first.get_attribute("href")
                    if href:
                        product_url = href if href.startswith("http") else WALMART_BASE + href
                        await _short_delay()
                        await self.page.goto(
                            product_url, wait_until="domcontentloaded", timeout=30000
                        )
                        await _delay(1.5, 2.8)
                        await self._check()
                        await _random_scroll(self.page)
                        if await self._try_add_from_page():
                            logger.info("Added from product page (fallback)", extra={"item": label})
                            if qty > 1:
                                await self._update_cart_qty(qty)
                            return True
                    break

            logger.warning("Could not add item", extra={"item": label})
            return False

        except BotChallengeError:
            raise
        except Exception as exc:
            logger.error("search_and_add error", extra={"item": label, "error": str(exc)})
            return False

    async def search_and_add_with_retry(
        self,
        name: str,
        qty: int,
        brand: str | None = None,
        max_price: float | None = None,
        max_attempts: int = 3,   # 1 initial + 2 retries
    ) -> bool:
        """Retry wrapper around search_and_add. Never retries BotChallengeError."""
        label = f"{brand} {name}".strip() if brand else name

        for attempt in range(1, max_attempts + 1):
            try:
                ok = await self.search_and_add(name=name, qty=qty, brand=brand, max_price=max_price)
                if ok:
                    return True
                if attempt < max_attempts:
                    logger.info(
                        "Item not added, retrying",
                        extra={"item": label, "attempt": attempt, "max": max_attempts},
                    )
                    await _delay(2.0, 4.0)
            except BotChallengeError:
                raise   # never retry; propagate immediately
            except Exception as exc:
                if attempt < max_attempts:
                    logger.warning(
                        "Attempt failed, retrying",
                        extra={"item": label, "attempt": attempt, "error": str(exc)},
                    )
                    await _delay(2.0, 4.0)
                else:
                    logger.error(
                        "All attempts failed",
                        extra={"item": label, "attempts": max_attempts, "error": str(exc)},
                    )

        return False

    async def build_cart(self, items: list[dict], job_id: str | None = None) -> dict:
        """
        Full flow: navigate to walmart.ca, set postal code, add all items, return cart URL.

        Returns:
            {
                "cart_url":  str,
                "added":     list[str],   # item labels successfully added
                "failed":    list[str],   # item labels that failed all retries
                "screenshot": str | None, # path if screenshot was taken
            }

        Raises BotChallengeError (screenshot taken before raising).
        """
        added: list[str] = []
        failed: list[str] = []

        try:
            logger.info("Starting cart build", extra={"job_id": job_id, "items": len(items)})
            # Warm-up: land on homepage first, pause like a human reading it
            await self.page.goto(WALMART_BASE, wait_until="domcontentloaded", timeout=30000)
            await _delay(2.0, 4.0)
            await self._check()
            # Brief random scroll on homepage before starting searches
            await _random_scroll(self.page)
            await self._set_postal_code()

            for item in items:
                label = (
                    f"{item['brand']} {item['name']}".strip()
                    if item.get("brand")
                    else item["name"]
                )
                ok = await self.search_and_add_with_retry(
                    name=item["name"],
                    qty=item.get("qty", 1),
                    brand=item.get("brand"),
                    max_price=item.get("max_price"),
                )
                if ok:
                    added.append(label)
                    logger.info("Item added", extra={"job_id": job_id, "item": label})
                else:
                    failed.append(label)
                    logger.warning("Item failed", extra={"job_id": job_id, "item": label})

            logger.info(
                "Cart build complete",
                extra={"job_id": job_id, "added": len(added), "failed": len(failed)},
            )

            await self.page.goto(
                f"{WALMART_BASE}/en/cart", wait_until="domcontentloaded", timeout=30000
            )
            await asyncio.sleep(1.5)
            await self._check()

            # Screenshot when at least one item failed
            screenshot = None
            if failed and job_id:
                screenshot = await self._safe_screenshot(job_id)
                self.last_screenshot = screenshot

            return {
                "cart_url":   self.page.url,
                "added":      added,
                "failed":     failed,
                "screenshot": screenshot,
            }

        except Exception as exc:
            # Take screenshot before the context manager closes the browser
            if job_id:
                self.last_screenshot = await self._safe_screenshot(job_id)
            raise


# ── Headful session refresh ────────────────────────────────────────────────────

async def refresh_session_headful(session_path: str = SESSION_PATH) -> bool:
    """
    Open a VISIBLE (headful) Firefox browser to obtain fresh Akamai-accepted
    cookies, then save the updated session to disk.

    Why this works:
        Headless Firefox has subtle fingerprint differences (canvas, WebGL,
        timing) that Akamai's JS probes detect.  A headful browser has a
        genuine fingerprint and passes the JS challenge automatically within
        a few seconds — no user interaction needed.

    Called automatically by the job worker on the first BotChallengeError
    before escalating to the user.

    Returns True if the session was refreshed and the site is accessible.
    """
    pw = None
    browser = None
    try:
        pw = await async_playwright().start()
        browser = await pw.firefox.launch(headless=False, args=_STEALTH_ARGS)

        ctx_kwargs: dict = {
            "viewport":   {"width": 1280, "height": 900},
            "user_agent": _USER_AGENT,
            "locale":     "en-CA",
            "timezone_id": "America/Toronto",
            "extra_http_headers": {"Accept-Language": "en-CA,en-US;q=0.9,en;q=0.8"},
        }
        # Keep Walmart auth cookies but strip all Akamai/PerimeterX trackers
        session_file = Path(session_path)
        if session_file.exists():
            ctx_kwargs["storage_state"] = _clean_session(session_path)

        context = await browser.new_context(**ctx_kwargs)
        await context.add_init_script(_STEALTH_SCRIPT)
        page = await context.new_page()

        # Load the homepage — headful Firefox auto-passes Akamai's JS challenge
        await page.goto(WALMART_BASE, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(7)   # let Akamai scripts run and issue fresh cookies

        # Navigate deeper to finalise the session
        await page.goto(f"{WALMART_BASE}/en", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(4)

        # Confirm we're not blocked
        url = page.url
        if any(x in url for x in ("blocked", "captcha", "denied")):
            logger.warning("Headful refresh still blocked", extra={"url": url})
            return False

        # Persist the fresh session (keeps auth + new Akamai cookies)
        dest = Path(session_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(dest))
        logger.info("Headful session refresh succeeded", extra={"path": session_path})
        return True

    except Exception as exc:
        logger.error("Headful session refresh failed", extra={"error": str(exc)})
        return False
    finally:
        try:
            if browser:
                await browser.close()
            if pw:
                await pw.stop()
        except Exception:
            pass


# ── WalmartLinker ──────────────────────────────────────────────────────────────

class WalmartLinker:
    """
    Headful (visible) browser session used exclusively for the /link command.
    No credentials are stored — the user logs in manually.

    Lifecycle:
        linker = WalmartLinker()
        await linker.start()          # opens visible browser at walmart.ca/signin
        # ... user logs in manually ...
        ok = await linker.is_logged_in()
        if ok:
            await linker.save_session("sessions/walmart_session.json")
        await linker.close()
    """

    def __init__(self) -> None:
        self._pw = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.firefox.launch(
            headless=False,
            args=_STEALTH_ARGS,
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=_USER_AGENT,
            locale="en-CA",
            timezone_id="America/Toronto",
            extra_http_headers={"Accept-Language": "en-CA,en;q=0.9"},
        )
        await self._context.add_init_script(_STEALTH_SCRIPT)
        self.page = await self._context.new_page()

        await self.page.goto(WALMART_BASE, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)
        await self.page.goto(
            f"{WALMART_BASE}/en/signin", wait_until="domcontentloaded", timeout=30000
        )
        await asyncio.sleep(1)

    async def is_logged_in(self) -> bool:
        """
        Returns True only if the browser shows clear evidence of an authenticated session.
        Never returns True based on URL alone — that caused guest sessions to be saved.
        """
        if self.page is None:
            return False
        try:
            url = self.page.url
            # Bail immediately on auth/block pages
            if any(x in url for x in ("signin", "login", "blocked", "captcha")):
                return False

            # Wait a moment for post-login JS to finish rendering the account UI
            await asyncio.sleep(1.5)

            # Check for account-menu DOM elements
            for sel in _LOGGED_IN_INDICATORS:
                try:
                    if await self.page.locator(sel).count() > 0:
                        return True
                except Exception:
                    pass

            # Fallback: check page source for server-rendered auth signals
            try:
                content = await self.page.content()
                auth_signals = [
                    "sign-out", "Sign out", "Sign Out",
                    '"isLoggedIn":true', '"isSignedIn":true',
                    "account-menu", "My Account",
                ]
                if any(sig in content for sig in auth_signals):
                    return True
            except Exception:
                pass

        except Exception:
            pass
        return False

    async def save_session(self, path: str) -> None:
        """
        Navigate to the account page first so all post-login cookies are set,
        then persist the full session to disk.
        """
        # Navigating to the account page forces Walmart's servers to issue
        # all auth cookies (some are set lazily after the login redirect).
        try:
            await self.page.goto(
                f"{WALMART_BASE}/en/account",
                wait_until="domcontentloaded",
                timeout=20000,
            )
            await asyncio.sleep(2)   # let cookie-setting JS finish
        except Exception as exc:
            logger.warning("Could not navigate to account page before save", extra={"error": str(exc)})

        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        await self._context.storage_state(path=str(dest))
        logger.info("Walmart session saved", extra={"path": path})

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        self._browser = None
        self._pw = None
        self.page = None


# ── WalmartResumeSession ───────────────────────────────────────────────────────

class WalmartResumeSession:
    """
    Headful browser for human-in-the-loop bot challenge resolution.

    Flow:
        session = WalmartResumeSession(postal_code="M5V3A1")
        await session.start()          # opens visible browser at walmart.ca
        # user solves CAPTCHA / verification manually in the browser window
        await session.save_session()   # persist updated cookies to SESSION_PATH
        await session.close()          # shut down browser
        # Re-run WalmartAgent headlessly — it loads the refreshed session file.
    """

    def __init__(self, postal_code: str = "") -> None:
        self.postal_code = postal_code
        self._pw = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.firefox.launch(
            headless=False,
            args=_STEALTH_ARGS,
        )
        ctx_kwargs: dict = {
            "viewport": {"width": 1280, "height": 900},
            "user_agent": _USER_AGENT,
            "locale": "en-CA",
            "timezone_id": "America/Toronto",
            "extra_http_headers": {"Accept-Language": "en-CA,en-US;q=0.9,en;q=0.8"},
        }
        session_file = Path(SESSION_PATH)
        if session_file.exists():
            ctx_kwargs["storage_state"] = _clean_session(SESSION_PATH)
            logger.info("Loaded session for resume (tracking cookies stripped)",
                        extra={"path": SESSION_PATH})

        self._context = await self._browser.new_context(**ctx_kwargs)
        await self._context.add_init_script(_STEALTH_SCRIPT)
        self.page = await self._context.new_page()
        await self.page.goto(WALMART_BASE, wait_until="domcontentloaded", timeout=30000)
        logger.info("WalmartResumeSession browser opened")

    async def save_session(self) -> None:
        if self._context is None:
            return
        dest = Path(SESSION_PATH)
        dest.parent.mkdir(parents=True, exist_ok=True)
        await self._context.storage_state(path=str(dest))
        logger.info("Resume session saved", extra={"path": SESSION_PATH})

    async def close(self) -> None:
        try:
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._browser = None
        self._pw = None
        self.page = None
