# /// script
# requires-python = ">=3.12"
# dependencies = ["reportlab==5.0.1"]
# ///
"""Export the actual privacy page, keeping HTML and upload PDF in sync.

Run from any directory: uv run scripts/export_privacy_policy.py
Document dependencies are separate from the model/serving environment.
"""

from html import escape
from html.parser import HTMLParser
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "extension" / "privacy.html"
OUTPUT = ROOT / "output" / "pdf" / "comment-lens-privacy-policy.pdf"


class PolicyParser(HTMLParser):
    """Read visible policy paragraphs and links, excluding site navigation."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.inside_article = False
        self.blocks = []
        self.tag = None
        self.parts = []
        self.notice = False
        self.external_link = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "article":
            self.inside_article = True
        if not self.inside_article:
            return
        if tag == "section" and "data-pdf-page" in attributes:
            self.blocks.append(("page", "", False))
        elif tag in {"h1", "h2", "p", "li"}:
            self.tag = tag
            self.parts = []
            self.notice = "notice" in attributes.get("class", "").split()
        elif self.tag and tag in {"strong", "em"}:
            self.parts.append("<b>" if tag == "strong" else "<i>")
        elif self.tag and tag == "a":
            href = escape(attributes.get("href", ""), quote=True)
            self.external_link = href.startswith(("https://", "mailto:"))
            # Relative app links have no published HTTPS destination yet.
            self.parts.append(f'<a href="{href}" color="#5540c8"><u>' if self.external_link else "<u>")
        elif self.tag and tag == "br":
            self.parts.append("<br/>")

    def handle_endtag(self, tag):
        if tag == "article":
            self.inside_article = False
        elif self.tag and tag in {"strong", "em", "a"}:
            self.parts.append({"strong": "</b>", "em": "</i>",
                               "a": "</u></a>" if self.external_link else "</u>"}[tag])
        elif self.tag == tag:
            self.blocks.append((tag, "".join(self.parts).strip(), self.notice))
            self.tag = None
            self.parts = []

    def handle_data(self, data):
        if self.inside_article and self.tag:
            self.parts.append(escape(data))


def export_document(source, output, *, title, subject, footer):
    parser = PolicyParser()
    parser.feed(source.read_text(encoding="utf-8"))
    parser.close()
    if not parser.blocks or parser.blocks[0][0] != "h1":
        raise ValueError("The document article has no title")

    body = ParagraphStyle(
        "Body", fontName="Helvetica", fontSize=9.7, leading=13.5,
        textColor=colors.HexColor("#202736"), spaceAfter=8, alignment=TA_LEFT,
    )
    styles = {
        "h1": ParagraphStyle("Title", parent=body, fontName="Helvetica-Bold", fontSize=25, leading=30, spaceAfter=9),
        "h2": ParagraphStyle("Heading", parent=body, fontName="Helvetica-Bold", fontSize=12, leading=16,
                             spaceBefore=10, spaceAfter=6, keepWithNext=True),
        "p": body,
        "li": ParagraphStyle("Bullet", parent=body, leftIndent=12, bulletIndent=0, spaceAfter=5),
        "notice": ParagraphStyle("Notice", parent=body, backColor=colors.HexColor("#f1edff"),
                                borderPadding=9, spaceBefore=6, spaceAfter=12),
    }
    story = []
    for tag, text, notice in parser.blocks:
        if tag == "page":
            story.append(PageBreak())
        else:
            story.append(Paragraph(text, styles["notice"] if notice else styles[tag],
                                   bulletText="-" if tag == "li" else None))

    def page_furniture(canvas, document):
        width, height = A4
        canvas.saveState()
        canvas.setTitle(title)
        canvas.setAuthor("Siddhartha Ganguli")
        canvas.setSubject(subject)
        canvas.setFont("Helvetica-Bold", 8)
        canvas.setFillColor(colors.HexColor("#5540c8"))
        canvas.drawString(43, height - 30, "COMMENT LENS")
        canvas.setStrokeColor(colors.HexColor("#e4e7ee"))
        canvas.line(43, 36, width - 43, 36)
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor("#586478"))
        canvas.drawString(43, 24, footer)
        canvas.drawRightString(width - 43, 24, f"Page {document.page}")
        canvas.restoreState()

    output.parent.mkdir(parents=True, exist_ok=True)
    document = SimpleDocTemplate(
        str(output), pagesize=A4, leftMargin=43, rightMargin=43,
        topMargin=49, bottomMargin=48,
    )
    document.build(story, onFirstPage=page_furniture, onLaterPages=page_furniture)
    print(f"{title} PDF written to {output}")


def export_policy():
    export_document(
        SOURCE, OUTPUT, title="Comment Lens Privacy Policy",
        subject="Current preview and planned live YouTube comment analysis",
        footer="Privacy Policy | Development preview | 14 October 2026",
    )


if __name__ == "__main__":
    export_policy()
