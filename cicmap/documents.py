"""Fetch a web page, PDF or Word file as plain text, for syllabus documents found by search.

Checks robots.txt first, waits between requests to the same site, and caches the extracted text in
raw_pages (fetch_method 'text:<html|pdf|docx>'). No headless browser: a page behind a bot check is skipped.
"""
import io
import logging
import time
import warnings
from typing import Dict, Tuple
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

from . import config
from .fetch import BlockedError, looks_blocked, robots_allowed

logging.getLogger("pypdf").setLevel(logging.ERROR)  # malformed-but-readable PDFs log a warning per object

MAX_BYTES = 15_000_000
MAX_PDF_PAGES = 40  # syllabi are short; long PDFs are catalogs or reports
_last_hit: Dict[str, float] = {}


class DocumentError(Exception):
    pass


def fetch_text(conn, url: str) -> Tuple[str, str]:
    """(text, kind) for a URL, where kind is 'html', 'pdf' or 'docx'."""
    row = conn.execute("SELECT body, fetch_method FROM raw_pages WHERE url = ? AND fetch_method LIKE 'text:%'",
                       (url,)).fetchone()
    if row:
        return row["body"], row["fetch_method"].split(":", 1)[1]
    if not robots_allowed(url):
        raise BlockedError(f"robots.txt disallows {url}")
    host = urlparse(url).netloc
    wait = config.REQUEST_DELAY_SECONDS - (time.time() - _last_hit.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    _last_hit[host] = time.time()
    resp = httpx.get(url, follow_redirects=True, timeout=30, headers={"User-Agent": config.USER_AGENT})
    if resp.status_code >= 400:
        raise DocumentError(f"HTTP {resp.status_code}: {url}")
    if len(resp.content) > MAX_BYTES:
        raise DocumentError(f"too large ({len(resp.content)} bytes): {url}")
    ctype = resp.headers.get("content-type", "").lower()
    path = urlparse(str(resp.url)).path.lower()
    if "pdf" in ctype or path.endswith(".pdf") or resp.content[:5] == b"%PDF-":
        text, kind = pdf_text(resp.content), "pdf"
    elif "wordprocessingml" in ctype or path.endswith(".docx"):
        text, kind = docx_text(resp.content), "docx"
    else:
        if looks_blocked(resp.text):
            raise BlockedError(f"bot check or empty page: {url}")
        text, kind = html_text(resp.text), "html"
    conn.execute("INSERT OR REPLACE INTO raw_pages(url, status_code, body, fetch_method, fetched_at) "
                 "VALUES (?, ?, ?, ?, datetime('now'))", (url, resp.status_code, text, f"text:{kind}"))
    conn.commit()
    return text, kind


def html_text(html: str) -> str:
    with warnings.catch_warnings():  # some pages are XHTML/XML; the HTML parser still reads their text
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        tag.decompose()
    lines = (line.strip() for line in soup.get_text("\n").split("\n"))
    return "\n".join(line for line in lines if line)


def pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(data))
        if len(reader.pages) > MAX_PDF_PAGES:
            raise DocumentError(f"{len(reader.pages)} pages: too long for a syllabus")
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except DocumentError:
        raise
    except Exception as e:  # malformed or encrypted PDF
        raise DocumentError(f"unreadable PDF: {e}") from e


def docx_text(data: bytes) -> str:
    import docx
    try:
        d = docx.Document(io.BytesIO(data))
    except Exception as e:
        raise DocumentError(f"unreadable Word file: {e}") from e
    parts = [p.text for p in d.paragraphs]
    for table in d.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text.strip() for cell in row.cells))
    return "\n".join(p for p in parts if p.strip())
