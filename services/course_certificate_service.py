import io
import re
import os
import secrets
import string
import sqlite3
import smtplib
import imaplib
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from email.message import EmailMessage

import pymupdf
import qrcode
from dotenv import load_dotenv

from email_utils import format_to_header

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATE_DIR = BASE_DIR / "pdf_templates"
TEMPLATE_PATH = TEMPLATE_DIR / "Course Completion Certificate.pdf"

# Email Configuration from environment
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "").strip()
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD", "").strip()
SMTP_HOST = "smtpout.secureserver.net"
SMTP_PORT = 465
IMAP_HOST = "imap.secureserver.net"
IMAP_PORT = 993
IMAP_SENT_FOLDER = "Sent"

# Local SQLite fallback database path
SQLITE_DB_PATH = BASE_DIR / "certificates_local.db"


def safe_filename(name):
    name = re.sub(r"[^\w\s-]", "", str(name), flags=re.UNICODE)
    name = name.strip()
    name = re.sub(r"\s+", "_", name)
    return name or "Student"


def format_display_date(date_str):
    """Format YYYY-MM-DD to DD-MM-YYYY if needed, or preserve existing."""
    if not date_str:
        return ""
    date_str = str(date_str).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
        try:
            return datetime.strptime(date_str, "%Y-%m-%d").strftime("%d-%m-%Y")
        except Exception:
            return date_str
    return date_str


def get_public_base_url(request=None):
    from certificate_service import get_public_base_url as _get_base
    return _get_base(request)


def get_verification_url(certificate_id, request=None, base_url=None):
    from certificate_service import get_verification_url as _get_url
    return _get_url(certificate_id, request=request, base_url=base_url)


def generate_certificate_id(supabase_client=None):
    """
    Generate unique Course Completion Certificate ID in format: PXL-CCC-YYYY-XXXXXX
    Ensures internal uniqueness and no collision with database.
    """
    current_year = datetime.now().year
    alphabet = string.ascii_uppercase + string.digits
    from database import repository

    for _ in range(50):
        random_suffix = "".join(secrets.choice(alphabet) for _ in range(6))
        candidate = f"PXL-CCC-{current_year}-{random_suffix}"
        # Check collision against existing records
        existing = repository.get_certificate_by_id(candidate, supabase_client=supabase_client)
        if not existing:
            return candidate

    raise RuntimeError("Failed to generate a unique Course Completion Certificate ID after multiple attempts.")


def _format_course_issue_date(value):
    """Format an ISO or DD-MM-YYYY date to match the new certificate artwork."""
    raw = str(value or "").strip()
    if not raw:
        return datetime.now().strftime("%d %B, %Y")
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%d %B, %Y")
        except ValueError:
            pass
    return raw


def _wrap_course_certificate_text(text, fontname, fontsize, max_width):
    """Wrap certificate copy without changing its wording."""
    words = text.split()
    lines = []
    current = []
    for word in words:
        candidate = " ".join(current + [word])
        if pymupdf.get_text_length(candidate, fontname=fontname, fontsize=fontsize) <= max_width:
            current.append(word)
        else:
            if current:
                lines.append(" ".join(current))
            current = [word]
    if current:
        lines.append(" ".join(current))
    return lines


def generate_certificate_pdf_bytes(data, base_url=None):
    """
    Generate a Course Completion Certificate using the approved original artwork.
    Only the student name, course description, issue date and unique QR are dynamic.
    All branding, logos, stamp and administrator signature remain from the PDF.
    """
    if not TEMPLATE_PATH.exists():
        raise FileNotFoundError(f"Course Completion Certificate template not found at {TEMPLATE_PATH}")

    student_name = str(data.get("student_name", "")).strip() or "Student Name"
    course_name = str(data.get("course_name") or data.get("domain") or "Course").strip()
    issued_date = _format_course_issue_date(data.get("issued_date") or data.get("issue_date"))

    cert_id = str(data.get("certificate_id", "")).strip()
    if not cert_id:
        cert_id = generate_certificate_id()

    resolved_base_url = (base_url or get_public_base_url()).rstrip("/")
    doc = pymupdf.open(str(TEMPLATE_PATH))
    page = doc[0]
    page_width = page.rect.width
    content_center_x = page_width / 2

    # Remove only the sample text. Image and vector artwork are deliberately retained.
    text_redactions = (
        pymupdf.Rect(265, 375, 850, 487),  # sample candidate name
        pymupdf.Rect(135, 505, 985, 575),  # sample course description
        pymupdf.Rect(440, 610, 680, 645),  # sample issue date
    )
    for rect in text_redactions:
        page.add_redact_annot(rect, fill=None)
    page.apply_redactions(
        images=pymupdf.PDF_REDACT_IMAGE_NONE,
        graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
        text=pymupdf.PDF_REDACT_TEXT_REMOVE,
    )

    # Candidate name: centered at the original baseline and styled like the supplied artwork.
    name_font = "tiro"
    name_fontsize = 86.16
    max_name_width = 850.0
    name_width = pymupdf.get_text_length(student_name, fontname=name_font, fontsize=name_fontsize)
    if name_width > max_name_width:
        name_fontsize = max(40.0, name_fontsize * max_name_width / name_width)
        name_width = pymupdf.get_text_length(student_name, fontname=name_font, fontsize=name_fontsize)
    page.insert_text(
        pymupdf.Point(content_center_x - name_width / 2, 459.75),
        student_name,
        fontname=name_font,
        fontsize=name_fontsize,
        color=(0.0196, 0.0392, 0.1882),
    )

    # Course description: preserve the original sentence and wrap long course names safely.
    description = (
        "This is to certify that the candidate has successfully completed the "
        f"{course_name} course at Persevex, demonstrating strong commitment and "
        "competence throughout the program."
    )
    body_font = "tiro"
    body_fontsize = 19.815
    max_body_width = 860.0
    lines = _wrap_course_certificate_text(description, body_font, body_fontsize, max_body_width)
    if len(lines) > 3:
        body_fontsize = 17.0
        lines = _wrap_course_certificate_text(description, body_font, body_fontsize, max_body_width)
    if len(lines) > 4:
        body_fontsize = 15.5
        lines = _wrap_course_certificate_text(description, body_font, body_fontsize, max_body_width)
    if len(lines) > 4:
        raise ValueError("Course name is too long to fit on the certificate. Please shorten it.")

    baseline_sets = {
        1: [546.0],
        2: [532.5, 560.25],
        3: [522.0, 546.0, 570.0],
        4: [516.0, 536.0, 556.0, 576.0],
    }
    baselines = baseline_sets[len(lines)]
    for line, baseline_y in zip(lines, baselines):
        line_width = pymupdf.get_text_length(line, fontname=body_font, fontsize=body_fontsize)
        page.insert_text(
            pymupdf.Point(content_center_x - line_width / 2, baseline_y),
            line,
            fontname=body_font,
            fontsize=body_fontsize,
            color=(0.0196, 0.0392, 0.1882),
        )

    # Issue date: retain the uppercase label and centered layout from the new artwork.
    issued_text = f"ISSUED DATE: {issued_date}"
    date_fontsize = 15.51
    date_width = pymupdf.get_text_length(issued_text, fontname=body_font, fontsize=date_fontsize)
    if date_width > 500:
        date_fontsize = max(11.0, date_fontsize * 500 / date_width)
        date_width = pymupdf.get_text_length(issued_text, fontname=body_font, fontsize=date_fontsize)
    page.insert_text(
        pymupdf.Point(content_center_x - date_width / 2, 633.0),
        issued_text,
        fontname=body_font,
        fontsize=date_fontsize,
        color=(0.0196, 0.0392, 0.1882),
    )

    # Replace the sample QR only; preserve the adjacent seal, signature and other artwork.
    cert_url = get_verification_url(cert_id, base_url=resolved_base_url)
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=1,
    )
    qr.add_data(cert_url)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white")
    qr_buffer = io.BytesIO()
    qr_img.save(qr_buffer, format="PNG")
    qr_bytes = qr_buffer.getvalue()

    qr_rect = pymupdf.Rect(60.0, 613.5, 176.25, 729.75)
    # Remove the embedded sample QR image resource before inserting the new unique QR.
    for image_info in page.get_images(full=True):
        image_xref = image_info[0]
        if any(
            abs(image_rect.x0 - qr_rect.x0) < 1
            and abs(image_rect.y0 - qr_rect.y0) < 1
            and abs(image_rect.x1 - qr_rect.x1) < 1
            and abs(image_rect.y1 - qr_rect.y1) < 1
            for image_rect in page.get_image_rects(image_xref)
        ):
            page.delete_image(image_xref)
            break
    page.insert_image(qr_rect, stream=qr_bytes, overlay=True)

    pdf_bytes = doc.tobytes(garbage=4, deflate=True)
    doc.close()

    filename = f"{safe_filename(student_name)}_Course_Certificate.pdf"
    return pdf_bytes, filename


def generate_certificate_pdf(data, output_dir, base_url=None):
    """Generates PDF and writes to output_dir, returning the filename."""
    pdf_bytes, filename = generate_certificate_pdf_bytes(data, base_url=base_url)
    out_path = Path(output_dir) / filename
    out_path.write_bytes(pdf_bytes)
    return filename


def generate_certificate_image_bytes(data, base_url=None, dpi=180):
    """Renders page 0 of the generated certificate as PNG image bytes."""
    pdf_bytes, _ = generate_certificate_pdf_bytes(data=data, base_url=base_url)
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    page = doc[0]
    pix = page.get_pixmap(dpi=dpi)
    img_bytes = pix.tobytes("png")
    doc.close()
    return img_bytes


def send_certificate_email(data: dict, pdf_bytes: bytes = None, filename: str = None, base_url: str = None, pdf_path: str = None):
    """
    Sends Course Completion Certificate email to the student with PDF attached.
    Syncs with SMTP and IMAP Sent folder.
    """
    student_name = str(data.get("student_name", "")).strip()
    recipient = str(data.get("student_email", "")).strip()
    course_name = str(data.get("course_name") or data.get("domain") or "Course").strip()
    cert_id = str(data.get("certificate_id", "")).strip()

    if not recipient:
        raise ValueError("Recipient student email address is required.")

    if not pdf_bytes and pdf_path and Path(pdf_path).exists():
        pdf_bytes = Path(pdf_path).read_bytes()

    if not pdf_bytes:
        pdf_bytes, auto_filename = generate_certificate_pdf_bytes(data, base_url=base_url)
        if not filename:
            filename = auto_filename

    if not filename:
        filename = f"{safe_filename(student_name)}_Course_Certificate.pdf"

    if not SENDER_EMAIL or not SENDER_PASSWORD:
        raise ValueError("Sender email credentials are not configured in environment variables.")

    verify_url = get_verification_url(cert_id, base_url=base_url)

    message = EmailMessage()
    message["From"] = f"Persevex LLP <{SENDER_EMAIL}>"
    message["To"] = format_to_header(student_name, recipient)
    message["Subject"] = "Congratulations on Successfully Completing Your Course with Persevex!"

    message.set_content(
        f"Dear {student_name},\n\n"
        f"We are delighted to congratulate you on the successful completion of your {course_name} course with Persevex!\n\n"
        "Your dedication, commitment, and efforts throughout the course are truly appreciated. We hope this experience has helped you strengthen your skills, gain valuable knowledge, and take another step toward achieving your professional goals.\n\n"
        "Please find your official Course Completion Certificate attached to this email as recognition of your successful completion of the program.\n\n"
        f"You can verify your certificate online anytime using the unique QR code on the document or by visiting:\n"
        f"{verify_url}\n\n"
        "We wish you continued success in your academic and professional journey and look forward to seeing you achieve great things in the future.\n\n"
        "Warm regards,\n"
        "Team Persevex\n"
        "Persevex LLP\n"
    )

    # Attach Certificate PDF
    message.add_attachment(
        pdf_bytes,
        maintype="application",
        subtype="pdf",
        filename=filename,
    )

    # Send via SMTP
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
        smtp.login(SENDER_EMAIL, SENDER_PASSWORD)
        smtp.send_message(message)

    # Save to IMAP Sent folder
    try:
        with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT) as imap:
            imap.login(SENDER_EMAIL, SENDER_PASSWORD)
            imap.append(IMAP_SENT_FOLDER, "\\Seen", None, message.as_bytes())
    except Exception as imap_err:
        print("IMAP save error (non-fatal):", repr(imap_err))

    return True
