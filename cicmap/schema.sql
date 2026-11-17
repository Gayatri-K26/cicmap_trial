-- cicmap database schema (SQLite).
-- Layers: sources (what to extract) -> raw cache -> programs / courses / prereqs -> syllabi.

PRAGMA foreign_keys = ON;

-- Key/value state, e.g. the last sheet sync time and hash.
CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per program to extract. Fed by the Google Sheet (origin='sheet') or added by hand (origin='manual').
CREATE TABLE IF NOT EXISTS program_sources (
    id                INTEGER PRIMARY KEY,
    institution       TEXT NOT NULL,
    program           TEXT NOT NULL,
    catalog_url       TEXT NOT NULL UNIQUE,
    origin            TEXT NOT NULL DEFAULT 'sheet' CHECK (origin IN ('sheet', 'manual')),
    row_hash          TEXT,                       -- hash of the sheet row, to detect edits
    active            INTEGER NOT NULL DEFAULT 1, -- 0 = removed from the sheet (kept, not deleted)
    status            TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'extracted', 'failed', 'needs_parser', 'blocked')),
    adapter_override  TEXT,                       -- force a specific parser for this program
    last_error        TEXT,
    first_seen_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now')),
    last_extracted_at TEXT
);

-- Raw fetched pages. Lets us re-parse without re-fetching, and backs the "cached" UI views.
CREATE TABLE IF NOT EXISTS raw_pages (
    url          TEXT PRIMARY KEY,
    status_code  INTEGER,
    content_hash TEXT,
    body         TEXT,
    fetch_method TEXT,          -- 'http' or 'browser'
    fetched_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Cached LLM responses, keyed by a hash of (model, prompt version, prompt). Re-runs cost nothing.
CREATE TABLE IF NOT EXISTS llm_cache (
    key        TEXT PRIMARY KEY,
    purpose    TEXT,
    model      TEXT,
    response   TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS institutions (
    id     INTEGER PRIMARY KEY,
    name   TEXT NOT NULL UNIQUE,
    domain TEXT
);

-- Program metadata.
CREATE TABLE IF NOT EXISTS programs (
    id              INTEGER PRIMARY KEY,
    source_id       INTEGER NOT NULL UNIQUE REFERENCES program_sources(id) ON DELETE CASCADE,
    institution_id  INTEGER NOT NULL REFERENCES institutions(id),
    name            TEXT NOT NULL,
    degree          TEXT,          -- e.g. 'BS'
    degree_full     TEXT,          -- e.g. 'Bachelor of Science'
    college         TEXT,
    department      TEXT,
    catalog_year    TEXT,
    total_credits   TEXT,
    description     TEXT,
    source_url      TEXT NOT NULL,
    adapter         TEXT,
    adapter_version INTEGER,
    extracted_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Courses, unique per institution + code. AI courses are fully populated;
-- non-AI courses appear only as stubs when an AI course lists them as a prerequisite.
CREATE TABLE IF NOT EXISTS courses (
    id              INTEGER PRIMARY KEY,
    institution_id  INTEGER NOT NULL REFERENCES institutions(id),
    code            TEXT NOT NULL,     -- normalized, e.g. 'CS 380'
    subject         TEXT,
    number          TEXT,
    title           TEXT,
    credits_min     REAL,
    credits_max     REAL,
    credits_text    TEXT,
    description     TEXT,
    prereq_text     TEXT,
    coreq_text      TEXT,
    restrictions    TEXT,
    extra_json      TEXT,              -- any other labeled fields the catalog gives (repeat status, offered terms, ...)
    source_url      TEXT,
    is_ai           INTEGER NOT NULL DEFAULT 0,
    is_stub         INTEGER NOT NULL DEFAULT 0,
    ai_confidence   REAL,
    ai_category     TEXT,
    ai_reason       TEXT,
    ai_method       TEXT,              -- 'keyword' or 'llm'
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (institution_id, code)
);

-- Which AI courses belong to which program, and where they sit in the curriculum.
CREATE TABLE IF NOT EXISTS program_courses (
    program_id        INTEGER NOT NULL REFERENCES programs(id) ON DELETE CASCADE,
    course_id         INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    requirement_group TEXT,            -- e.g. 'Core Requirements', 'AI Electives'
    is_alternative    INTEGER NOT NULL DEFAULT 0,  -- listed as an "or" option
    PRIMARY KEY (program_id, course_id)
);

-- Prerequisites as a queryable graph, in AND-of-OR form:
-- rows sharing (course_id, kind, group_no) are alternatives (OR); different group_no values are all required (AND).
-- A row with prereq_course_id NULL is a non-course condition (e.g. 'junior standing').
CREATE TABLE IF NOT EXISTS course_prereqs (
    id               INTEGER PRIMARY KEY,
    course_id        INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    kind             TEXT NOT NULL DEFAULT 'prereq' CHECK (kind IN ('prereq', 'coreq')),
    group_no         INTEGER NOT NULL,
    prereq_course_id INTEGER REFERENCES courses(id) ON DELETE CASCADE,
    condition_text   TEXT,
    min_grade        TEXT,
    concurrent_ok    INTEGER NOT NULL DEFAULT 0,
    parse_method     TEXT              -- 'llm' or 'regex'
);
CREATE INDEX IF NOT EXISTS idx_prereqs_course ON course_prereqs(course_id);
CREATE INDEX IF NOT EXISTS idx_prereqs_target ON course_prereqs(prereq_course_id);

-- Syllabi: structured, with child tables, since schools vary in what they publish.
CREATE TABLE IF NOT EXISTS syllabi (
    id               INTEGER PRIMARY KEY,
    course_id        INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    source_url       TEXT NOT NULL,
    source_type      TEXT,             -- 'html', 'pdf', 'docx'
    term             TEXT,             -- e.g. 'Fall'
    year             INTEGER,
    section          TEXT,
    instructor       TEXT,
    contact_hours    TEXT,
    delivery_mode    TEXT,             -- in person / online / hybrid
    summary          TEXT,
    grading_scale    TEXT,
    raw_text         TEXT,             -- kept so syllabi can be re-parsed later
    document_hash    TEXT,
    match_confidence REAL,             -- how sure we are this syllabus belongs to the course
    match_method     TEXT,
    parse_method     TEXT,
    extra_json       TEXT,
    extracted_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (course_id, source_url)
);

CREATE TABLE IF NOT EXISTS syllabus_outcomes (
    id          INTEGER PRIMARY KEY,
    syllabus_id INTEGER NOT NULL REFERENCES syllabi(id) ON DELETE CASCADE,
    position    INTEGER,
    text        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS syllabus_topics (
    id          INTEGER PRIMARY KEY,
    syllabus_id INTEGER NOT NULL REFERENCES syllabi(id) ON DELETE CASCADE,
    position    INTEGER,
    week        TEXT,                  -- text, since some use ranges like '3-4'
    title       TEXT NOT NULL,
    details     TEXT
);

CREATE TABLE IF NOT EXISTS syllabus_assessments (
    id          INTEGER PRIMARY KEY,
    syllabus_id INTEGER NOT NULL REFERENCES syllabi(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    kind        TEXT,                  -- exam / project / homework / quiz / participation / other
    weight_pct  REAL,
    details     TEXT
);

CREATE TABLE IF NOT EXISTS syllabus_materials (
    id          INTEGER PRIMARY KEY,
    syllabus_id INTEGER NOT NULL REFERENCES syllabi(id) ON DELETE CASCADE,
    kind        TEXT,                  -- textbook / software / dataset / other
    title       TEXT NOT NULL,
    authors     TEXT,
    required    INTEGER
);

-- One row per extraction attempt, for debugging and the UI.
CREATE TABLE IF NOT EXISTS extraction_runs (
    id          INTEGER PRIMARY KEY,
    source_id   INTEGER NOT NULL REFERENCES program_sources(id) ON DELETE CASCADE,
    started_at  TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    status      TEXT,
    adapter     TEXT,
    llm_calls   INTEGER DEFAULT 0,
    pages_fetched INTEGER DEFAULT 0,
    message     TEXT
);
