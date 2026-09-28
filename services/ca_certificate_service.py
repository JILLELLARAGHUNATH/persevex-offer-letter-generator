import re
from datetime import datetime
from pathlib import Path

import pymupdf
from tempfile import TemporaryDirectory


BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = BASE_DIR / "pdf_templates" / "campus_ambassador_certificate.pdf"


def safe_filename(name):
    value = re.sub(r"[^\w\s-]", "", str(name), flags=re.UNICODE).strip()
    return re.sub(r"\s+", "_", value) or "Participant"


def format_program_date(value):
    if not value:
        raise ValueError("Program date is required.")
    value = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(value, fmt).strftime("%d-%m-%Y")
        except ValueError:
            continue
    try:
        return datetime.strptime(value, "%d-%m-%Y").strftime("%d-%m-%Y")
    except ValueError as exc:
        raise ValueError("Program date must use YYYY-MM-DD.") from exc


def _extract_font(doc, font_names, output_path):
    for font in doc[0].get_fonts(full=True):
        if font[3].lower() not in {name.lower() for name in font_names}:
            continue
        extracted = doc.extract_font(font[0])
        if extracted and extracted[1].lower() in ("ttf", "otf"):
            output_path.write_bytes(extracted[3])
            return str(output_path)
    raise RuntimeError(f"Could not extract embedded font: {', '.join(font_names)}")


def _font_path(doc, names, output_dir):
    output = output_dir / (names[0].replace(" ", "_").lower() + ".ttf")
    if output.exists():
        return str(output)
    return _extract_font(doc, names, output)


def _text_span(page, text):
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                if span["text"] == text or text in span["text"]:
                    return span
    raise RuntimeError(f"CA Certificate text anchor was not found: {text}")


def generate_pdf(participant_name, program_date, output_path=None, font_dir=None):
    participant_name = str(participant_name or "").strip()
    if not participant_name:
        raise ValueError("Participant name is required.")
    formatted_date = format_program_date(program_date)
    if not TEMPLATE_PATH.exists():
        raise FileNotFoundError(f"CA Certificate template not found: {TEMPLATE_PATH}")

    output_path = Path(output_path) if output_path else None
    with pymupdf.open(TEMPLATE_PATH) as doc:
        page = doc[0]
        font_dir = Path(font_dir or TEMPLATE_PATH.parent)
        font_dir.mkdir(parents=True, exist_ok=True)
        name_font = _font_path(doc, ("Pinyon Script Regular", "pinyon"), font_dir)
        date_font = _font_path(doc, ("Raleway Light", "raleway"), font_dir)
        page.insert_font(fontname="CA_Pinyon", fontfile=name_font)
        page.insert_font(fontname="CA_Raleway", fontfile=date_font)

        name_rects = page.search_for("Rkl Anurag")
        date_rects = page.search_for("25-09-2026")
        if len(name_rects) != 1 or len(date_rects) != 1:
            raise RuntimeError("CA Certificate template anchors were not found exactly once.")

        name_rect = name_rects[0]
        date_rect = date_rects[0]
        name_span = _text_span(page, "Rkl Anurag")
        date_span = _text_span(page, "25-09-2026")
        # Transparent redactions remove only the original glyphs. A filled
        # redaction would paint over the underline and certificate artwork.
        page.add_redact_annot(name_rect, fill=None)
        page.add_redact_annot(date_rect, fill=None)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE)

        name_font_obj = pymupdf.Font(fontfile=name_font)
        size = 54.0
        while size > 20 and name_font_obj.text_length(participant_name, fontsize=size) > name_rect.width:
            size -= 0.25
        if size <= 20:
            raise ValueError("Participant name is too long for the certificate name area.")
        page.insert_text(
            tuple(name_span["origin"]),
            participant_name,
            fontname="CA_Pinyon",
            fontfile=name_font,
            fontsize=size,
            color=(0, 0, 0),
            overlay=True,
        )
        date_size = 14.0
        page.insert_text(
            (date_rect.x0, date_span["origin"][1]),
            formatted_date,
            fontname="CA_Raleway",
            fontfile=date_font,
            fontsize=date_size,
            color=(0, 0, 0),
            overlay=True,
        )
        if output_path:
            doc.save(output_path, garbage=4, deflate=True)
            return output_path.name
        return doc.tobytes(garbage=4, deflate=True), formatted_date


def generate_pdf_bytes(participant_name, program_date):
    """Generate a certificate entirely within the current request."""
    with TemporaryDirectory(prefix="persevex-ca-") as temp_dir:
        pdf_bytes, formatted_date = generate_pdf(
            participant_name,
            program_date,
            font_dir=Path(temp_dir),
        )
        return pdf_bytes, formatted_date
