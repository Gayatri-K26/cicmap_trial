# cicmap command-line guide

cicmap pulls AI courses, prerequisites and syllabi from university catalog pages into a SQLite database.
Everything is run as `.venv/bin/python -m cicmap <command>`. Every command has `--help`.

Most commands only read the database and are instant. The commands that fetch pages or call an API
(`sync`, `extract`, `syllabus portal|web|llm`) cache what they get, so running them again is fast and free
unless you pass `--refresh`.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium     # headless browser for bot-protected catalogs
```

Keys go in `.env` in the project root (gitignored; never commit it):

```
DEEPSEEK_API_KEY=sk-...      # AI filter, prerequisite parsing, LLM fallback parser, syllabus LLM pass
BRAVE_API_KEY=...            # only for `syllabus web`
```

Without `DEEPSEEK_API_KEY` the pipeline still runs, using keywords and regex only (same as `--no-llm`).

## The pipeline, in order

```
sync  ->  extract  ->  syllabus portal  ->  syllabus web  ->  syllabus llm
(program list)  (courses,      (Simple Syllabus     (school websites,  (DeepSeek fills
                prerequisites,  libraries)           Brave Search)      sections the
                catalog entry)                                          rules missed)
```

To build everything from scratch:

```bash
.venv/bin/python -m cicmap sync
.venv/bin/python -m cicmap extract --pending
.venv/bin/python -m cicmap syllabus portal
.venv/bin/python -m cicmap syllabus web
.venv/bin/python -m cicmap syllabus llm
.venv/bin/python -m cicmap check
```

Run `syllabus llm` after `portal` and `web`: those two re-parse their documents, which resets what the LLM
pass added. Re-running `syllabus llm` restores it from the cache with no API calls.

## Commands

### Programs

| Command | What it does |
|---|---|
| `sync [--force]` | Updates the program list from the Google Sheet. Skipped if the last sync was under 6 hours ago or the sheet is unchanged; `--force` downloads it anyway. |
| `add INSTITUTION PROGRAM URL` | Tracks a program that isn't on the sheet. Prints the new source id. |
| `sources [--all]` | Lists programs with their id, status and AI-course count. `--all` includes programs removed from the sheet. |

```bash
.venv/bin/python -m cicmap add "Wellesley College" "BA Computer Science" "https://catalog.wellesley.edu/..."
# Added source 17. Run: python -m cicmap extract 17     (the id depends on what's already tracked)
```

### Extraction

`extract [IDS...] [--pending | --all]` fetches each program page, picks a parser, finds the AI courses,
parses their prerequisites and stores the result.

| Option | Use it to |
|---|---|
| `IDS...` | Extract specific programs (ids from `sources`). |
| `--pending` | Extract every program not yet extracted. |
| `--all` | Re-extract every active program. |
| `--no-llm` | Run without DeepSeek: keyword AI filter, regex prerequisites, no fallback parser. |
| `--refresh` | Re-fetch pages instead of using the cache. |
| `--no-browser` | Never use the headless browser (pages behind a bot check then fail as `blocked`). |
| `-v` | Show each step. |

```bash
.venv/bin/python -m cicmap extract 4
# [4] Drexel University
#     EXTRACTED: 20 AI courses out of 82 in the curriculum  (pages fetched 0, via browser 0, LLM calls 0)
```

A program ends as `extracted`, `needs_parser` (no parser recognized the page), `blocked` (bot check) or
`failed` (the error is saved and shown by `sources`). One failing program doesn't stop the others.

### Courses

| Command | What it shows |
|---|---|
| `courses SCHOOL` | A school's AI courses: code, title, credits, AI category and confidence. `SCHOOL` is any part of the name, e.g. `drexel`. |
| `course SCHOOL CODE` | One course: description, why it counts as AI, prerequisites (as written and parsed) and the courses that require it. |
| `stats` | Per school: degree, parser used, AI courses, prerequisite links. |
| `check` | Looks for prerequisite loops (A requires B, B requires A), which mean a parsing or catalog error. Exits with code 1 if it finds any. |
| `parsers` | The catalog parsers available. |

```bash
.venv/bin/python -m cicmap course drexel "CS 380"
# Drexel University  CS 380  Artificial Intelligence  (3.0 credits)
# ...
# Parsed prereqs: CS 260 (min C)  AND  CS 270 (min C)
# Required by: CS 385, CS 387, CS 417, CS 481, CS 482, CS 486, ECE 417
```

Quote course codes that contain a space (`"CS 380"`).

### Syllabi

A course's syllabus is combined from several documents: its catalog entry, Simple Syllabus pages and
syllabi found on the school's website. Each section (outcomes, grading, textbooks, weekly topics, instructor)
comes from the best document that has it.

| Command | What it does |
|---|---|
| `syllabus catalog` | Stores every AI course's catalog entry as a syllabus document. `extract` does this already; use it for courses extracted before syllabi existed. |
| `syllabus portal [--school X] [--refresh]` | Finds syllabi in the public Simple Syllabus libraries (Purdue, Middle Georgia State). Keeps up to 3 recent terms per course. |
| `syllabus web [--school X]` | Searches each school's own website with Brave Search and keeps only pages that are this course's syllabus. Needs `BRAVE_API_KEY`. Documents that no longer pass the checks are removed. |
| `syllabus llm [--school X]` | DeepSeek fills sections the rules missed (grading weights, textbooks, topics). Only fills empty sections; every item must appear in the document. |
| `syllabus show SCHOOL CODE` | A course's combined syllabus, each section with the document it came from, and the sections not found. |
| `syllabus search TEXT` | Which schools have an AI course mentioning a topic (titles, summaries, outcomes, weekly topics). |
| `syllabus textbooks [--min-schools N]` | Textbooks and readings used at N or more schools (default 2). |

```bash
.venv/bin/python -m cicmap syllabus show illinois "CS 422"
# Illinois Institute of Technology  CS 422  Data Mining  (2 source documents)
# Learning outcomes  [from faculty Spring 2013: http://www.cs.iit.edu/~agam/cs422/outline/index.html]
# ...
# Grading  [from faculty Spring 2013: ...]
#     participation: 5%
#     ...

.venv/bin/python -m cicmap syllabus search "reinforcement learning"
# 'reinforcement learning': 13 schools
# Brigham Young University
#     CS 270  Introduction to Machine Learning  (in summary)
# ...
```

### Web UI

`ui [--port N]` opens the Streamlit app at http://localhost:8501. It reads the same database.

## Testing

### Automated tests

```bash
.venv/bin/pytest -q
```

They run offline in about a second, using saved pages in `tests/fixtures/` and in-memory databases. They
never call DeepSeek or Brave.

### Checking the CLI by hand

Run these against the real database. The numbers are from 2026-11-21 and will change as data changes.

| # | Command | Expected |
|---|---|---|
| 1 | `--help` | Lists every command, exit code 0. |
| 2 | `sources` | The 14 sheet programs plus any added by hand (`origin` = `manual`), all `extracted`. |
| 3 | `stats` | One row per school with AI courses and prerequisite links. |
| 4 | `courses drexel` | 20 Drexel AI courses. |
| 5 | `course drexel "CS 380"` | Parsed prerequisites `CS 260 (min C) AND CS 270 (min C)` and a "Required by" list. |
| 6 | `course drexel "CS 9999"` | `Not found.`, exit code 1. |
| 7 | `check` | `No prerequisite loops.` |
| 8 | `extract 4` | `EXTRACTED`, with `pages fetched 0` and `LLM calls 0` (everything from cache). |
| 9 | `add "Drexel University" x <Drexel's catalog URL>` | `Already tracked as source 4`. |
| 10 | `syllabus show purdue "CS 37300"` | Sections from `portal Fall 2026` and the catalog; missing sections listed at the end. |
| 11 | `syllabus search "computer vision"` | Most schools (13 on 2026-11-21), each with its matching courses. |
| 12 | `syllabus textbooks` | Shared books such as Russell & Norvig; no software like "Python". |
| 13 | `syllabus portal`, `syllabus web`, `syllabus llm` | Finish with `pages loaded 0` / `LLM calls 0` on a re-run. |

To see the exit code of the last command, run `echo $?`.

## Troubleshooting

| Problem | Fix |
|---|---|
| `DEEPSEEK_API_KEY is not set` / `BRAVE_API_KEY is not set` | Add the key to `.env` in the project root. |
| A program shows `blocked` | The site has a bot check. Run without `--no-browser` and make sure Chromium is installed (setup step 2). |
| A program shows `needs_parser` | No parser recognized the page; with DeepSeek on, the LLM fallback parser is tried automatically. |
| The UI shows an error like `no such column` after an update | Restart it (Ctrl+C, then `cicmap ui`). Streamlit keeps old code in memory until restarted. |
| Data looks stale after a catalog changed | Re-run with `--refresh` to re-fetch pages instead of using the cache. |

## Where things are stored

- Database: `data/cicmap.db` (change with `CICMAP_DB=...` in `.env`).
- Fetched pages, search results and extracted document text: the `raw_pages` table.
- DeepSeek answers: the `llm_cache` table, keyed by the prompt, so the same question is never paid for twice.
