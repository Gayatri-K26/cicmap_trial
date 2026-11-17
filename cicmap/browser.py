"""Headless-browser fetching (Playwright + Chromium) for pages behind JavaScript bot checks.

The browser is only started when a plain request is blocked. After it loads a page, its cookies
(e.g. AWS WAF's aws-waf-token) are handed back to the HTTP client, so later requests to the same
site go through fast plain HTTP again.
"""
import logging
from typing import Dict, Optional, Tuple

from . import config

log = logging.getLogger("cicmap")


class BrowserUnavailable(Exception):
    pass


class BrowserSession:
    def __init__(self, headless: bool = True):
        self.headless = headless
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    def _ensure(self):
        if self._page is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise BrowserUnavailable("Playwright is not installed: pip install playwright && "
                                     "python -m playwright install chromium") from e
        log.info("starting headless browser")
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        self._context = self._browser.new_context(user_agent=config.USER_AGENT, locale="en-US")
        self._page = self._context.new_page()

    def get(self, url: str, settle_ms: int = 1500) -> Tuple[str, int, Dict[str, Dict[str, str]]]:
        """Load a page. Returns (html, status, cookies grouped by domain)."""
        self._ensure()
        resp = self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            self._page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass  # pages with endless analytics requests never go idle; the content is there anyway
        self._page.wait_for_timeout(settle_ms)
        html = self._page.content()
        status = resp.status if resp else 0
        cookies: Dict[str, Dict[str, str]] = {}
        for c in self._context.cookies():
            cookies.setdefault(c["domain"].lstrip("."), {})[c["name"]] = c["value"]
        return html, status, cookies

    def close(self) -> None:
        for obj, method in ((self._context, "close"), (self._browser, "close"), (self._pw, "stop")):
            if obj is not None:
                try:
                    getattr(obj, method)()
                except Exception:
                    pass
        self._pw = self._browser = self._context = self._page = None


def cookies_for_host(cookies: Dict[str, Dict[str, str]], host: str) -> Optional[Dict[str, str]]:
    out: Dict[str, str] = {}
    for domain, jar in cookies.items():
        if host == domain or host.endswith("." + domain):
            out.update(jar)
    return out or None
