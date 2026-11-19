"""cicmap command-line interface. Run `python -m cicmap --help`."""
import logging
from typing import List, Optional

import typer

from . import db, queries, sheet, syllabi
from .adapters import all_adapters
from .browser import BrowserSession
from .pipeline import extract_source

app = typer.Typer(help="Extract AI courses and syllabi from university catalogs.", no_args_is_help=True,
                  add_completion=False)


def _conn():
    return db.connect()


def _table(rows: List[dict], cols: List[str]) -> None:
    if not rows:
        typer.echo("(none)")
        return
    widths = {c: min(60, max(len(c), *(len(str(r.get(c) if r.get(c) is not None else "")) for r in rows))) for c in cols}
    typer.echo("  ".join(c.ljust(widths[c]) for c in cols))
    typer.echo("  ".join("-" * widths[c] for c in cols))
    for r in rows:
        typer.echo("  ".join(str(r.get(c) if r.get(c) is not None else "")[:widths[c]].ljust(widths[c]) for c in cols))


@app.command("sync")
def sync_cmd(force: bool = typer.Option(False, "--force", help="Ignore the TTL and re-download the sheet.")):
    """Sync the program list from the Google Sheet (skipped if synced recently or unchanged)."""
    conn = _conn()
    res = sheet.sync(conn, force=force)
    typer.echo(res.summary())
    for label, items in (("Added", res.added), ("Changed", res.changed), ("Removed", res.removed)):
        for i in items:
            typer.echo(f"  {label}: {i}")


@app.command("add")
def add_cmd(institution: str, program: str, url: str):
    """Add a program by hand (not from the sheet)."""
    sid, existed = queries.add_source(_conn(), institution, program, url)
    if existed:
        typer.echo(f"Already tracked as source {sid}")
        raise typer.Exit()
    typer.echo(f"Added source {sid}. Run: python -m cicmap extract {sid}")


@app.command("sources")
def sources_cmd(all_: bool = typer.Option(False, "--all", help="Include sources removed from the sheet.")):
    """List programs to extract and their status."""
    conn = _conn()
    q = ("SELECT s.id, s.institution, s.program, s.origin, s.status, s.active, s.last_extracted_at, "
         "(SELECT COUNT(*) FROM programs p JOIN program_courses pc ON pc.program_id = p.id "
         " WHERE p.source_id = s.id) AS ai_courses FROM program_sources s "
         + ("" if all_ else "WHERE s.active = 1 ") + "ORDER BY s.institution")
    _table([dict(r) for r in conn.execute(q)],
           ["id", "institution", "program", "origin", "status", "ai_courses", "last_extracted_at"])


@app.command("extract")
def extract_cmd(
    ids: Optional[List[int]] = typer.Argument(None, help="Source ids (see `sources`)."),
    pending: bool = typer.Option(False, "--pending", help="Extract every source not yet extracted."),
    all_: bool = typer.Option(False, "--all", help="Re-extract every active source."),
    no_llm: bool = typer.Option(False, "--no-llm", help="Keyword-only AI filter and regex prerequisites."),
    refresh: bool = typer.Option(False, "--refresh", help="Re-fetch pages instead of using the cache."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Never fall back to the headless browser."),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Run extraction. Pages and LLM answers are cached, so re-runs are fast and free."""
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    conn = _conn()
    if all_:
        ids = [r["id"] for r in conn.execute("SELECT id FROM program_sources WHERE active = 1 ORDER BY id")]
    elif pending:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM program_sources WHERE active = 1 AND status != 'extracted' ORDER BY id")]
    if not ids:
        typer.echo("Nothing to extract. Pass source ids, --pending or --all.")
        raise typer.Exit()
    browser = None if no_browser else BrowserSession()  # started lazily, only if a page is blocked
    try:
        _extract_many(conn, ids, not no_llm, refresh, not no_browser, browser, verbose)
    finally:
        if browser:
            browser.close()


def _extract_many(conn, ids, use_llm, refresh, use_browser, browser, verbose):
    for sid in ids:
        name = conn.execute("SELECT institution FROM program_sources WHERE id = ?", (sid,)).fetchone()
        typer.echo(f"\n[{sid}] {name['institution'] if name else '?'}")
        res = extract_source(conn, sid, use_llm=use_llm, refresh=refresh, use_browser=use_browser,
                             browser=browser, progress=lambda m: typer.echo(f"    {m}") if verbose else None)
        typer.echo(f"    {res.status.upper()}: {res.message}  (pages fetched {res.pages_fetched}, "
                   f"via browser {res.browser_fetches}, LLM calls {res.llm_calls})")


@app.command("courses")
def courses_cmd(school: str = typer.Argument(..., help="Part of an institution name, e.g. 'drexel'.")):
    """Show cached AI courses for a school (no extraction)."""
    conn = _conn()
    rows = conn.execute(
        "SELECT c.code, c.title, c.credits_text AS credits, c.ai_category AS category, "
        "ROUND(c.ai_confidence, 2) AS conf, pc.requirement_group AS requirement, i.name AS school "
        "FROM courses c JOIN institutions i ON i.id = c.institution_id "
        "JOIN program_courses pc ON pc.course_id = c.id "
        "WHERE i.name LIKE ? AND c.is_ai = 1 ORDER BY i.name, c.code", (f"%{school}%",)).fetchall()
    _table([dict(r) for r in rows], ["school", "code", "title", "credits", "category", "conf", "requirement"])


@app.command("course")
def course_cmd(school: str, code: str):
    """Show one course with its prerequisites and the courses that require it."""
    c = queries.find_course(_conn(), school, code)
    if not c:
        typer.echo("Not found.")
        raise typer.Exit(1)
    typer.echo(f"{c['institution']}  {c['code']}  {c['title']}  ({c['credits_text'] or '?'} credits)")
    typer.echo(f"AI: {bool(c['is_ai'])}  category={c['ai_category']}  confidence={c['ai_confidence']}  "
               f"[{c['ai_method']}] {c['ai_reason'] or ''}")
    typer.echo(f"\n{c['description'] or '(no description)'}\n")
    typer.echo(f"Prerequisite text: {c['prereq_text'] or '-'}")
    if c["prereqs"]:
        typer.echo("Parsed prereqs: " + queries.format_groups(c["prereqs"]))
    if c["coreqs"]:
        typer.echo("Parsed coreqs: " + queries.format_groups(c["coreqs"]))
    typer.echo(f"Required by: {', '.join(c['required_by']) or '-'}")


@app.command("stats")
def stats_cmd():
    """Counts per school."""
    conn = _conn()
    rows = conn.execute(
        "SELECT i.name AS school, p.degree, p.college, p.adapter, COUNT(pc.course_id) AS ai_courses, "
        "(SELECT COUNT(*) FROM course_prereqs cp JOIN program_courses x ON x.course_id = cp.course_id "
        " WHERE x.program_id = p.id) AS prereq_edges "
        "FROM programs p JOIN institutions i ON i.id = p.institution_id "
        "LEFT JOIN program_courses pc ON pc.program_id = p.id GROUP BY p.id ORDER BY i.name").fetchall()
    _table([dict(r) for r in rows], ["school", "degree", "adapter", "ai_courses", "prereq_edges", "college"])


@app.command("parsers")
def parsers_cmd():
    """List registered catalog parsers."""
    for a in all_adapters():
        typer.echo(f"{a.name} v{a.version}  domains={', '.join(a.domains) or '-'}")


syllabus_app = typer.Typer(help="Syllabus documents: load, discover and show.", no_args_is_help=True)
app.add_typer(syllabus_app, name="syllabus")


@syllabus_app.command("catalog")
def syllabus_catalog_cmd():
    """Store every AI course's catalog entry (description, outcomes, hours) as a syllabus document.
    New extractions do this automatically; run it once for courses extracted earlier."""
    conn = _conn()
    n = syllabi.load_catalog(conn)
    conn.commit()
    typer.echo(f"Saved {n} catalog syllabus documents.")


SECTION_LABELS = {"summary": "Summary", "outcomes": "Learning outcomes", "topics": "Weekly topics",
                  "assessments": "Grading", "materials": "Textbooks and materials", "instructor": "Instructor",
                  "contact_hours": "Contact hours", "grading_scale": "Grading scale"}


def _source_label(src: dict) -> str:
    when = " ".join(str(x) for x in (src["term"], src["year"]) if x)
    return f"{src['source_kind']}{' ' + when if when else ''}: {src['source_url']}"


@syllabus_app.command("show")
def syllabus_show_cmd(school: str, code: str):
    """Show a course's combined syllabus: each section with the document it came from."""
    conn = _conn()
    c = queries.find_course(conn, school, code)
    if not c:
        typer.echo("Not found.")
        raise typer.Exit(1)
    p = queries.syllabus_profile(conn, c["id"])
    typer.echo(f"{c['institution']}  {c['code']}  {c['title']}  ({len(p['documents'])} source documents)\n")
    for name, sec in p["sections"].items():
        typer.echo(f"{SECTION_LABELS[name]}  [from {_source_label(sec['source'])}]")
        v = sec["value"]
        if name == "outcomes":
            lines = [f"{i}. {o}" for i, o in enumerate(v, 1)]
        elif name == "topics":
            lines = [f"{t['week'] or '-'}: {t['title']}" for t in v]
        elif name == "assessments":
            lines = [f"{a['name']}: {a['weight_pct']:g}%" if a["weight_pct"] is not None else a["name"] for a in v]
        elif name == "materials":
            lines = [m["title"] + (f" ({m['authors']})" if m["authors"] else "") for m in v]
        else:
            lines = [str(v)]
        for line in lines:
            typer.echo(f"    {line}")
    typer.echo("\nNot found: " + (", ".join(SECTION_LABELS[n] for n in p["missing"]) or "-"))


@syllabus_app.command("search")
def syllabus_search_cmd(text: str = typer.Argument(..., help="A topic, e.g. 'reinforcement learning'.")):
    """Which schools teach a topic (searches AI course titles, summaries, outcomes and weekly topics)."""
    found = queries.search_syllabi(_conn(), text)
    typer.echo(f"'{text}': {len(found)} schools\n")
    for s in found:
        typer.echo(s["institution"])
        for c in s["courses"]:
            typer.echo(f"    {c['code']}  {c['title'] or ''}  (in {', '.join(c['found_in'])})")


@syllabus_app.command("textbooks")
def syllabus_textbooks_cmd(min_schools: int = typer.Option(2, help="Used at this many schools or more.")):
    """Textbooks and materials shared across schools."""
    common = queries.common_materials(_conn(), min_schools)
    if not common:
        typer.echo(f"No material is listed by {min_schools}+ schools yet (catalogs don't list textbooks; "
                   "syllabi found on the web will).")
    for m in common:
        typer.echo(f"{m['school_count']} schools  {m['title']}" + (f"  ({m['authors']})" if m["authors"] else ""))
        for school, codes in m["schools"].items():
            typer.echo(f"    {school}: {', '.join(codes)}")


@app.command("ui")
def ui_cmd(port: int = typer.Option(8501, help="Port for the web UI.")):
    """Open the web UI (Streamlit) in the browser."""
    import subprocess
    import sys
    from pathlib import Path

    app_path = Path(__file__).with_name("ui.py")
    raise typer.Exit(subprocess.call([sys.executable, "-m", "streamlit", "run", str(app_path),
                                      "--server.port", str(port), "--browser.gatherUsageStats", "false"]))


if __name__ == "__main__":
    app()
