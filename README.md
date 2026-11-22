# cicmap_trial

Extracts program metadata, AI-related courses, prerequisites and (soon) syllabi from university
catalog pages into a SQLite database.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e .            # adds the `cicmap` command (then: source .venv/bin/activate)
.venv/bin/python -m playwright install chromium   # headless browser for bot-protected catalogs
echo "DEEPSEEK_API_KEY=sk-..." > .env      # never commit this file
```

## Usage

Full command reference, pipeline order and how to test: [docs/CLI.md](docs/CLI.md).

### Web UI

```bash
.venv/bin/python -m cicmap ui     # opens http://localhost:8501
```

- **Browse:** every program with its status; click one to see its cached AI courses, then a course for its
  description, prerequisites (as written and parsed), the courses that require it, and its syllabi. Reads the
  database only.
- **Add program:** university, program and catalog URL. A URL already extracted shows the cached result;
  otherwise extraction runs in the background (you can keep browsing) with live progress.
- **Sync from Google Sheet** in the sidebar pulls new or edited rows.

### CLI

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
| Coursedog | BYU | extracted (LLM fallback; course ids resolved from course pages) |
| Custom sites | Hallmark | extracted (LLM fallback; details read inline from the program page) |
| Custom sites → CourseLeaf | Miami | extracted (LLM fallback finds the full course list, then CourseLeaf parses it) |

All 14 programs in the sheet extract: 153 AI courses, 266 prerequisite links.

**LLM fallback** ([cicmap/adapters/llm_fallback.py](cicmap/adapters/llm_fallback.py)): used only when no
deterministic parser matches and the LLM is on. A course is kept only if its code or title is on the page, and a
link only if it is a real link on the page. A deterministic parser is still preferable when a platform recurs.

Known gaps: Purdue's catalog shows no prerequisites on course pages; Acalog pages and BYU don't name the college;
Hallmark lists no credits.

Tests: `.venv/bin/pytest`
