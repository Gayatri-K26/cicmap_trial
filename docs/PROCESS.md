# How cicmap works, and why it was built this way

This document explains how the trial project was built: the steps the system follows, the decisions made
along the way and the reasons for them, and what is still missing. For how to run it, see
[CLI.md](CLI.md). Numbers are as of 2026-11-23.

## What it does

cicmap reads a university program's catalog page and stores three things in a database:

1. **Program details:** degree, college, department, catalog year.
2. **The AI courses in that program:** code, title, credits, description, and prerequisites stored as links
   between courses.
3. **Syllabi for those courses:** learning outcomes, grading, textbooks, weekly topics and instructor,
   collected from several sources.

You can use it from the command line (`cicmap ...`) or a web page (`cicmap ui`).

**Results for the 14 programs on Hasnain's sheet:**

| | |
|---|---|
| Programs extracted | 14 of 14 |
| AI courses found | 153 (out of the full curriculum of each program) |
| Prerequisite links between courses | 242 (plus 24 non-course conditions like "junior standing") |
| AI courses with at least one real syllabus | 27 of 153, at 5 of 14 schools |

Two more programs were added by hand to test adding a school (Illinois Tech's MS in AI, Northeastern's BS in
Computer Science), bringing the database to 175 AI courses and 28 courses with a real syllabus.

## How it works, step by step

### 1. Getting the list of programs

The programs to extract come from Hasnain's public Google Sheet. `cicmap sync` downloads it as a CSV and
compares it with what's already in the database:

- New rows are added, edited rows are marked to be extracted again, and rows deleted from the sheet are marked
  inactive (not deleted, so their data isn't lost).
- To avoid downloading for nothing, a sync is skipped if the last one was under 6 hours ago, and nothing
  changes if the file is identical to last time.

Programs can also be added by hand (`cicmap add`, or the "Add program" page in the UI).

### 2. Fetching pages

Every page fetched is saved in the database. Running extraction again reads the saved copy instead of the
website, so re-runs take seconds and don't load the schools' sites. `--refresh` forces a fresh copy.

Pages are fetched with a normal web request. Five schools' catalogs (the Acalog platform) sit behind a bot
check that returns an empty page to plain requests. For those, a headless browser (Chromium) loads the page
once, and its cookies are then reused for plain requests to the same site. The browser is only started when a
page is blocked.

### 3. Reading the catalog: one parser per platform

Most schools don't build their own catalog website; they use one of a few commercial platforms. So instead of
one parser per school, there is one parser per platform:

| Platform | Schools |
|---|---|
| Acalog | Capitol Tech, Dakota State, Ferris, Harding, Purdue |
| SmartCatalog | Kean, Marymount, Middle Georgia State, Rhode Island College |
| CourseLeaf | Drexel, Illinois Tech, and Miami's full course list |
| DeepSeek fallback | BYU, Hallmark, and Miami's program page |

Each parser turns a page into the same output format (program details plus a list of courses), so the rest
of the system doesn't care which platform a school uses. Adding a platform means adding one file.

For pages no parser recognizes, a fallback sends the page text to DeepSeek and asks it for the program
details and course list. To stop it from inventing courses, a course is kept only if its code or title
actually appears on the page, and a link only if it's a real link on the page.

### 4. Deciding which courses are AI courses

A program's curriculum lists 25 to 129 courses, and most aren't about AI. Filtering happens in two passes, to
keep DeepSeek costs low:

1. **Titles only, whole curriculum:** a keyword list ("machine learning", "neural", "computer vision", ...)
   marks each course yes, maybe or no, and one DeepSeek call over all titles can add more yes/maybe courses.
2. **Descriptions, only the yes/maybe courses:** their course pages are fetched, and one DeepSeek call reads
   the descriptions and gives the final answer, with a category (e.g. machine learning, NLP), a confidence
   score and a one-line reason.

The confidence and reason are stored, so borderline decisions can be checked later. The UI shows them under
"Why this counts as an AI course".

### 5. Prerequisites as links between courses

Prerequisite text like "CS 260 and (CS 270 or CS 275)" is turned into groups: every group is required, and
any one course inside a group satisfies it. Each requirement is a row in a table pointing from one course to
another. This makes both directions simple to answer: what a course requires, and which courses require it.

DeepSeek does the parsing; a simpler rule-based parser is the fallback. Courses that are prerequisites but
not AI courses (like a math course) are stored as small placeholder rows so the links always point somewhere.

A check (`cicmap check`) looks for loops, like A requiring B while B requires A. A real curriculum never has
one, so a loop means something was misread. Its first run found one real parsing bug (see Decisions).

### 6. Syllabi

When asked what "syllabus" means, Hasnain said it means the real course syllabus, that the data will be
uneven (grading and textbooks for some courses, only outcomes for others), and that how we source and
structure it is part of what's being evaluated. The design follows from that.

**How syllabi are stored.** A course's syllabus is not one document. Each source document (a catalog entry,
a syllabus page, a professor's PDF) is stored separately, with where it came from and how sure we are it
belongs to the course. Its contents go into fixed sections: outcomes, weekly topics, grading, textbooks and
materials, instructor, grading scale. Every section can be empty.

A document can belong to more than one course, for classes taught together like "CS 471 / CS 571".

**The combined syllabus.** For each course, cicmap builds one combined view: each section is taken from the
best document that has it (real syllabi rank above the catalog entry, then the more certain match, then the
newest term), and shows which document it came from. Sections no document has are listed as "not found".

**Where syllabi come from**, in order:

1. **The catalog entry** (every AI course): description, and learning outcomes and class hours where the
   catalog lists them.
2. **Simple Syllabus libraries** (Purdue, Middle Georgia State): public syllabus sites run on a common
   platform. cicmap searches each library by course code, keeps exact code matches, and stores up to the
   three most recent terms, because newer syllabi sometimes drop sections an older one had.
3. **The schools' own websites**, found with Brave Search. The search is limited to the school's own domain,
   because open web search mostly returns other schools' courses with the same number. A result is kept only
   if all three checks pass:
   - the course code is in the page's title, address or opening text (the page is about this course, not
     just mentioning it);
   - it names at most 15 courses (degree maps and catalogs name dozens; the syllabi found name at most 12);
   - it has at least 3 syllabus words (grading, textbook, office hours, outcomes, schedule, ...).
4. **A DeepSeek pass** over the documents from steps 2 and 3. Rules extract what they can read reliably
   (instructor, term, bullet-point outcomes, "Homework 40%" weights). DeepSeek then fills only the sections
   still empty, for example a grading table that a PDF flattened into one cell per line. Everything it
   returns must appear in the document's text, and grading weights must add up to about 100%.

**Details tab vs syllabus tab.** In the UI, each fact appears in one place. The Details tab holds what the
course is (description, credits, terms offered). The Syllabus tab holds how it's taught (outcomes, grading,
textbooks, topics). Catalog labels are sorted into one or the other by a single list in the code, so a new
school's label only needs one line added.

**Comparing schools.** Because syllabi are stored as sections rather than blocks of text, schools can be
compared: `cicmap syllabus search "reinforcement learning"` lists which schools teach a topic, and
`cicmap syllabus textbooks` lists textbooks used at more than one school (for example Russell & Norvig at
Drexel and Purdue).

### 7. The interfaces

- **CLI:** every step above has a command; see [CLI.md](CLI.md).
- **Web UI (Streamlit):** browse programs, their AI courses, and each course's details, prerequisites and
  combined syllabus; compare schools; add a program, which extracts it in the background. The UI only reads
  saved data, so browsing never triggers extraction.

### 8. Testing

87 automated tests run offline in about a second, using saved sample pages and fake DeepSeek answers. Every
new parser comes with a saved page and a test. Each change was committed separately once its tests passed.

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
| 11-19 | A syllabus is a set of source documents, each linked to one or more courses, combined per course | Hasnain's answer: data is uneven and from several sources; this keeps every fact tied to its source. |
| 11-19 | The catalog entry is the first syllabus source | Every course has one, and some catalogs list outcomes and hours. |
| 11-19 | Prerequisite loop check, run after every extraction | A loop is always an error; it found a real parser bug on its first run (Hallmark read the next course's code as a prerequisite). |
| 11-19 | Use public Simple Syllabus libraries (Purdue, Middle Georgia State) | Real, current syllabi from one predictable source. Their robots.txt allows general crawlers. |
| 11-19 | Skip Concourse (Capitol Tech, Harding) | Its robots.txt disallows all automated access. |
| 11-19 | Skip Kean's and Miami's Simple Syllabus | Both need a login. |
| 11-19 | Check robots.txt before fetching any syllabus page; never use the headless browser for syllabi | Respect what sites allow. The browser was acceptable for public catalog pages, not for getting around blocks on syllabus pages. |
| 11-19 | Brave Search limited to each school's domain, with three content checks | Open search returned other schools' courses; most domain results were degree maps and old catalogs. |
| 11-20 | Rules first, DeepSeek only for empty sections, with every item checked against the text | Rules are predictable and free; DeepSeek reads messy layouts the rules can't, and the checks stop invented data. |
| 11-20 | Grading weights kept only if they add up to about 100% | Caught a table counted twice (200%) and partial lists; also stops a grade scale ("A 90%") being read as weights. |
| 11-20 | Tried, then dropped, broader searches for the 9 schools with no syllabus | About 320 extra searches found no syllabus there, only assignment pages and course lists. |
| 11-21 | Open Syllabus not used | Its terms don't allow access to individual syllabi; a free student account only shows totals up to 2019. |
| 11-21 | Prerequisites written inside Purdue's syllabus descriptions are shown on the Prerequisites tab | Purdue's catalog lists no prerequisites, so this fills a real gap for 7 of its 11 AI courses. |
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

### Data quality

- **Textbooks with different titles aren't matched.** Middle Georgia State lists "ARTIFICIAL INTELLIGENCE
  (4TH 21 ed.)" and Purdue "Artificial Intelligence: A Modern Approach"; they are the same book but aren't
  matched. Matching on ISBN, where a syllabus gives one, would fix most cases.
- **Purdue's prerequisites from syllabi are shown as text, not links.** They appear on the Prerequisites tab
  but aren't turned into course-to-course links, so they don't show under "Needed for".
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

- **14 or 15 programs?** The brief mentions 15 pages; the sheet has 14.
- **How does cicmap handle courses with no public syllabus** (catalog only, collected by hand, or a dataset)?
  Asked of Hasnain; waiting for an answer.
- **API keys:** the DeepSeek and Brave keys were shared in chat and should be replaced after the trial.
