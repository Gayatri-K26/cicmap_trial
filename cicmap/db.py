"""SQLite connection and schema setup."""
import sqlite3
from pathlib import Path
from typing import Optional

from . import config

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    path = Path(path or config.DB_PATH)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    _migrate(conn)
    conn.executescript(SCHEMA_PATH.read_text())
    return conn


SYLLABUS_TABLES = ("syllabus_outcomes", "syllabus_topics", "syllabus_assessments", "syllabus_materials", "syllabi")


def _migrate(conn: sqlite3.Connection) -> None:
    """Syllabi used to hang off one course (syllabi.course_id); now documents link to courses through
    syllabus_courses. The old tables were never filled, so they are dropped and recreated by the schema."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(syllabi)")}
    if "course_id" not in cols:
        return
    if conn.execute("SELECT COUNT(*) FROM syllabi").fetchone()[0]:
        raise RuntimeError("Old-style syllabi table has rows; migrate it by hand before upgrading.")
    for t in SYLLABUS_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit()


def get_state(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO sync_state(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_or_create_institution(conn: sqlite3.Connection, name: str, domain: Optional[str] = None) -> int:
    row = conn.execute("SELECT id FROM institutions WHERE name = ?", (name,)).fetchone()
    if row:
        if domain:
            conn.execute("UPDATE institutions SET domain = COALESCE(domain, ?) WHERE id = ?", (domain, row["id"]))
        return row["id"]
    return conn.execute("INSERT INTO institutions(name, domain) VALUES (?, ?)", (name, domain)).lastrowid
