"""Page fetching with a database cache and two tiers.

Tier 1: plain HTTP. Tier 2: a headless browser, used only when tier 1 is blocked (bot checks such as
Acalog's empty 202 + AWS WAF challenge, or Cloudflare). After the browser passes a site's check, its
cookies are copied into the HTTP client so the rest of that site's pages are fetched over plain HTTP.
"""
import hashlib
import logging
import time
from typing import Dict, Optional
from urllib.parse import urlparse

import httpx

from . import config
from .browser import BrowserSession, BrowserUnavailable, cookies_for_host

log = logging.getLogger("cicmap")

BLOCK_MARKERS = ("just a moment...", "cf-chl", "challenge-platform", "attention required! | cloudflare",
                 "awswafintegration", "aws-waf-token", "gokuprops")


class BlockedError(Exception):
    pass


class NotFoundError(Exception):
    """The page does not exist (404/410). Not a bot check, so the browser is not tried."""


class Fetcher:
    def __init__(self, conn, refresh: bool = False, use_browser: bool = True,
                 browser: Optional[BrowserSession] = None):
        self.conn = conn
        self.refresh = refresh
        self.use_browser = use_browser
        self.pages_fetched = 0
        self.browser_fetches = 0
        self._browser = browser
        self._owns_browser = browser is None
        self._browser_hosts = set()   # hosts known to need the browser's cookies
        self._last_hit: Dict[str, float] = {}
        self._client = httpx.Client(
            follow_redirects=True, timeout=30,
            headers={"User-Agent": config.USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*",
                     "Accept-Language": "en-US,en;q=0.9"},
        )

    def cached(self, url: str) -> Optional[str]:
        row = self.conn.execute("SELECT body FROM raw_pages WHERE url = ? AND status_code < 400", (url,)).fetchone()
        if row and not looks_blocked(row["body"]):
            return row["body"]
        return None

    def get(self, url: str) -> str:
        if not self.refresh:
            body = self.cached(url)
            if body is not None:
                return body

        body, status = self._http(url)
        if status < 400 and not looks_blocked(body):
            self._store(url, status, body, "http+cookies" if urlparse(url).netloc in self._browser_hosts else "http")
            return body
        if status in (404, 410):
            self._store(url, status, body, "http")
            raise NotFoundError(f"HTTP {status}: {url}")
        if not self.use_browser:
            self._store(url, status, body, "http")
            raise BlockedError(f"HTTP {status} ({len(body)} bytes), bot check; browser fetch disabled: {url}")

        body, status = self._via_browser(url)
        self._store(url, status, body, "browser")
        if status < 400 and not looks_blocked(body):
            return body
        raise BlockedError(f"Blocked even in the headless browser (HTTP {status}): {url}")

    def _http(self, url: str):
        host = urlparse(url).netloc
        wait = config.REQUEST_DELAY_SECONDS - (time.time() - self._last_hit.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        self._last_hit[host] = time.time()
        resp = self._client.get(url)
        self.pages_fetched += 1
        return resp.text, resp.status_code

    def _via_browser(self, url: str):
        if self._browser is None:
            self._browser = BrowserSession()
        try:
            html, status, cookies = self._browser.get(url)
        except BrowserUnavailable as e:
            raise BlockedError(f"{e} ({url})") from e
        self.pages_fetched += 1
        self.browser_fetches += 1
        host = urlparse(url).netloc
        jar = cookies_for_host(cookies, host)
        if jar:
            for name, value in jar.items():
                self._client.cookies.set(name, value, domain=host)
            self._browser_hosts.add(host)
            log.info("browser passed the check on %s; reusing %d cookies over HTTP", host, len(jar))
        return html, status

    def _store(self, url: str, status: int, body: str, method: str) -> None:
        self.conn.execute(
            "INSERT INTO raw_pages(url, status_code, content_hash, body, fetch_method, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, datetime('now')) ON CONFLICT(url) DO UPDATE SET "
            "status_code=excluded.status_code, content_hash=excluded.content_hash, body=excluded.body, "
            "fetch_method=excluded.fetch_method, fetched_at=excluded.fetched_at",
            (url, status, hashlib.sha256(body.encode()).hexdigest(), body, method),
        )
        self.conn.commit()

    def close(self) -> None:
        self._client.close()
        if self._browser is not None and self._owns_browser:
            self._browser.close()
            self._browser = None


def looks_blocked(body: str) -> bool:
    if len(body.strip()) < 200:
        return True
    head = body[:6000].lower()
    return any(m in head for m in BLOCK_MARKERS)
