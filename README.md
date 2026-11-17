# cicmap_trial

Extracts program metadata, AI-related courses, prerequisites and (soon) syllabi from university
catalog pages into a SQLite database.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium   # headless browser for bot-protected catalogs
echo "DEEPSEEK_API_KEY=sk-..." > .env      # never commit this file
```

## Usage

```bash
.venv/bin/python -m cicmap sync              # pull the program list from the Google Sheet
.venv/bin/python -m cicmap sources           # list programs and their status
.venv/bin/python -m cicmap extract --pending # extract everything not yet extracted
.venv/bin/python -m cicmap stats             # AI courses per school
.venv/bin/python -m cicmap courses drexel    # cached AI courses for a school
.venv/bin/python -m cicmap course illinois "CS 480"   # one course + parsed prerequisites
.venv/bin/python -m cicmap add "Some U" "BS AI" https://catalog.some.edu/ai   # add a program by hand
```

Flags: `extract --no-llm` (keyword filter only), `--refresh` (re-fetch pages), `--no-browser` (never use the
headless browser), `sync --force` (ignore the TTL).

## How it works

```
sheet sync -> fetch (cached) -> pick parser -> parse curriculum -> AI title screen
           -> course details -> AI classification -> prerequisite parsing -> SQLite
```

- **Sheet sync** ([cicmap/sheet.py](cicmap/sheet.py)): skipped within a 6h TTL; skipped if the CSV hash is
  unchanged; otherwise rows are diffed by catalog URL so only new or edited rows are re-queued.
- **Fetching** ([cicmap/fetch.py](cicmap/fetch.py), [cicmap/browser.py](cicmap/browser.py)): plain HTTP first. If a
  page is a bot check (AWS WAF, Cloudflare), it is loaded once in headless Chromium, and the browser's cookies are
  copied to the HTTP client so the rest of that site is fetched over plain HTTP. One browser is shared per run.
- **Caching**: raw pages (`raw_pages`) and LLM answers (`llm_cache`) are stored, so re-runs cost no
  requests or tokens. Use `--refresh` to re-fetch.
- **Parsers** ([cicmap/adapters/](cicmap/adapters/)): one per catalog platform, all returning the models in
  [cicmap/models.py](cicmap/models.py). Chosen by manual override, then domain, then `detect()` score.
- **AI filter** ([cicmap/ai_filter.py](cicmap/ai_filter.py)): keywords + a batched DeepSeek call on titles, then
  a second call with descriptions for the candidates. Stores category, confidence and reason.
- **Prerequisites** ([cicmap/prereqs.py](cicmap/prereqs.py)): parsed into AND-of-OR groups in `course_prereqs`,
  so "what does X require" and "what requires X" are plain SQL queries.
- **Schema**: [cicmap/schema.sql](cicmap/schema.sql), including structured syllabus tables.

## Adding a parser

Create `cicmap/adapters/<platform>.py` with a `@register`ed `CatalogAdapter` subclass (`detect`,
`parse_program`, `course_detail_url`, `parse_course`), import it in `cicmap/adapters/__init__.py`, and add a
saved sample page plus a test in `tests/`. Bump `version` when parsing changes.

## Status (Phase 1)

| Platform | Schools | State |
|---|---|---|
| CourseLeaf | Drexel, Illinois Tech | extracted |
| SmartCatalog | Kean, Marymount, Middle Georgia State, Rhode Island | extracted |
| Acalog | Capitol Tech, Dakota State, Ferris, Harding, Purdue | extracted (program page via headless browser) |
| Coursedog | BYU | needs parser |
| Custom sites | Hallmark, Miami | needs parser (LLM fallback planned) |

Known gaps: Purdue's catalog shows no prerequisites on course pages; Acalog pages don't name the college.

Tests: `.venv/bin/pytest`
