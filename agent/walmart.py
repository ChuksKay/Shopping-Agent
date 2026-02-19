"""
Walmart.ca browser automation via Playwright.

Classes:
    WalmartAgent   – headless cart-building worker (used by job worker)
    WalmartLinker  – headful manual-login helper (used by /link command)
"""

import asyncio
import logging
import os
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

# Use a real, recent Chrome UA — no "HeadlessChrome" in the string
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# Launch flags that strip Playwright's automation signals
_STEALTH_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-blink-features=AutomationControlled",  # removes navigator.webdriver
    "--disable-infobars",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-features=IsolateOrigins,site-per-process",
    "--disable-extensions",
]

# JS injected before every page load to mask remaining automation fingerprints
_STEALTH_SCRIPT = """
// Remove the main automation flag
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// Mock a real Chrome runtime
window.chrome = {
    runtime: {},
    loadTimes: function() {},
    csi: function() {},
    app: {}
};

// Realistic plugin list
Object.defineProperty(navigator, 'plugins', {
    get: () => {
        const arr = [
            { name: 'Chrome PDF Plugin',   filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
            { name: 'Chrome PDF Viewer',   filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
            { name: 'Native Client',       filename: 'internal-nacl-plugin', description: '' },
        ];
        arr.item   = i => arr[i];
        arr.namedItem = n => arr.find(p => p.name === n) || null;
        arr.refresh = () => {};
        return arr;
    }
});

// Languages consistent with locale
Object.defineProperty(navigator, 'languages', { get: () => ['en-CA', 'en-US', 'en'] });

// Platform
Object.defineProperty(navigator, 'platform', { get: () => 'MacIntel' });

// Permissions — prevent detection via permission probe
const _origPermQuery = window.navigator.permissions.query.bind(navigator.permissions);
window.navigator.permissions.query = params =>
    params.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : _origPermQuery(params);

// WebGL — mask Mesa/SwiftShader (headless tell-tale)
const _getParam = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(p) {
    if (p === 37445) return 'Intel Inc.';
    if (p === 37446) return 'Intel Iris OpenGL Engine';
    return _getParam.call(this, p);
};
"""

# Selectors for "Add to cart" — tried in order
_ADD_TO_CART = [
    '[data-automation-id="add-to-cart-btn"]',
    'button[aria-label*="Add to cart"]',
    'button[aria-label*="add to cart"]',
    'button:has-text("Add to cart")',
    'button:has-text("Add to Cart")',
]

# Selectors that indicate a successful login
_LOGGED_IN_INDICATORS = [
    '[data-automation-id="account-menu"]',
    'a[href*="/account/"]',
    'a[href*="sign-out"]',
    'a[href*="logout"]',
    'button[aria-label*="My Account"]',
]


class BotChallengeError(Exception):
    """Raised when Walmart presents a bot-detection or CAPTCHA page."""


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
        "access denied",
        "robot check",
        "captcha",
        "unusual traffic",
        "verify you are human",
        "checking your browser",
        "ddos-guard",
        "please enable cookies",
        "sorry, you have been blocked",
        "403 forbidden",
        "just a moment",
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
                await el.click()
                await asyncio.sleep(0.4)
        except Exception:
            pass


def _save_session_sync(context: BrowserContext, path: str) -> None:
    """Convenience wrapper — call via await context.storage_state(path=path)."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)


# ── WalmartAgent ───────────────────────────────────────────────────────────────

class WalmartAgent:
    """
    Headless Playwright agent that builds a Walmart.ca cart.
    Use as an async context manager:

        async with WalmartAgent(postal_code="M5V3A1") as agent:
            cart_url = await agent.build_cart([("milk", 2), ("eggs", 1)])
    """

    def __init__(self, postal_code: str = "", headless: bool = HEADLESS):
        self.postal_code = postal_code
        self.headless = headless
        self._pw = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None

    async def __aenter__(self) -> "WalmartAgent":
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=self.headless,
            args=_STEALTH_ARGS,
        )
        ctx_kwargs: dict = {
            "viewport": {"width": 1280, "height": 900},
            "user_agent": _USER_AGENT,
            "locale": "en-CA",
            "timezone_id": "America/Toronto",
            "extra_http_headers": {"Accept-Language": "en-CA,en;q=0.9"},
        }
        session = Path(SESSION_PATH)
        if session.exists():
            ctx_kwargs["storage_state"] = str(session)
            logger.info("Loaded Walmart session from %s", SESSION_PATH)

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
                        await el.click()
                        clicked = True
                        await asyncio.sleep(1)
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
                        await el.fill(self.postal_code)
                        await asyncio.sleep(0.3)
                        await el.press("Enter")
                        await asyncio.sleep(2)
                        logger.info("Postal code set: %s", self.postal_code)
                        break
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("Could not set postal code: %s", exc)

    async def _try_add_from_page(self) -> bool:
        """Attempt to click the first visible 'Add to cart' button on current page."""
        for sel in _ADD_TO_CART:
            btns = self.page.locator(sel)
            if await btns.count() > 0:
                try:
                    await btns.first.scroll_into_view_if_needed()
                    await btns.first.click(timeout=5000)
                    await asyncio.sleep(1.5)
                    await self._dismiss()
                    return True
                except Exception as exc:
                    logger.debug("Click failed for %s: %s", sel, exc)
        return False

    async def _update_cart_qty(self, qty: int) -> None:
        """Best-effort: navigate to cart and update qty of last item."""
        try:
            await self.page.goto(
                f"{WALMART_BASE}/en/cart",
                wait_until="domcontentloaded",
                timeout=20000,
            )
            await asyncio.sleep(1)
            qty_sels = [
                'input[aria-label*="Quantity"]',
                'input[aria-label*="quantity"]',
                'input[name="quantity"]',
                '[data-automation-id="quantity-input"]',
            ]
            for sel in qty_sels:
                inputs = self.page.locator(sel)
                n = await inputs.count()
                if n > 0:
                    last = inputs.nth(n - 1)
                    current = await last.input_value()
                    if current != str(qty):
                        await last.triple_click()
                        await last.fill(str(qty))
                        await last.press("Enter")
                        await asyncio.sleep(1)
                    break
        except Exception as exc:
            logger.debug("Could not update qty: %s", exc)

    # ── Public API ─────────────────────────────────────────────────────────────

    async def search_and_add(self, item: str, qty: int) -> bool:
        """Search for *item* on walmart.ca and add it to cart. Returns True on success."""
        try:
            encoded = item.replace(" ", "+")
            await self.page.goto(
                f"{WALMART_BASE}/en/search?q={encoded}",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await asyncio.sleep(1.5)
            await self._check()

            # Try adding directly from search results
            if await self._try_add_from_page():
                logger.info("Added '%s' from search results", item)
                if qty > 1:
                    await self._update_cart_qty(qty)
                return True

            # Fall back: open first product link
            product_sels = [
                '[data-automation-id="product-title"] a',
                'a[data-automation-id="product-link"]',
                'a[href*="/en/ip/"]',
                'a[href*="/ip/"]',
            ]
            for sel in product_sels:
                links = self.page.locator(sel)
                if await links.count() > 0:
                    href = await links.first.get_attribute("href")
                    if href:
                        product_url = (
                            href if href.startswith("http") else WALMART_BASE + href
                        )
                        await self.page.goto(
                            product_url,
                            wait_until="domcontentloaded",
                            timeout=30000,
                        )
                        await asyncio.sleep(1.5)
                        await self._check()
                        if await self._try_add_from_page():
                            logger.info("Added '%s' from product page", item)
                            if qty > 1:
                                await self._update_cart_qty(qty)
                            return True
                    break

            logger.warning("Could not add '%s' to cart", item)
            return False

        except BotChallengeError:
            raise
        except Exception as exc:
            logger.error("Error processing item '%s': %s", item, exc, exc_info=True)
            return False

    async def build_cart(self, items: list[tuple[str, int]]) -> str:
        """
        Full flow: load walmart.ca, set postal code, add all items, return cart URL.
        Raises BotChallengeError if Walmart presents a bot challenge.
        """
        logger.info("Building cart for %d item(s)", len(items))
        await self.page.goto(WALMART_BASE, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(1.5)
        await self._check()
        await self._set_postal_code()

        ok = 0
        for text, qty in items:
            try:
                if await self.search_and_add(text, qty):
                    ok += 1
            except BotChallengeError:
                raise

        logger.info("Added %d/%d items", ok, len(items))

        # Navigate to cart and return its URL
        await self.page.goto(
            f"{WALMART_BASE}/en/cart",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await asyncio.sleep(1.5)
        await self._check()
        return self.page.url


# ── WalmartLinker ──────────────────────────────────────────────────────────────

class WalmartLinker:
    """
    Headful (visible) browser session used exclusively for the /link command.

    The user manually logs into Walmart.ca; we then save the storage state.
    No credentials are ever stored.

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
        self._browser = await self._pw.chromium.launch(
            headless=False,   # must be visible — user interacts with it
            args=_STEALTH_ARGS,
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=_USER_AGENT,
            locale="en-CA",
            timezone_id="America/Toronto",
            extra_http_headers={"Accept-Language": "en-CA,en;q=0.9"},
        )
        # Inject stealth script before every page load
        await self._context.add_init_script(_STEALTH_SCRIPT)
        self.page = await self._context.new_page()

        # Go to homepage first (less suspicious than jumping straight to /signin)
        await self.page.goto(WALMART_BASE, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)

        # Now navigate to sign-in
        await self.page.goto(
            f"{WALMART_BASE}/en/signin",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await asyncio.sleep(1)

    async def is_logged_in(self) -> bool:
        """Return True if the browser's current page looks like an authenticated session."""
        if self.page is None:
            return False
        try:
            url = self.page.url
            # Still on the sign-in page → not logged in
            if "signin" in url or "login" in url:
                return False
            # Check for elements that only appear when authenticated
            for sel in _LOGGED_IN_INDICATORS:
                try:
                    if await self.page.locator(sel).count() > 0:
                        return True
                except Exception:
                    pass
            # If we navigated away from signin, optimistically treat as logged in
            if "walmart.ca" in url:
                return True
        except Exception:
            pass
        return False

    async def save_session(self, path: str) -> None:
        """Persist browser storage state (cookies + localStorage) to *path*."""
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        await self._context.storage_state(path=str(dest))
        logger.info("Walmart session saved to %s", path)

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        self._browser = None
        self._pw = None
        self.page = None
