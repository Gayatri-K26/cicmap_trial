# How cicmap works, and why it was built this way

This document explains how the trial project was built: the steps the system follows, the decisions made
along the way and the reasons for them, and what is still missing. It is meant to explain the whole program
without reading the code. For how to run it, see [CLI.md](CLI.md). Numbers are as of 2026-11-23.

## What it does

cicmap reads a university program's catalog page and stores three things in a database:

1. **Program details:** degree, college, department, catalog year.
2. **The AI courses in that program:** code, title, credits, description, and prerequisites stored as links
   between courses.
3. **Syllabi for those courses:** learning outcomes, grading, textbooks, weekly topics and instructor,
   collected from several sources.

You can use it from the command line (`cicmap ...`) or a web page (`cicmap ui`).

**Results for the 14 programs on google sheet:**

| | |
|---|---|
| Programs extracted | 14 of 14 |
| AI courses found | 153 (out of the full curriculum of each program) |
| Prerequisite links between courses | 243 (plus 24 non-course conditions like "junior standing") |
| AI courses with at least one real syllabus | 27 of 153, at 5 of 14 schools |

Two more programs were added by hand to test adding a school (Illinois Tech's MS in AI, Northeastern's BS in
Computer Science), bringing the database to 175 AI courses and 28 courses with a real syllabus.

## The big picture

```
Google Sheet ──sync──▶ list of programs to extract
                              │
                        extract (per program)
                              ▼
  1. fetch the program page          (saved in the database)
  2. pick a parser for the page      (Acalog, SmartCatalog, CourseLeaf, or DeepSeek fallback)
  3. read the curriculum             (every course in the program)
  4. find the AI courses             (keywords + DeepSeek, two passes)
  5. read their prerequisites        (turned into links between courses)
  6. store everything                (programs, courses, links, catalog entry as first syllabus source)
                              │
      syllabus portal / web / llm    (more syllabus sources, then DeepSeek fills gaps)
                              ▼
                 CLI (cicmap ...)  and  web UI (cicmap ui)  read the database
```

The sections below follow this order. Section 9 explains the database itself.

## How it works, step by step

### 1. Getting the list of programs (`cicmap sync`)

The sheet has one row per program. cicmap uses three of its columns: **Institution**, **Program** and
**Latest Catalog Link** (the URL of the program's catalog page). Rows without a link are ignored.

What a sync does:

1. If the last sync was less than 6 hours ago, it stops (unless you pass `--force`).
2. It downloads the sheet as a CSV file. If the file is identical to last time (same fingerprint/hashcode), it stops.
3. Otherwise it compares each row with the database, using the catalog URL as the row's identity:
   - **New URL:** the program is added with status `pending` (waiting to be extracted).
   - **Same URL, but the institution or program name changed:** the row is updated and set back to `pending`.
   - **URL no longer in the sheet:** the program is marked inactive. Its data stays in the database.
   - **A program added by hand that later appears in the sheet:** it becomes a sheet program.

Each program to extract is one row in the `program_sources` table. A program added by hand
(`cicmap add` or the UI's "Add program" page) is the same kind of row, marked `manual`.

### 2. Fetching pages

Every page cicmap downloads is saved in the `raw_pages` table with its address, status code, text and how it
was fetched. Before downloading, cicmap looks there first, so a second run reads saved copies and touches no
website. `--refresh` skips the saved copy and downloads again.

How a page is downloaded:

1. **A normal web request** (like a browser, but without running the page's scripts). Requests to the same
   site are spaced at least half a second apart, to be polite.
2. **If the answer looks like a bot check**, a headless Chrome browser loads the page instead. "Looks like a
   bot check" means the page is almost empty (under 200 characters) or contains the telltale text of a
   Cloudflare or AWS bot check. This happens on the five Acalog catalogs.
3. Once the browser gets past the check, the site's cookies are copied into the normal request client, so the
   rest of that site's pages (all the course pages) are fetched quickly without the browser.
4. A "page not found" (404) is saved as such and never sent to the browser, since no check is involved.

### 3. Reading the catalog

#### Choosing a parser

A parser is the code that knows one catalog platform's page layout. For each program page, cicmap picks a
parser in this order:

1. **A parser forced for that program** (set by hand in the database, rarely used).
2. **A parser that claims the website's domain** (SmartCatalog claims `smartcatalogiq.com`).
3. **Each parser scores the page from 0 to 1** for "I can read this", by looking for its platform's markers
   (for example Acalog's `preview_program.php` address, or CourseLeaf's course table). The best score wins if it
   is at least 0.5.
4. **If none qualifies, the DeepSeek fallback** reads the page (only when DeepSeek is turned on).

#### What each parser reads

Every parser produces the same two things: the program details, and the list of courses in the curriculum
with each course's code, title, credits, the requirement group it's listed under (e.g. "Core Requirements",
"AI Electives"), whether it's one option among several, and the link to its own page.

| Parser | How the program page lists courses | Where course details come from |
|---|---|---|
| Acalog | Courses in requirement sections under headings; each course is a link that opens a popup | Each course's own page; fields are found by their labels ("Prerequisite:", "Credits:"), since labels differ by school |
| SmartCatalog | Tables of courses under requirement headings | Each course's own page, with fixed blocks for description, credits and prerequisites |
| CourseLeaf | One course table on the program page | CourseLeaf's course lookup, which returns one course's block by code |
| DeepSeek fallback | DeepSeek reads the page text and lists the courses | The course's own page, or the text right after the course on the program page (Hallmark) |

Some details handled along the way:

- **Course codes are written many ways** ("CS 380", "CS-380", "CPS*2800", BYU's "C S 111"). They are all
  turned into one form, `SUBJECT NUMBER` (e.g. `CS 380`), so a prerequisite on one page matches the course row
  from another page.
- **Some program pages show only part of the curriculum** and link to the full list (Miami). cicmap follows
  that link once, reads the full list with the right parser, and keeps details from the first page that the
  second one lacks.
- **Missing program details** (degree, college, department, catalog year) are filled in two ways: the degree
  from the program name ("Bachelor of Science" → BS), and if anything is still missing, DeepSeek reads the top
  of the page.
- **BYU lists course titles without codes.** Those courses get a temporary placeholder code, and the real code
  is read from each course's page.
- **The DeepSeek fallback is checked against the page:** a course is kept only if its code or title appears in
  the page text, and a link only if it is an actual link on the page.

### 4. Finding the AI courses

A curriculum lists 25 to 129 courses, and most are not about AI (programming, math, general education). The
filter runs in two passes so DeepSeek only reads full descriptions for likely candidates.

**Pass 1: titles, for the whole curriculum.**

- A keyword check labels each title:
  - **yes** if the title contains a strong AI term ("artificial intelligence", "machine learning", "neural
    network", "computer vision", "NLP", "robotics", ...);
  - **maybe** if it contains a weaker term ("data science", "analytics", "optimization", "algorithm", ...);
  - **no** otherwise.
- Then one DeepSeek call reads all the titles at once and can upgrade a course to yes or maybe (for titles like
  "Special Topics" or "Capstone"). It can't downgrade a keyword "yes".

**Pass 2: descriptions, only for yes and maybe.**

- Each candidate's course page is fetched (description, credits, prerequisites, other labeled fields).
- One DeepSeek call reads all candidate descriptions together, with a written definition of what counts as an
  AI course (AI or machine learning is the main subject or a major part; general programming, math and
  statistics don't count even when they are prerequisites). For each course it returns: AI or not, a category
  from a fixed list (foundations, machine learning, deep learning, NLP, computer vision, robotics, ethics, ...),
  a confidence from 0 to 1, and a one-line reason.

Courses DeepSeek marks as AI are stored as AI courses. With DeepSeek off (`--no-llm`), the keyword labels
decide on their own, and the stored method says `keyword` instead of `llm`.

**When a course has no description.** The decision then rests on the title alone. Such a course's reason starts
with "Judged on title only (no description found)" and its confidence is capped at 0.6, so it stands out as
less certain in the CLI and the UI. Today every AI course has a description; the two that didn't were parser
bugs, now fixed (Northeastern's catalog uses a different element for descriptions, and a Dakota State
description that began "Concepts in Artificial intelligence: ..." was read as a field label).

### 5. Prerequisites

Each AI course's prerequisite text is turned into **groups**: all groups are required, and any one course in a
group is enough for that group. For example, Drexel's CS 380 says "CS 260 [Min Grade: C] and CS 270 [Min
Grade: C]", which becomes two groups:

| Course | Group | Requires | Minimum grade |
|---|---|---|---|
| CS 380 | 1 | CS 260 | C |
| CS 380 | 2 | CS 270 | C |

"CS 101 or CS 102" would be one group with two rows. Requirements that aren't courses ("junior standing",
"permission of instructor") are stored as rows with text instead of a course. Corequisites (taken at the same
time) use the same table, marked as corequisites.

How the text is read:

- **DeepSeek** reads the prerequisite text of all of a program's AI courses in one call and returns the groups.
- **A rule-based reader** is the fallback (and runs alone without DeepSeek): it splits the text on "and",
  commas, semicolons and parentheses, and treats course codes inside each piece as alternatives.
- A fix found during testing: Miami writes "OR" and "AND" in capitals, which was misread, so they are turned
  into lowercase before parsing.

Prerequisite courses that aren't AI courses (CS 260 above) are stored as **placeholder courses**: just code and
title, marked as stubs, so every link points to a real row. There are 130 of these.

Because every requirement is a row pointing from one course to another, "what does CS 380 require?" and "which
courses require CS 380?" are both simple lookups. The UI shows them as **Requires** and **Needed for**.

**Loop check.** After every extraction, cicmap follows the prerequisite links looking for a loop (A requires B,
B requires A, possibly through longer chains). A real curriculum never has one, so the extraction result gets a
warning, and `cicmap check` lists any loops in the whole database. Corequisites are left out, because a lecture
and its lab may each list the other.

### 6. Storing a program

When a program is extracted again (for example after the sheet changed), its rows are updated, not
duplicated:

- the program row is updated in place;
- its list of AI courses is replaced;
- each AI course is updated in place (a course is identified by school + code), and its prerequisite rows are
  replaced;
- each AI course's catalog entry is saved again as its first syllabus document (step 7);
- a course that no program lists as an AI course any more stops counting as one. Its row is kept, because
  prerequisite links and syllabi may point to it. (Example: Northeastern's EECE 2160 "Embedded Design: Enabling
  Robotics" was an AI course on its title alone; once its description was read, it wasn't.)

Every run also writes a row to `extraction_runs` with its result, the parser used, how many pages were
fetched and how many DeepSeek calls were made. The program's status becomes one of:

| Status | Meaning |
|---|---|
| `extracted` | Done. |
| `needs_parser` | No parser could read the page, or the parser found no courses (the layout may have changed). |
| `blocked` | The page stayed behind a bot check, even in the headless browser. |
| `failed` | Something else went wrong; the error message is saved and shown by `cicmap sources`. |

One program failing never stops the others.

### 7. Syllabi

A syllabus here means the real course syllabus. The data is uneven: some courses have grading and textbooks,
others only learning outcomes. The design handles that by storing every source separately and combining them.

#### How syllabi are stored

- **One row per source document** in the `syllabi` table: its address, what kind of source it is (`catalog`,
  `portal`, `faculty` page, `department` page), file type (web page, PDF, Word), term and year, instructor,
  summary, grading scale, delivery mode (online, in person), the document's full text, and how it was parsed.
- **Its contents in section tables**, each row pointing back to the document: learning outcomes, weekly topics,
  assessments (name, type such as exam or homework, weight in percent), and materials (title, authors, type:
  textbook, software or other).
- **A link table** (`syllabus_courses`) says which course(s) a document belongs to, how sure we are (0 to 1) and
  how that was decided. A document can belong to several courses, for classes taught together like
  "CS 471 / CS 571".

Every section can be empty. Nothing is invented to fill a gap.

#### The combined syllabus

For each course, cicmap builds one view from all its documents. Documents are ranked:

1. real syllabi before the catalog entry (they are more specific);
2. then the more certain match;
3. then the most recent term.

Each section (outcomes, topics, grading, materials, instructor, contact hours, grading scale, summary) is taken
from the highest-ranked document that has it, and keeps a note of which document that was. The summary is never
taken from the catalog entry, because the catalog description is already shown on the Details tab. Sections no
document has are listed as "not found".

#### Source 1: the catalog entry

Every AI course's catalog entry is stored as its first document, with match certainty 1.0:

- the description becomes the summary;
- any label containing "outcome" becomes learning outcomes, with numbered lists ("1. ... 2. ...") split into
  separate items (only when the numbers count up from 1, so "Python 3." inside a sentence doesn't split it);
- lecture and lab hours become contact hours.

#### Source 2: Simple Syllabus libraries (`cicmap syllabus portal`)

Purdue and Middle Georgia State publish syllabi in public Simple Syllabus libraries. For each AI course:

1. **Search** the library's public search for the course code. It returns a list of syllabi with titles like
   "CS (WL) 37300 PIN" and terms like "WL Fall 2026".
2. **Keep exact matches:** public syllabi whose title, once the part in brackets is removed, is exactly this
   course code. For Purdue, only titles with "(WL)" (West Lafayette), because the Fort Wayne campus "(FW)" uses
   the same codes for different courses.
3. **One per term, newest three terms.** Sections of the same term share a syllabus. Three terms are kept
   because a newer syllabus sometimes drops a section an older one had (Middle Georgia State's 2027 CSCI 4250
   has no textbook; 2026 does).
4. **Match certainty:** 0.95 if the syllabus title agrees with our course title, 0.8 if not (titles get renamed).
5. **Load the page.** Syllabus pages are built by JavaScript, so a plain request returns an empty shell. After
   checking the site's robots.txt allows it, the headless browser loads the public page and the rendered page
   is saved.
6. **Read the sections.** Each section on the page has a heading, and headings are matched by keyword:
   "outcome" or "objective" → outcomes; textbook/materials sections → materials (title, authors, edition);
   "instructor" → instructor; "schedule" or "week" → topics; "grading" or "evaluation" → grading scale; the
   description section → summary. Facts written after the description ("Credits 3 ... Prerequisite ...") are
   split off, and the prerequisite wording is kept separately (see "Prerequisites from syllabi" below).
   Grading weights are only taken when written as "Name NN%" and adding up to about 100%.

#### Source 3: the schools' own websites (`cicmap syllabus web`)

For each AI course:

1. **Search with Brave** for `site:<school domain> "<course code>" syllabus`, for example
   `site:drexel.edu "CS 380" syllabus`. The school's domain comes from its catalog address
   (`catalog.purdue.edu` → `purdue.edu`; SmartCatalog's `kean.smartcatalogiq.com` → `kean.edu`). Results are
   saved, so a repeated search costs nothing.
2. **Drop results that can't be a usable syllabus**, by address: catalog sites (already used), other syllabus
   platforms, course websites behind a login (Canvas, Blackboard, Brightspace), and pages whose address says
   catalog, transfer, directory, schedule, assignment, project, lecture, slides and similar, unless the address
   also says "syllabus".
3. **Download the document as text**, after checking robots.txt: web pages (menus and scripts removed), PDFs (up
   to 40 pages; longer ones are catalogs or reports) and Word files (including tables). The text is saved. The
   headless browser is never used here: a page behind a bot check is skipped.
4. **Check it's this course's syllabus.** All three must hold:
   - the course code is in the page title, address or the first part of the text;
   - the text names at most 15 different courses;
   - at least 3 kinds of syllabus wording appear (grading, textbook, office hours, outcomes, schedule,
     prerequisite, attendance, exam, homework, instructor).
5. **Read what the rules can read reliably:** term and year near the top ("Spring 2021"); the instructor after an
   "Instructor:" label; bullet-point outcomes under an outcomes or objectives heading (joining lines a PDF
   wrapped); "Name NN%" grading weights that add up to about 100%; "Week N: ..." topics.
6. **Save it** with match certainty 0.9 if our course title also appears in the text, 0.75 if only the code
   does. The source kind is `faculty` if the address looks like a personal page (`/~name/`, `/homes/`,
   `/people/`), otherwise `department`.
7. **Remove documents that no longer pass.** If the rules were tightened since a document was saved, running
   the command again deletes it.

#### Source 4: the DeepSeek pass (`cicmap syllabus llm`)

For every portal and website document (not catalog entries):

1. DeepSeek gets the course code and title and the document's text (first 15,000 characters), and is asked for
   outcomes, weekly topics, assessments with weights (converting points to percent), materials, instructor,
   term, year and grading scale, copying text from the document.
2. Its answer is checked against the document: each outcome, topic, assessment name and material title must
   appear in the text (its first six words, ignoring punctuation and capitals), the year must appear in the
   text, and if the weights don't add up to about 100% they are dropped (the names are kept).
3. Only sections that are still empty are filled. Anything the rules already found is never overwritten.
   One exception: if the rules found assessment names without weights, DeepSeek's weights for the same names
   are added, but only if the document's weights then add up to about 100%.
4. The document is marked as `deterministic+llm`, with a list of the sections DeepSeek filled.

The source commands re-read their documents each time they run, which clears what DeepSeek added, so this
command is always run last. Its answers are saved, so running it again costs nothing.

#### Prerequisites from syllabi

Purdue's catalog pages list no prerequisites, but its syllabi do (inside the description). That wording is
stored with the syllabus document and shown on the course's Prerequisites tab, labeled with its source, when the
catalog has none. It is shown as written; it isn't turned into course links.

#### Details tab vs Syllabus tab

Each fact appears in one place in the UI:

- **Details:** what the course is: description, credits, terms offered, restrictions, AI category (with the
  reason, confidence and method in a collapsed section), and any other catalog labels (collapsed).
- **Syllabus:** how it's taught: outcomes, contact hours, grading, textbooks, topics, instructor.

Catalog labels are sorted by one rule in the code: a label containing "outcome", or one of the lecture/lab hours
labels, goes to the syllabus; everything else stays on Details.

#### Comparing schools

- **Topic search** (`cicmap syllabus search`, "Compare schools" page): finds AI courses whose title, summary,
  outcomes or weekly topics mention a phrase, grouped by school.
- **Shared textbooks** (`cicmap syllabus textbooks`): groups materials by title, ignoring capitals, punctuation
  and edition, and lists those used at two or more schools. Software (Python, PyTorch) is left out.

### 8. DeepSeek: how calls are made and saved

- cicmap talks to DeepSeek through the same library used for OpenAI, pointed at DeepSeek's address, and always
  asks for an answer in JSON (structured data), with randomness turned off so the same question gets the same
  answer.
- If the answer isn't valid JSON or the request fails, it retries up to three times, then gives up on that step
  (the rule-based result is kept).
- Every answer is saved in the `llm_cache` table under a fingerprint of the model, the purpose (for example
  "parse_prereqs"), a version number and the exact prompt. The same question is never paid for twice.
  Changing a prompt's version number deliberately makes cicmap ask again.
- Without a DeepSeek key, every step still runs on its rule-based path.

### 9. The database

cicmap uses **SQLite**: the whole database is one file, `data/cicmap.db` (about 45 MB, most of it saved pages).
SQLite needs no server or setup, comes with Python, and handles this amount of data easily. The file's location
can be changed with `CICMAP_DB` in `.env`.

Each time cicmap opens the database it applies the table definitions in
[cicmap/schema.sql](../cicmap/schema.sql) (they only create tables that don't exist yet), turns on checks that
links between rows point to real rows, and upgrades older layouts if needed (the syllabus tables were reshaped
once, while they were still empty).

The tables, in four layers (row counts as of 2026-11-23):

| Layer | Table | One row is | Rows |
|---|---|---|---|
| What to extract | `program_sources` | a program to extract (from the sheet or added by hand), with its status | 16 |
| | `sync_state` | a saved setting, like the last sync time and the sheet's fingerprint | 2 |
| Saved inputs | `raw_pages` | a fetched page, search result or document text | 1,197 |
| | `llm_cache` | a saved DeepSeek answer | 204 |
| Programs and courses | `institutions` | a school | 15 |
| | `programs` | an extracted program's details and the parser used | 16 |
| | `courses` | a course: AI courses in full, prerequisite-only courses as placeholders | 305 |
| | `program_courses` | "this AI course is in this program", with its requirement group | 184 |
| | `course_prereqs` | one prerequisite: course, group, required course or condition, minimum grade | 324 |
| Syllabi | `syllabi` | one source document | 213 |
| | `syllabus_courses` | "this document belongs to this course", with match certainty | 213 |
| | `syllabus_outcomes` | one learning outcome | 298 |
| | `syllabus_topics` | one weekly topic | 302 |
| | `syllabus_assessments` | one graded component and its weight | 148 |
| | `syllabus_materials` | one textbook, reading or software item | 87 |
| Log | `extraction_runs` | one extraction attempt and its result | 73 |

Because the data is in proper tables rather than text blobs, questions like "which AI courses at Drexel require
CS 260?" or "which schools use Bishop's textbook?" are single database queries.

### 10. The interfaces

- **CLI:** every step above has a command; see [CLI.md](CLI.md).
- **Web UI (Streamlit)**, three pages:
  - **Browse:** all programs and their status; click one for its AI courses; click a course for its Details,
    Prerequisites and Syllabus tabs. It only reads saved data, so browsing never triggers extraction.
  - **Compare schools:** topic search and the shared-textbook table.
  - **Add program:** enter a school, program and catalog URL. If it was already extracted, the saved result is
    shown; otherwise extraction runs in the background (so clicking around doesn't interrupt it), and a progress
    panel updates every 2 seconds.

### 11. Testing

87 automated tests run offline in about a second. They use saved copies of real catalog and syllabus pages
(in `tests/fixtures/`), databases created in memory, and fake DeepSeek and search answers, so they never touch
the network or cost anything. Every parser has a saved page and a test. Each change was committed separately
after its tests passed.

## Decisions and why

| Date | Decision | Why |
|---|---|---|
| 11-16 | Python 3.9, SQLite, Typer for the CLI, Streamlit for the UI | Quickest route to a working database and the three required UI views. |
| 11-16 | The Google Sheet is the live source of programs, no local copy | One source of truth; only new or edited rows get re-extracted. |
| 11-16 | One parser per catalog platform, all producing the same format | Platforms repeat across schools, so a few parsers cover most schools; a new platform is one file. |
| 11-16 | Cache every page and every DeepSeek answer in the database | A full re-run went from about 2 minutes to under 2 seconds with zero API calls. |
| 11-16 | Two-pass AI filter (titles, then descriptions) with confidence and reason stored | Cheap first pass over the whole curriculum; borderline courses can be reviewed. |
| 11-16 | Prerequisites stored as groups of course-to-course links | "What does X require" and "what requires X" become simple queries. |
| 11-16 | Headless browser only for pages behind a bot check, cookies reused afterwards | Five Acalog catalogs return empty pages to plain requests; the browser is slow, so use it once per site. |
| 11-18 | DeepSeek fallback parser for pages no parser recognizes, keeping only courses that appear on the page | BYU, Hallmark and Miami use custom sites; the check stops invented courses. |
| 11-19 | A syllabus is a set of source documents, each linked to one or more courses, combined per course | data is uneven and from several sources; this keeps every fact tied to its source. |
| 11-19 | The catalog entry is the first syllabus source | Every course has one, and some catalogs list outcomes and hours. |
| 11-19 | Prerequisite loop check, run after every extraction | A loop is always an error; it found a real parser bug on its first run (Hallmark read the next course's code as a prerequisite). |
| 11-19 | Use public Simple Syllabus libraries (Purdue, Middle Georgia State) | Real, current syllabi from one predictable source. Their robots.txt allows general crawlers. |
| 11-19 | Skip Concourse (Capitol Tech, Harding) | Its robots.txt disallows all automated access. |
| 11-19 | Skip Kean's and Miami's Simple Syllabus | Both need a login. |
| 11-19 | Check robots.txt before fetching any syllabus page; never use the headless browser for documents found by search | Respect what sites allow. The browser is used for public pages that need JavaScript (catalogs, Simple Syllabus), not for getting around blocks. |
| 11-19 | Brave Search limited to each school's domain, with three content checks | Open search returned other schools' courses; most domain results were degree maps and old catalogs. |
| 11-20 | Rules first, DeepSeek only for empty sections, with every item checked against the text | Rules are predictable and free; DeepSeek reads messy layouts the rules can't, and the checks stop invented data. |
| 11-20 | Grading weights kept only if they add up to about 100% | Caught a table counted twice (200%) and partial lists; also stops a grade scale ("A 90%") being read as weights. |
| 11-20 | Tried, then dropped, broader searches for the 9 schools with no syllabus | About 320 extra searches found no syllabus there, only assignment pages and course lists. |
| 11-21 | Open Syllabus not used | Its terms don't allow access to individual syllabi; a free student account only shows totals up to 2019. |
| 11-21 | Prerequisites written inside Purdue's syllabus descriptions are shown on the Prerequisites tab | Purdue's catalog lists no prerequisites, so this fills a real gap for 7 of its 11 AI courses. |
| 11-23 | Courses with no description are flagged "judged on title only", confidence at most 0.6 | A title alone can mislead; the flag shows which verdicts are less certain. |
| 11-23 | Before changing a parser, re-parse every saved page of that platform and compare | Shows exactly which courses a fix changes (the Acalog label fix changed only the course it was meant to). |
| 11-23 | Tried, then dropped, following "syllabus" links one step from course pages found by search | Of 72 course pages, 5 had such a link and none led to a new syllabus, so it added code without adding data. |
| 11-23 | Don't follow links from program pages to other requirement pages | On our 14 programs those links lead only to general education, university core and other degrees, never AI elective lists. |
| — | Never generate a syllabus with an LLM when none is found | It would be invented data presented as a real syllabus. Missing sections are shown as "not found" instead. |

## Gaps

### Syllabus coverage

- **Only 27 of the 153 sheet AI courses (5 of 14 schools) have a real syllabus.** Purdue and Middle Georgia
  State are well covered through their public libraries; Illinois Tech, Drexel and Miami partly, through
  professors' pages.
- **9 schools have none:** BYU, Capitol Tech, Dakota State, Ferris, Hallmark, Harding, Kean, Marymount and
  Rhode Island College. Their syllabi appear to be non-public: inside course websites that need a login
  (Canvas, Blackboard), or in a portal that blocks automated access (Concourse). Some pages the search index
  still lists have been deleted (for example Purdue's "canonical syllabi" and BYU's learning-outcomes pages).
- **Some syllabi are old.** Professors' pages range from 2013 to 2027. The term and year are shown with every
  section, but an old syllabus may not match how the course is taught today.
- **Uneven sections.** Of the 175 AI courses in the database, grading is known for 25, textbooks for 23,
  weekly topics for 18 and instructors for 20.

- **Syllabi reachable only by clicking through aren't found.** Following "syllabus" links one step from the
  course pages search found was tried and dropped: of 72 such pages, 5 had a link, and none led to a new
  syllabus (one was already found; the rest were old index pages, a deleted Purdue page and a
  registration-system stub). It would be worth retrying for new schools.
- **Miami now blocks automated requests.** Its site redirects them to a "sorry" page, so its pages can't be
  re-read. Its earlier syllabi stay saved.

### Data quality

- **The AI definition and keywords are our own.** The trial brief says "AI-related courses" without defining
  it, so the definition given to DeepSeek, the keyword lists and the 13 categories were written for this
  project, not taken from cicmap or a published standard. No hand-labelled set measures how often the filter
  is right. Borderline cases show the effect, for example Northeastern's MKTG 4606 (a data analytics
  practicum) counts as AI at 0.7. Matching cicmap's own definition, or the AI topics in the ACM/IEEE CS2023
  curriculum guidelines, would make it easier to defend.
- **Categories are only labels.** Each AI course gets one category, shown in the CLI and UI, but nothing
  groups or compares by it yet (for example a category breakdown per school on the Compare page).

- **Textbooks with different titles aren't matched.** Middle Georgia State lists "ARTIFICIAL INTELLIGENCE
  (4TH 21 ed.)" and Purdue "Artificial Intelligence: A Modern Approach"; they are the same book but aren't
  matched. Matching on ISBN, where a syllabus gives one, would fix most cases.
- **Purdue's prerequisites from syllabi are shown as text, not links.** They appear on the Prerequisites tab
  but aren't turned into course-to-course links, so they don't show under "Needed for".
- **Curricula split across pages aren't followed.** If a future program lists some AI courses on a separate
  page (an "approved electives" list), only the program page is read. None of our 14 programs do this (Miami's
  full course list is the exception, and it is followed). Rule-based parsers would need to report such links.
- **Missing program details:** Acalog pages and BYU don't name the college, and Hallmark lists no credits.
- **The content checks were tuned on a small sample.** The 15-course limit and the 3-word minimum fit the
  pages seen so far; schools with different page styles could need adjusting.
- **Known gap in the DeepSeek fallback parser.** When a course is read from a program page (Hallmark style),
  its text stops at the next course in the program's list, but not at a course outside it, so that course's
  code could be read as a prerequisite. The loop check only catches this when it creates a loop. Current data
  was checked and is clean.

### How it runs

- **Adding a program in the UI doesn't search for syllabi.** It extracts the courses and stores their catalog
  entries, but the Simple Syllabus search, web search and DeepSeek pass only run from the command line.
- **Order matters when re-running syllabus steps.** `syllabus portal` and `syllabus web` re-read their
  documents, which resets what the DeepSeek pass added; `syllabus llm` must be run after them (it restores
  everything from the cache at no cost).
- **Bad input isn't always handled cleanly.** `cicmap extract 999` (an id that doesn't exist) shows a raw
  Python error instead of a short message.

### Open questions and housekeeping

- **How does cicmap handle courses with no public syllabus** (catalog only, collected by hand, or a dataset)?
- **API keys:** the DeepSeek and Brave keys were shared in chat and should be replaced after the trial.
