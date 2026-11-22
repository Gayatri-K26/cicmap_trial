"""cicmap web UI (Streamlit). Run: .venv/bin/python -m cicmap ui

Pages:
  * Browse: every tracked program, its cached AI courses, one course's details, prerequisites and syllabi.
    Reads the database only; nothing is re-extracted.
  * Add program: track a new university program and extract it. A program already extracted is shown
    from the cache instead of being extracted again.
"""
import re
import sys
import threading
from pathlib import Path
from typing import Dict

import pandas as pd
import streamlit as st

if __package__ in (None, ""):  # `streamlit run cicmap/ui.py` runs this file as a plain script
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cicmap import db, queries, sheet  # noqa: E402
from cicmap.browser import BrowserSession  # noqa: E402
from cicmap.pipeline import extract_source  # noqa: E402

STATUS_ICON = {"extracted": "✅", "pending": "⏳", "failed": "❌", "needs_parser": "🧩", "blocked": "🚫"}

st.set_page_config(page_title="cicmap", page_icon="🎓", layout="wide")
conn = db.connect()


def select_source(source_id: int) -> None:
    st.session_state["source_id"] = source_id
    st.session_state.pop("course_id", None)


def go_to_source(source_id: int) -> None:
    """Button callback (runs before the next rerun, so it may switch the page widget)."""
    select_source(source_id)
    st.session_state["page"] = "Browse"


@st.cache_resource
def jobs() -> Dict[int, dict]:
    """Extraction jobs by source id, shared across reruns and browser tabs of this server."""
    return {}


def start_extraction(source_id: int, refresh: bool = False) -> None:
    """Run the extraction in a background thread, so clicking around the UI doesn't interrupt it
    (Streamlit stops the running script on every click)."""
    running = jobs().get(source_id)
    if running and running["thread"].is_alive():
        return
    job = {"log": [], "result": None, "error": None, "seen": False}

    def work():
        c = db.connect()  # own connection: SQLite connections don't cross threads
        browser = BrowserSession()  # only starts Chromium if a page turns out to be a bot check
        try:
            job["result"] = extract_source(c, source_id, refresh=refresh, browser=browser,
                                           progress=job["log"].append)
        except Exception as e:  # extract_source records its own failures; this is a last resort
            job["error"] = f"{type(e).__name__}: {e}"
        finally:
            browser.close()
            c.close()

    job["thread"] = threading.Thread(target=work, name=f"extract-{source_id}", daemon=True)
    jobs()[source_id] = job
    job["thread"].start()


@st.fragment(run_every="2s")
def jobs_panel() -> None:
    """Live progress of extraction jobs. Refreshes itself; reloads the page once when a job finishes."""
    finished_now = False
    fconn = db.connect()  # fragment reruns run on another thread than the page's connection
    for sid, job in list(jobs().items()):
        src = queries.source(fconn, sid)
        name = f"{src['institution']}: {src['program']}" if src else f"source {sid}"
        alive = job["thread"].is_alive()
        res = job["result"]
        if alive:
            label, state = f"Extracting {name}…", "running"
        elif res and res.status == "extracted":
            label, state = f"{name}: {res.message}", "complete"
        else:
            label, state = f"{name}: {res.status + ': ' + res.message if res else job['error']}", "error"
        with st.status(label, state=state, expanded=alive):
            for line in job["log"]:
                st.write(line)
        if not alive:  # outside the status box, which collapses when the job ends
            c1, c2, _ = st.columns([1, 1, 8])
            # A click inside a fragment reruns only the fragment, so rerun the whole page explicitly.
            if c1.button("View", key=f"view{sid}", type="primary"):
                go_to_source(sid)
                st.rerun(scope="app")
            if c2.button("Dismiss", key=f"dismiss{sid}"):
                jobs().pop(sid, None)
                st.rerun(scope="app")
        if not alive and not job["seen"]:
            job["seen"] = finished_now = True
    fconn.close()
    if finished_now:
        st.rerun(scope="app")  # refresh counts and tables with the new data


# --- sidebar -------------------------------------------------------------------------------------

with st.sidebar:
    st.title("cicmap")
    st.caption("AI courses and syllabi from university catalogs")
    page = st.radio("Page", ["Browse", "Compare schools", "Add program"], key="page",
                    label_visibility="collapsed")
    t = queries.totals(conn)
    c1, c2 = st.columns(2)
    c1.metric("Programs", f"{t['extracted']}/{t['programs']}")
    c2.metric("AI courses", t["ai_courses"])
    c1.metric("Prereq links", t["prereq_links"])
    c2.metric("With syllabus", t["courses_with_syllabus"],
              help=f"AI courses with at least one real syllabus ({t['syllabi']} documents); catalog entries "
                   "are not counted.")
    st.divider()
    if st.button("Sync from Google Sheet", width="stretch"):
        res = sheet.sync(conn, force=True)
        st.success(res.summary())
        if res.added or res.changed:
            st.info("New or changed programs are pending: extract them from Add program.")


# --- browse ----------------------------------------------------------------------------------------

def browse_page():
    srcs = queries.sources(conn)
    if not srcs:
        st.info("No programs yet. Sync from the Google Sheet or add one.")
        return

    st.header("Programs")
    st.caption("Click a row to see its AI courses.")
    overview = pd.DataFrame([{
        "Institution": s["institution"], "Program": s["program"],
        "Status": f"{STATUS_ICON.get(s['status'], '')} {s['status']}", "AI courses": s["ai_courses"],
        "Parser": s["adapter"] or "", "Last extracted": s["last_extracted_at"] or "",
    } for s in srcs])
    pick = st.dataframe(overview, hide_index=True, width="stretch", on_select="rerun",
                        selection_mode="single-row", key="programs_table", height=35 * (len(srcs) + 1) + 3)
    # React only when the table selection changes, so a "View" button elsewhere isn't overridden by a stale row.
    picked = srcs[pick.selection.rows[0]]["id"] if pick.selection.rows else None
    if picked != st.session_state.get("last_table_pick"):
        st.session_state["last_table_pick"] = picked
        if picked:
            select_source(picked)
    src = queries.source(conn, st.session_state.get("source_id") or srcs[0]["id"])
    if not src:
        return

    st.divider()
    st.subheader(f"{src['institution']}: {src['program']}")
    if not src["program_id"]:
        st.warning(f"Not extracted yet (status: {src['status']}). {src['last_error'] or ''}")
        st.button("Extract now", on_click=start_extraction, args=(src["id"],))
        return
    p = queries.program(conn, src["program_id"])
    meta = {"Degree": p["degree_full"] or p["degree"], "College": p["college"], "Department": p["department"],
            "Catalog year": p["catalog_year"], "Total credits": p["total_credits"],
            "Parser": f"{p['adapter']} v{p['adapter_version']}", "Extracted": p["extracted_at"]}
    items = list(meta.items())
    for row in (items[:4], items[4:]):
        for col, (k, v) in zip(st.columns(4), row):
            col.markdown(f"**{k}**  \n{v or '—'}")
    st.markdown(f"[Catalog page]({src['catalog_url']})")
    if src["status"] != "extracted":
        st.warning(f"Last extraction attempt: {src['status']}. {src['last_error'] or ''} Showing earlier data.")

    courses = queries.ai_courses(conn, p["id"])
    st.markdown(f"#### AI courses ({len(courses)})")
    st.caption("Cached data. Click a row to see the course.")
    table = pd.DataFrame([{
        "Code": c["code"], "Title": c["title"], "Credits": c["credits"] or "", "Category": c["category"] or "",
        "Confidence": c["confidence"], "Requirement": c["requirement_group"] or "", "Syllabi": c["syllabi"],
    } for c in courses])
    pick = st.dataframe(table, hide_index=True, width="stretch", on_select="rerun",
                        selection_mode="single-row", key=f"courses_{p['id']}",
                        column_config={"Confidence": st.column_config.ProgressColumn(
                            "AI confidence", min_value=0.0, max_value=1.0, format="%.2f")})
    if pick.selection.rows:
        st.session_state["course_id"] = courses[pick.selection.rows[0]]["id"]
    course_id = st.session_state.get("course_id")
    if course_id in {c["id"] for c in courses}:
        st.divider()
        course_panel(course_id)


def course_panel(course_id: int):
    c = queries.course(conn, course_id)
    st.subheader(f"{c['code']}: {c['title']}")
    st.caption(f"{c['credits_text'] or '?'} credits · {c['institution']}"
               + (f" · [course page]({c['source_url']})" if c["source_url"] else ""))
    details, prereqs, syll = st.tabs(["Details", "Prerequisites", f"Syllabi ({len(queries.syllabi(conn, course_id))})"])

    with details:
        st.markdown(c["description"] or "_No description on the catalog page._")
        st.markdown(f"**AI category:** {(c['ai_category'] or 'not set').replace('_', ' ').capitalize()}")
        if c["restrictions"]:
            st.markdown(f"**Restrictions:** {c['restrictions']}")
        offered = "; ".join(c["extra"][k] for k in ("When Taught", "Term Offered") if c["extra"].get(k))
        if offered:
            st.markdown(f"**Terms offered:** {offered}")
        other = {k: v for k, v in c["extra"].items() if k not in ("When Taught", "Term Offered")}
        if other:
            with st.expander("Other catalog info"):
                for k, v in other.items():
                    st.markdown(f"**{k}:** {v}")
        with st.expander("Why this counts as an AI course"):
            conf = f"{c['ai_confidence']:.2f}" if c["ai_confidence"] is not None else "—"
            decided = "DeepSeek" if c["ai_method"] == "llm" else "keyword match"
            st.markdown(f"{c['ai_reason'] or '—'}  \n<small>Decided by {decided} · confidence {conf}</small>",
                        unsafe_allow_html=True)

    with prereqs:
        requirement_lines("Requires", c["prereqs"], c["prereq_text"])
        if c["coreqs"] or c["coreq_text"]:
            requirement_lines("Take at the same time", c["coreqs"], c["coreq_text"])
        st.markdown("**Needed for:** " + (", ".join(c["required_by"]) or "no other extracted course"))

    with syll:
        syllabi_view(course_id)


def _plain(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def requirement_lines(label: str, groups, written) -> None:
    """'Requires: CS 101 or CS 102, and MATH 120', plus the catalog wording only when it says more."""
    if groups:
        many = len(groups) > 1
        readable = ", and ".join(f"({' or '.join(g)})" if many and len(g) > 1 else " or ".join(g) for g in groups)
    else:
        readable = written or "none listed"
    st.markdown(f"**{label}:** {readable}")
    if groups and written and _plain(written) != _plain(readable):
        st.caption(f"Catalog wording: {written}")


SECTION_LABELS = {"summary": "Summary", "outcomes": "Learning outcomes", "topics": "Weekly topics",
                  "assessments": "Grading", "materials": "Textbooks and materials", "instructor": "Instructor",
                  "contact_hours": "Contact hours", "grading_scale": "Grading scale"}


def syllabi_view(course_id: int):
    p = queries.syllabus_profile(conn, course_id)
    items = p["documents"]
    if not items:
        st.info("No syllabus documents for this course yet.")
        return
    st.markdown("#### Combined syllabus")
    st.caption("Each section comes from the best source that has it. Real syllabi rank above the catalog entry.")
    for name, sec in p["sections"].items():
        src = sec["source"]
        when = " ".join(str(x) for x in (src["term"], src["year"]) if x)
        st.markdown(f"**{SECTION_LABELS[name]}** <small>· from [{src['source_kind']}{' ' + when if when else ''}]"
                    f"({src['source_url']})</small>", unsafe_allow_html=True)
        v = sec["value"]
        if name == "outcomes":
            st.markdown("\n".join(f"{i}. {o}" for i, o in enumerate(v, 1)))
        elif isinstance(v, list):
            st.dataframe(pd.DataFrame(v), hide_index=True, width="stretch")
        else:
            st.markdown(str(v))
    missing = [SECTION_LABELS[n] for n in p["missing"]]
    if missing:
        st.caption("Not found in any source: " + ", ".join(missing))

    st.markdown(f"#### Source documents ({len(items)})")
    for s in items:
        label = " ".join(str(x) for x in (s["source_kind"], s["term"], s["year"], s["section"]) if x)
        with st.expander(label + (f" · {s['instructor']}" if s["instructor"] else "")):
            st.markdown(f"[Source]({s['source_url']}) · {s['source_type'] or ''} · "
                        f"match confidence {s['match_confidence'] or '—'} · {s['delivery_mode'] or ''}")
            if s["summary"]:
                st.markdown(s["summary"])
            if s["outcomes"]:
                st.markdown("**Learning outcomes**")
                st.markdown("\n".join(f"{i}. {o}" for i, o in enumerate(s["outcomes"], 1)))
            if s["topics"]:
                st.markdown("**Schedule**")
                st.dataframe(pd.DataFrame(s["topics"]), hide_index=True, width="stretch")
            if s["assessments"]:
                st.markdown("**Assessments**")
                st.dataframe(pd.DataFrame(s["assessments"]), hide_index=True, width="stretch")
            if s["materials"]:
                st.markdown("**Materials**")
                st.dataframe(pd.DataFrame(s["materials"]), hide_index=True, width="stretch")
            if s["grading_scale"]:
                st.markdown(f"**Grading scale:** {s['grading_scale']}")


# --- compare schools --------------------------------------------------------------------------------

def compare_page():
    st.header("Compare schools")
    st.caption("Searches AI course titles, summaries, learning outcomes and weekly topics across every school.")
    text = st.text_input("Which schools teach…", placeholder="reinforcement learning")
    if text.strip():
        found = queries.search_syllabi(conn, text)
        total = len({s["institution"] for s in queries.sources(conn) if s["ai_courses"]})
        st.markdown(f"**{len(found)} of {total} schools** have an AI course mentioning “{text.strip()}”.")
        if found:
            st.dataframe(pd.DataFrame([{
                "School": s["institution"], "Courses": len(s["courses"]),
                "Which": ", ".join(f"{c['code']} {c['title'] or ''}".strip() for c in s["courses"]),
                "Found in": ", ".join(sorted({f for c in s["courses"] for f in c["found_in"]})),
            } for s in found]), hide_index=True, width="stretch")

    st.markdown("#### Textbooks used at more than one school")
    common = queries.common_materials(conn)
    if not common:
        st.info("None yet. Catalogs don't list textbooks; syllabi found on the web will.")
    else:
        st.dataframe(pd.DataFrame([{
            "Title": m["title"], "Authors": m["authors"] or "", "Schools": m["school_count"],
            "Where": "; ".join(f"{k}: {', '.join(v)}" for k, v in m["schools"].items()),
        } for m in common]), hide_index=True, width="stretch")


# --- add program ------------------------------------------------------------------------------------

def add_page():
    st.header("Add a program")
    st.caption("Paste the program's catalog page. If it was already extracted, the cached result is shown "
               "instead of extracting again.")
    with st.form("add"):
        inst = st.text_input("University", placeholder="Northeastern University")
        prog = st.text_input("Program", placeholder="Bachelor of Science in Artificial Intelligence")
        url = st.text_input("Catalog URL", placeholder="https://catalog.example.edu/...")
        refresh = st.checkbox("Re-extract even if cached (re-fetches the pages)")
        submitted = st.form_submit_button("Add and extract", type="primary")
    if submitted:
        if not (inst.strip() and prog.strip() and url.strip().startswith("http")):
            st.error("Fill in the university, the program and a full http(s) URL.")
            return
        sid, existed = queries.add_source(conn, inst, prog, url)
        src = queries.source(conn, sid)
        if existed and src["status"] == "extracted" and not refresh:
            st.success(f"Already extracted: {src['institution']}, {src['ai_courses']} AI courses (cached).")
            st.button("View it", on_click=go_to_source, args=(sid,), type="primary")
        else:
            start_extraction(sid, refresh=refresh)
            st.info("Extraction started. It keeps running if you browse elsewhere.")

    busy = {sid for sid, j in jobs().items() if j["thread"].is_alive()}
    pending = [s for s in queries.sources(conn) if s["status"] != "extracted" and s["id"] not in busy]
    if pending:
        st.divider()
        st.markdown("#### Not extracted yet")
        for s in pending:
            c1, c2 = st.columns([4, 1])
            c1.markdown(f"{STATUS_ICON.get(s['status'], '')} **{s['institution']}**: {s['program']}  \n"
                        f"<small>{s['status']} · {s['last_error'] or ''}</small>", unsafe_allow_html=True)
            c2.button("Extract", key=f"x{s['id']}", on_click=start_extraction, args=(s["id"],))


jobs_slot = st.container()  # at the top of the page, filled after the page (which may start a job)
if page == "Browse":
    browse_page()
elif page == "Compare schools":
    compare_page()
else:
    add_page()
if jobs():
    with jobs_slot:
        jobs_panel()
