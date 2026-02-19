"""
Walmart.ca browser automation via Playwright.

Classes:
    WalmartAgent   – headless cart-building worker (used by job worker)
    WalmartLinker  – headful manual-login helper (used by /link command)
"""

import asyncio
import logging
import os
import random
from pathlib import Path

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PWTimeout,
)

logger = logging.getLogger(__name__)

SESSION_PATH: str = os.getenv("SESSION_PATH", "sessions/walmart_session.json")
HEADLESS: bool = os.getenv("HEADLESS", "true").lower() == "true"
WALMART_BASE = "https://www.walmart.ca"
SCREENSHOT_DIR = Path("storage/screenshots")

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
            ctx_kwargs["storage_state"] = str(session)
            logger.info("Loaded Walmart session", extra={"session": SESSION_PATH})

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
        """Single attempt to search and add one item. Returns True on success."""
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

            # Human-like: scroll through results before adding
            await _random_scroll(self.page)

            if await self._try_add_from_page():
                logger.info("Added from search results", extra={"item": label})
                if qty > 1:
                    await self._update_cart_qty(qty)
                return True

            # Fall back: open first product link
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
                            logger.info("Added from product page", extra={"item": label})
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
        if self.page is None:
            return False
        try:
            url = self.page.url
            if "signin" in url or "login" in url:
                return False
            for sel in _LOGGED_IN_INDICATORS:
                try:
                    if await self.page.locator(sel).count() > 0:
                        return True
                except Exception:
                    pass
            if "walmart.ca" in url:
                return True
        except Exception:
            pass
        return False

    async def save_session(self, path: str) -> None:
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
            ctx_kwargs["storage_state"] = str(session_file)
            logger.info("Loaded session for resume", extra={"path": SESSION_PATH})

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
