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


def generate_certificate_pdf_bytes(data, base_url=None):
    """
    Generates a Course Completion Certificate PDF from the uploaded PDF template.
    Preserves all original artwork, watermark, logo, ISO 9001 logo, and administrator signature.
    Dynamically replaces only:
      1. Student Name
      2. Course Name in the paragraph
      3. Issue Date
      4. Unique QR Code placed immediately to the left of the ISO 9001 logo.
    Returns: tuple (pdf_bytes, filename)
    """
    if not TEMPLATE_PATH.exists():
        raise FileNotFoundError(f"Course Completion Certificate template not found at {TEMPLATE_PATH}")

    student_name = str(data.get("student_name", "")).strip() or "Student Name"
    course_name = str(data.get("course_name") or data.get("domain") or "Course").strip()
    issued_date = format_display_date(data.get("issued_date") or data.get("issue_date") or "")
    if not issued_date:
        issued_date = datetime.now().strftime("%d-%m-%Y")

    cert_id = str(data.get("certificate_id", "")).strip()
    if not cert_id:
        cert_id = generate_certificate_id()

    resolved_base_url = (base_url or get_public_base_url()).rstrip("/")

    doc = pymupdf.open(str(TEMPLATE_PATH))
    page = doc[0]
    # Geometric center of the white content area (aligns with 'CERTIFICATE' and 'This certificate is proudly presented to')
    content_center_x = 461.54

    # 1. Redact static placeholders with transparent redactions (preserves vector drawings & watermark)
    # Student name area
    page.add_redact_annot(pymupdf.Rect(200, 298, 750, 352))
    # Paragraph area
    page.add_redact_annot(pymupdf.Rect(200, 355, 750, 424))
    # Issued on area
    page.add_redact_annot(pymupdf.Rect(545, 444, 700, 465))

    page.apply_redactions(
        images=pymupdf.PDF_REDACT_IMAGE_NONE,
        graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
        text=pymupdf.PDF_REDACT_TEXT_REMOVE,
    )

    # 2. Insert Student Name (Centered within main white content area at baseline y=336.0)
    # Primary font: Times-Bold (built-in 'tibo', matching TimesNRMTPro-Bold)
    name_fontsize = 42.0
    name_len = len(student_name)
    if name_len > 32:
        name_fontsize = 28.0
    elif name_len > 24:
        name_fontsize = 32.0
    elif name_len > 18:
        name_fontsize = 36.0

    name_width = pymupdf.get_text_length(student_name, fontname="tibo", fontsize=name_fontsize)
    if name_width > 540.0:
        name_fontsize = round(name_fontsize * (540.0 / name_width), 1)
        name_width = pymupdf.get_text_length(student_name, fontname="tibo", fontsize=name_fontsize)

    name_x = content_center_x - (name_width / 2)
    page.insert_text(
        pymupdf.Point(name_x, 336.0),
        student_name,
        fontname="tibo",
        fontsize=name_fontsize,
        color=(0.0196, 0.0392, 0.1882),  # Deep navy #050a30 matching template
    )

    # 3. Insert Centered & Enlarged Completion Description
    # Formats the sentence into centered lines with balanced line spacing
    max_desc_width = 530.0
    body_fontsize = 15.0

    line1 = "This is to certify that the candidate has successfully completed the"
    line2 = f"{course_name} course at Persevex, demonstrating strong"
    line3 = "commitment and competence throughout the program."

    w2 = pymupdf.get_text_length(line2, fontname="tibo", fontsize=body_fontsize)
    if w2 > max_desc_width:
        # Modestly adjust font size to keep the clean 3-line shape
        body_fontsize = max(12.5, round(body_fontsize * (max_desc_width / w2), 1))
        w2 = pymupdf.get_text_length(line2, fontname="tibo", fontsize=body_fontsize)

    # If course name is exceptionally long, dynamically wrap full paragraph
    if w2 > max_desc_width:
        full_para = f"This is to certify that the candidate has successfully completed the {course_name} course at Persevex, demonstrating strong commitment and competence throughout the program."
        words = full_para.split()
        desc_lines = []
        cur_line = []
        body_fontsize = 13.0
        for w in words:
            candidate = " ".join(cur_line + [w])
            if pymupdf.get_text_length(candidate, fontname="tibo", fontsize=body_fontsize) <= max_desc_width:
                cur_line.append(w)
            else:
                desc_lines.append(" ".join(cur_line))
                cur_line = [w]
        if cur_line:
            desc_lines.append(" ".join(cur_line))
    else:
        desc_lines = [line1, line2, line3]

    if len(desc_lines) <= 3:
        baselines = [371.0, 393.0, 415.0][:len(desc_lines)]
    else:
        baselines = [365.0, 383.0, 401.0, 419.0][:len(desc_lines)]

    for line, baseline_y in zip(desc_lines, baselines):
        line_w = pymupdf.get_text_length(line, fontname="tibo", fontsize=body_fontsize)
        line_x = content_center_x - (line_w / 2)
        page.insert_text(
            pymupdf.Point(line_x, baseline_y),
            line,
            fontname="tibo",
            fontsize=body_fontsize,
            color=(0.0196, 0.0392, 0.1882),
        )

    # 4. Insert Issued on Date at Point(549.58, 457.68)
    issued_text = f"Issued on: {issued_date}"
    page.insert_text(
        pymupdf.Point(549.58, 457.68),
        issued_text,
        fontname="tibo",
        fontsize=12.08,
        color=(0.0196, 0.0392, 0.1882),
    )

    # 5. Insert Unique QR Code to the left of the ISO 9001 logo
    # ISO 9001 logo bounding box is Rect(417.02, 499.18, 505.71, 575.12)
    # We place the QR code at Rect(275, 497, 355, 577) - size 80x80 pt, 62 pt spacing to ISO logo
    verify_url = get_verification_url(cert_id, base_url=resolved_base_url)
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=1,
    )
    qr.add_data(verify_url)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white")

    qr_buffer = io.BytesIO()
    qr_img.save(qr_buffer, format="PNG")
    qr_bytes = qr_buffer.getvalue()

    qr_rect = pymupdf.Rect(225, 497, 305, 577)
    page.insert_image(qr_rect, stream=qr_bytes)

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
    message["To"] = recipient
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
        "Persevex Education Consultancy LLP\n"
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
