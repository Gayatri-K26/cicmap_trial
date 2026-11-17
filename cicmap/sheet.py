"""Sync the program list from the public Google Sheet.

Cheap by design:
  * normal commands read from the database, never the sheet;
  * a sync is skipped if the last one is newer than SHEET_TTL_HOURS (unless forced);
  * if the downloaded CSV hashes the same as last time, nothing else happens;
  * otherwise rows are diffed by catalog URL + row hash, and only new/changed rows are queued.
"""
import csv
import hashlib
import io
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import httpx

from . import config, db

COL_INSTITUTION = "Institution"
COL_PROGRAM = "Program"
COL_URL = "Latest Catalog Link"


@dataclass
class SyncResult:
    skipped_reason: Optional[str] = None
    added: List[str] = field(default_factory=list)
    changed: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    reactivated: List[str] = field(default_factory=list)
    unchanged: int = 0

    def summary(self) -> str:
        if self.skipped_reason:
            return f"Skipped: {self.skipped_reason}"
        return (f"{len(self.added)} added, {len(self.changed)} changed, {len(self.removed)} removed, "
                f"{len(self.reactivated)} reactivated, {self.unchanged} unchanged")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def parse_rows(csv_text: str) -> List[Dict[str, str]]:
    """Parse the sheet CSV. The csv module handles Google's \\r\\n line endings and quoted commas."""
    rows = []
    for r in csv.DictReader(io.StringIO(csv_text)):
        r = {(k or "").strip(): (v or "").strip() for k, v in r.items()}
        if r.get(COL_URL) and r.get(COL_INSTITUTION):
            rows.append(r)
    return rows


def row_hash(r: Dict[str, str]) -> str:
    key = "\x1f".join([r.get(COL_INSTITUTION, ""), r.get(COL_PROGRAM, ""), r.get(COL_URL, "")])
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def download_csv(url: str = config.SHEET_CSV_URL) -> str:
    resp = httpx.get(url, follow_redirects=True, timeout=30, headers={"User-Agent": config.USER_AGENT})
    resp.raise_for_status()
    return resp.text


def apply_rows(conn, rows: List[Dict[str, str]]) -> SyncResult:
    """Diff sheet rows against program_sources (origin='sheet') and apply the changes."""
    res = SyncResult()
    existing = {r["catalog_url"]: r for r in conn.execute(
        "SELECT * FROM program_sources WHERE origin = 'sheet'")}
    seen = set()
    for r in rows:
        url, h = r[COL_URL], row_hash(r)
        label = f"{r[COL_INSTITUTION]} - {r.get(COL_PROGRAM, '')}"
        seen.add(url)
        old = existing.get(url)
        if old is None:
            # A manual entry with the same URL becomes sheet-managed.
            manual = conn.execute("SELECT id FROM program_sources WHERE catalog_url = ?", (url,)).fetchone()
            if manual:
                conn.execute("UPDATE program_sources SET origin='sheet', row_hash=?, institution=?, program=?, "
                             "active=1, updated_at=datetime('now') WHERE id=?",
                             (h, r[COL_INSTITUTION], r.get(COL_PROGRAM, ""), manual["id"]))
                res.changed.append(label)
            else:
                conn.execute("INSERT INTO program_sources(institution, program, catalog_url, origin, row_hash) "
                             "VALUES (?, ?, ?, 'sheet', ?)", (r[COL_INSTITUTION], r.get(COL_PROGRAM, ""), url, h))
                res.added.append(label)
        elif old["row_hash"] != h:
            conn.execute("UPDATE program_sources SET institution=?, program=?, row_hash=?, active=1, "
                         "status='pending', updated_at=datetime('now') WHERE id=?",
                         (r[COL_INSTITUTION], r.get(COL_PROGRAM, ""), h, old["id"]))
            res.changed.append(label)
        elif not old["active"]:
            conn.execute("UPDATE program_sources SET active=1, updated_at=datetime('now') WHERE id=?", (old["id"],))
            res.reactivated.append(label)
        else:
            res.unchanged += 1
    for url, old in existing.items():
        if url not in seen and old["active"]:
            conn.execute("UPDATE program_sources SET active=0, updated_at=datetime('now') WHERE id=?", (old["id"],))
            res.removed.append(f"{old['institution']} - {old['program']}")
    return res


def sync(conn, force: bool = False, csv_text: Optional[str] = None) -> SyncResult:
    last = db.get_state(conn, "sheet_last_sync")
    if not force and csv_text is None and last:
        age = _now() - datetime.fromisoformat(last)
        if age < timedelta(hours=config.SHEET_TTL_HOURS):
            mins = int(age.total_seconds() // 60)
            return SyncResult(skipped_reason=f"last sync was {mins} min ago (TTL {config.SHEET_TTL_HOURS:g}h); use --force")

    text = csv_text if csv_text is not None else download_csv()
    file_hash = hashlib.sha256(text.encode()).hexdigest()
    db.set_state(conn, "sheet_last_sync", _now().isoformat())
    if file_hash == db.get_state(conn, "sheet_hash"):
        conn.commit()
        n = conn.execute("SELECT COUNT(*) FROM program_sources WHERE origin='sheet' AND active=1").fetchone()[0]
        return SyncResult(unchanged=n)

    res = apply_rows(conn, parse_rows(text))
    db.set_state(conn, "sheet_hash", file_hash)
    conn.commit()
    return res
