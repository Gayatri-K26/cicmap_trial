"""Text extraction from HTML, PDF and Word files (no network)."""
import io

from cicmap import documents


def test_html_text_drops_scripts_and_navigation():
    html = "<html><nav>Menu</nav><script>x()</script><h1>CS 380</h1><p>Grading: 40%</p></html>"
    assert documents.html_text(html) == "CS 380\nGrading: 40%"


def test_docx_text_includes_tables():
    import docx
    d = docx.Document()
    d.add_paragraph("CS 380 Syllabus")
    t = d.add_table(rows=1, cols=2)
    t.rows[0].cells[0].text, t.rows[0].cells[1].text = "Final exam", "35%"
    buf = io.BytesIO()
    d.save(buf)
    assert documents.docx_text(buf.getvalue()) == "CS 380 Syllabus\nFinal exam | 35%"


def _pdf(text: str) -> bytes:
    """A minimal one-page PDF with one line of text."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, xref)


def test_pdf_text():
    assert "CS 380 Syllabus" in documents.pdf_text(_pdf("CS 380 Syllabus"))


def test_page_links_absolute_and_once():
    from cicmap.documents import page_links
    html = ('<a href="syllabus.pdf">Syllabus</a> <a href="/~ab/">Home</a> <a href="syllabus.pdf#p2">again</a>'
            '<a href="mailto:x@y.edu">mail</a>')
    assert page_links("https://cs.x.edu/~ab/cs1/", html) == [
        ("Syllabus", "https://cs.x.edu/~ab/cs1/syllabus.pdf"), ("Home", "https://cs.x.edu/~ab/")]
