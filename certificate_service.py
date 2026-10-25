import os
import io
import re
import html
import base64
import secrets
import string
import sqlite3
import smtplib
import imaplib
import tempfile
from pathlib import Path
from datetime import datetime
from email.message import EmailMessage

import pymupdf
import qrcode
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATE_DIR = BASE_DIR / "pdf_templates"

# Email Configuration from environment
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "").strip()
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD", "").strip()
SMTP_HOST = "smtpout.secureserver.net"
SMTP_PORT = 465
IMAP_HOST = "imap.secureserver.net"
IMAP_PORT = 993
IMAP_SENT_FOLDER = "Sent"

# Local SQLite fallback database path (used if Supabase table is not yet created)
DB_FALLBACK_FILE = BASE_DIR / "certificates_local.db"


# ============================================================
# INITIALIZE LOCAL SQLITE FALLBACK TABLE
# ============================================================
def init_local_db():
    try:
        from database.repository import init_database_tables
        init_database_tables()
    except Exception as exc:
        print("LOCAL DB INIT ERROR:", repr(exc))


import os as _os
# Skip local SQLite initialisation on Vercel: the project-root path is
# read-only there and the call would only produce a caught OperationalError
# log on every cold start.  Supabase is the authoritative persistence layer
# on Vercel, so skipping the local init has no functional impact.
if not _os.environ.get("VERCEL"):
    init_local_db()



# ============================================================
# HELPER FUNCTIONS
# ============================================================
def safe_filename(name):
    name = re.sub(r"[^\w\s-]", "", str(name), flags=re.UNICODE)
    name = name.strip()
    name = re.sub(r"\s+", "_", name)
    return name or "Student"


def generate_bulk_certificate_ids(count, supabase_client=None, existing_ids=None):
    """
    Generate `count` unique Certificate IDs in format: PXL-CERT-YYYY-XXXXXX
    Ensures internal uniqueness and verifies against database in a single batch query
    instead of per-certificate round trips.
    """
    if count <= 0:
        return []

    current_year = datetime.now().year
    alphabet = string.ascii_uppercase + string.digits
    known_used = set(str(cid).strip() for cid in (existing_ids or []) if cid)
    generated_ids = []

    # Generate count unique candidate IDs
    candidates = []
    while len(candidates) < count:
        random_suffix = "".join(secrets.choice(alphabet) for _ in range(6))
        candidate = f"PXL-CERT-{current_year}-{random_suffix}"
        if candidate not in known_used and candidate not in candidates:
            candidates.append(candidate)

    # Check collisions in database via single batch query
    existing_in_db = repository.check_existing_certificate_ids_batch(candidates, supabase_client=supabase_client)

    for candidate in candidates:
        if candidate not in existing_in_db:
            generated_ids.append(candidate)
            known_used.add(candidate)
        else:
            # Collision handling (extremely rare): generate until non-colliding
            resolved = False
            for _ in range(20):
                random_suffix = "".join(secrets.choice(alphabet) for _ in range(6))
                new_candidate = f"PXL-CERT-{current_year}-{random_suffix}"
                if new_candidate not in known_used:
                    single_rec, db_error = db_get_certificate_by_id(new_candidate, supabase_client, return_error_detail=True)
                    if db_error:
                        raise RuntimeError(f"Database error while verifying candidate certificate ID collision: {db_error}")
                    if not single_rec:
                        generated_ids.append(new_candidate)
                        known_used.add(new_candidate)
                        resolved = True
                        break
            if not resolved:
                raise RuntimeError(f"Failed to generate non-colliding certificate ID after 20 attempts for year {current_year}")

    return generated_ids


def generate_certificate_id(supabase_client=None):
    """
    Generate unique Certificate ID in format: PXL-CERT-YYYY-XXXXXX
    Ensures no collision with existing records.
    Raises RuntimeError if database error prevents verifying uniqueness.
    """
    ids = generate_bulk_certificate_ids(1, supabase_client=supabase_client)
    if ids:
        return ids[0]
    raise RuntimeError("Failed to generate a verified unique certificate ID.")


import socket
import urllib.parse


def get_lan_ip():
    """Get the local network IP for mobile device QR testing on LAN."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def get_public_base_url(request=None):
    """
    ONE centralized resolver for the public base URL of the certificate verification portal.
    Priority:
    1. PUBLIC_BASE_URL (or BASE_URL) environment variable:
       Explicit canonical public URL (e.g. https://your-canonical-domain.com or production Vercel domain).
    2. Incoming public cloud request (if host is not localhost/private IP):
       Uses request.host_url (e.g. https://your-deployment.vercel.app).
    3. Production cloud environment detection (e.g. VERCEL_URL):
       If running on Vercel with VERCEL_URL, uses https://{VERCEL_URL}.
    4. Local development host from request:
       Uses request.host_url (e.g. http://localhost:5000 or http://127.0.0.1:5000).
    5. Local testing fallback:
       http://127.0.0.1:5000
    """
    env_base = (
        os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
        or os.getenv("BASE_URL", "").strip().rstrip("/")
    )
    if env_base:
        return env_base

    if request:
        try:
            host = request.host.lower()
            # If deployed on cloud domain (not localhost/127.0.0.1/private IP range)
            if (
                "localhost" not in host
                and "127.0.0.1" not in host
                and not host.startswith("192.168.")
                and not host.startswith("10.")
                and not host.startswith("172.")
            ):
                return request.host_url.rstrip("/")

            # Local development host: preserve local request origin (e.g. http://localhost:5000)
            return request.host_url.rstrip("/")
        except Exception:
            pass

    # Check VERCEL_URL environment variable if deployed on Vercel
    vercel_url = os.getenv("VERCEL_URL", "").strip().rstrip("/")
    if vercel_url:
        return f"https://{vercel_url}"

    # Default local testing fallback (never encode external third-party domain)
    return "http://127.0.0.1:5000"


# Alias for backward compatibility if called as get_base_url
get_base_url = get_public_base_url


def get_verification_url(certificate_id, request=None, base_url=None):
    """
    ONE centralized function for generating the public verification URL.
    Returns: {PUBLIC_BASE_URL}/verify/{certificate_id}
    """
    resolved_base = (base_url or get_public_base_url(request)).rstrip("/")
    cert_id = str(certificate_id).strip()
    return f"{resolved_base}/verify/{cert_id}"


def get_certificate_thumbnail_url(certificate_id, request=None):
    """
    ONE centralized function for generating the public certificate thumbnail URL
    specifically used for Open Graph, Twitter Cards, and social sharing image previews.
    Returns: {PUBLIC_BASE_URL}/verify/{certificate_id}/image
    """
    base_url = get_public_base_url(request)
    cert_id = str(certificate_id).strip()
    return f"{base_url}/verify/{cert_id}/image"


def get_certificate_download_url(certificate_id, request=None):
    """
    ONE centralized function for generating the public certificate PDF download URL.
    Returns: {PUBLIC_BASE_URL}/verify/{certificate_id}/download
    """
    base_url = get_public_base_url(request)
    cert_id = str(certificate_id).strip()
    return f"{base_url}/verify/{cert_id}/download"


def get_certificate_preview_url(certificate_id, request=None):
    """
    ONE centralized function for generating the public certificate PDF preview/stream URL.
    Returns: {PUBLIC_BASE_URL}/verify/{certificate_id}/preview
    """
    base_url = get_public_base_url(request)
    cert_id = str(certificate_id).strip()
    return f"{base_url}/verify/{cert_id}/preview"


def get_linkedin_share_data(record, request=None):
    """
    Generates professional LinkedIn post content and share URL with production verification URL.
    """
    cert_id = str(record.get("certificate_id", "")).strip()
    student_name = str(record.get("student_name", "Student")).strip()
    domain = str(record.get("internship_domain") or record.get("domain") or "Internship").strip()
    verification_url = get_verification_url(cert_id, request)
    clean_domain_tag = re.sub(r"[^\w]", "", domain) or "Internship"

    post_text = (
        f"🎓 Excited to share that I have successfully completed my internship in {domain} "
        f"at Persevex Education Consultancy LLP.\n\n"
        f"I am grateful for the opportunity to gain practical experience, "
        f"strengthen my technical skills, and learn throughout this internship.\n\n"
        f"📜 Official Certificate:\n{verification_url}\n\n"
        f"Thank you to Persevex Education Consultancy LLP for this valuable learning experience.\n\n"
        f"#Internship\n#{clean_domain_tag}\n#Persevex\n#InternshipCertificate\n#CareerGrowth\n#Learning\n#ProfessionalDevelopment"
    )

    share_url = f"https://www.linkedin.com/sharing/share-offsite/?url={urllib.parse.quote(verification_url, safe='')}"
    return post_text, share_url


def get_whatsapp_share_data(record, request=None):
    """
    Generates professional WhatsApp sharing text and URL with production verification URL.
    """
    cert_id = str(record.get("certificate_id", "")).strip()
    student_name = str(record.get("student_name", "Student")).strip()
    domain = str(record.get("internship_domain") or record.get("domain") or "Internship").strip()
    verification_url = get_verification_url(cert_id, request)

    message_text = (
        f"🎓 I am proud to share my officially verified Persevex Internship Certificate.\n\n"
        f"Certificate Holder: {student_name}\n"
        f"Internship Domain: {domain}\n"
        f"Certificate ID: {cert_id}\n\n"
        f"Verify the certificate:\n{verification_url}"
    )

    share_url = f"https://wa.me/?text={urllib.parse.quote(message_text, safe='')}"
    return message_text, share_url


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


PERSISTENT_CERT_DIR = Path(tempfile.gettempdir()) / "persevex_certificates"
try:
    PERSISTENT_CERT_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass


# ============================================================
# DATABASE OPERATIONS (DELEGATED TO CENTRALIZED REPOSITORY)
# ============================================================
from database import repository


def db_get_certificate_by_id(cert_id, supabase_client=None, return_error_detail=False):
    """Fetch certificate by ID with automatic Supabase + SQLite fallback."""
    return repository.get_certificate_by_id(cert_id, supabase_client=supabase_client, return_error_detail=return_error_detail)


def db_get_certificate_by_email(email, supabase_client=None):
    """Fetch most recent certificate by student email."""
    if not email:
        return None
    email_clean = email.strip().lower()
    sb = supabase_client or repository.get_supabase_client()
    if sb:
        try:
            res = (
                sb.table("certificate_history")
                .select("*")
                .eq("student_email", email_clean)
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            if res.data and len(res.data) > 0:
                rec = dict(res.data[0])
                if not rec.get("certificate_status"):
                    rec["certificate_status"] = "active"
                return rec
        except Exception:
            pass

    try:
        conn = sqlite3.connect(DB_FALLBACK_FILE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM certificate_history WHERE LOWER(student_email) = ? ORDER BY id DESC LIMIT 1",
            (email_clean,)
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            rec = dict(row)
            if not rec.get("certificate_status"):
                rec["certificate_status"] = "active"
            return rec
    except Exception as exc:
        print("LOCAL DB GET EMAIL ERROR:", repr(exc))

    return None


def db_get_certificate_by_pk_id(record_id, supabase_client=None):
    """Retrieve certificate record by primary key ID (integer)."""
    return repository.get_certificate_by_pk_id(record_id)


def db_save_certificate_record(record_data, supabase_client=None, existing_id=None):
    """Field-preserving merged save to Supabase and local SQLite."""
    return repository.save_certificate_record(record_data, existing_id=existing_id)


def db_get_all_certificates(supabase_client=None):
    """Fetch all certificate records from Supabase / SQLite."""
    return repository._fetch_all_raw_certificates()


def db_delete_certificate(record_id, supabase_client=None):
    """Safely delete certificate record from history."""
    return repository.delete_certificate_record(record_id)


def db_revoke_certificate(cert_id, reason="Revoked by administrator"):
    """Revoke a certificate."""
    return repository.revoke_certificate_record(cert_id, reason)


# ============================================================
# CERTIFICATE PDF GENERATION ENGINE (IN-MEMORY & STATELESS)
# ============================================================
def generate_certificate_pdf_bytes(data, base_url=None, template_version="v1"):
    """
    Generates a high-quality Certificate PDF in memory without requiring disk storage.
    Supports template versioning (default 'v1').
    Returns: tuple (pdf_bytes, filename)
    """
    clean_bg_path = STATIC_DIR / "certificate_clean_bg.png"
    if not clean_bg_path.exists():
        clean_bg_path = TEMPLATE_DIR / "certificate_clean_bg.png"

    if not clean_bg_path.exists():
        raise FileNotFoundError(f"Clean certificate background image not found at {clean_bg_path}")

    student_name = str(data.get("student_name", "")).strip()
    domain = str(data.get("domain") or data.get("internship_domain") or "").strip()
    start_date = format_display_date(data.get("start_date", ""))
    end_date = format_display_date(data.get("end_date", ""))
    issued_date = format_display_date(data.get("issued_date", ""))
    cert_id = str(data.get("certificate_id", "")).strip()

    resolved_base_url = (base_url or get_public_base_url()).rstrip("/")

    # Standard A4 Landscape: 842.0 x 595.0 pt
    doc = pymupdf.open()
    page = doc.new_page(width=842.0, height=595.0)

    # 1. Insert high-resolution clean background template
    page.insert_image(page.rect, filename=str(clean_bg_path))

    # 2. Student Name - Centered bold serif (Times-Bold)
    # Dynamic font size adjustment for long student names to keep naturally on 1 line
    name_len = len(student_name)
    if name_len > 34:
        name_fontsize = 24.0
    elif name_len > 25:
        name_fontsize = 28.5
    elif name_len > 18:
        name_fontsize = 33.0
    else:
        name_fontsize = 36.0

    name_rect = pymupdf.Rect(40, 275, 802, 342)
    page.insert_textbox(
        name_rect,
        student_name,
        fontname="tibo",  # Times-Bold serif matching original template
        fontsize=name_fontsize,
        color=(0.03, 0.03, 0.06),
        align=pymupdf.TEXT_ALIGN_CENTER,
    )

    # 3. Dynamic Body Text Paragraph - Times-Bold serif matching original template
    line1 = f"This is to certify that the candidate has successfully completed the Internship in {domain}"
    line2 = f"at Persevex from {start_date} to {end_date}, demonstrating strong commitment"
    line3 = "and competence throughout the program."
    body_text = f"{line1}\n{line2}\n{line3}"

    dom_len = len(domain)
    if dom_len > 36:
        body_fontsize = 12.2
    elif dom_len > 26:
        body_fontsize = 13.6
    else:
        body_fontsize = 15.0

    body_rect = pymupdf.Rect(50, 344, 792, 428)
    page.insert_textbox(
        body_rect,
        body_text,
        fontname="tibo",  # Times-Bold serif
        fontsize=body_fontsize,
        color=(0.03, 0.03, 0.06),
        align=pymupdf.TEXT_ALIGN_CENTER,
    )

    # 4. Issued on Date - Times-Bold serif matching original template
    issued_text = f"Issued on: {issued_date}"
    issued_rect = pymupdf.Rect(440, 422, 652, 456)
    page.insert_textbox(
        issued_rect,
        issued_text,
        fontname="tibo",
        fontsize=12.5,
        color=(0.03, 0.03, 0.06),
        align=pymupdf.TEXT_ALIGN_RIGHT,
    )

    # 5. Dynamic Scannable QR Code linking to verification page
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

    # QR Code bounding box at bottom-left
    qr_rect = pymupdf.Rect(92, 480, 167, 555)
    page.insert_image(qr_rect, stream=qr_bytes)

    pdf_bytes = doc.tobytes(garbage=4, deflate=True)
    doc.close()

    filename = f"{safe_filename(student_name)}_Certificate.pdf"
    return pdf_bytes, filename


def generate_certificate_image_bytes(data, base_url=None, dpi=180, template_version="v1"):
    """
    Renders and returns high-resolution PNG image bytes directly from memory.
    """
    pdf_bytes, _ = generate_certificate_pdf_bytes(
        data=data,
        base_url=base_url,
        template_version=template_version,
    )
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    page = doc[0]
    pix = page.get_pixmap(dpi=dpi)
    img_bytes = pix.tobytes("png")
    doc.close()
    return img_bytes


def render_certificate_preview_image(data, base_url=None, dpi=150):
    """
    Renders an in-memory PNG preview data URI for a candidate record before generation.
    Does NOT write to database, does NOT create permanent files, does NOT send emails.
    """
    preview_data = dict(data)
    if not preview_data.get("certificate_id"):
        preview_data["certificate_id"] = "PXL-CERT-PREVIEW"

    img_bytes = generate_certificate_image_bytes(
        data=preview_data,
        base_url=base_url,
        dpi=dpi,
        template_version="v1"
    )
    b64_str = base64.b64encode(img_bytes).decode("ascii")
    return f"data:image/png;base64,{b64_str}"



def generate_certificate_pdf(data, output_dir=None, base_url=None, template_version="v1", custom_filename=None):
    """
    Generates certificate PDF in memory and optionally writes to output_dir as local cache.
    Returns: filename string
    """
    pdf_bytes, default_filename = generate_certificate_pdf_bytes(
        data=data,
        base_url=base_url,
        template_version=template_version,
    )
    filename = custom_filename or default_filename

    if output_dir is not None:
        try:
            output_path = Path(output_dir) / filename
            output_path.parent.mkdir(parents=True, exist_ok=True)
            temp_output = output_path.with_suffix(f".tmp.{os.getpid()}.{secrets.token_hex(4)}")
            temp_output.write_bytes(pdf_bytes)
            os.replace(temp_output, output_path)
        except Exception as save_err:
            pass

    # Opportunistically write to persistent cert dir if writable
    try:
        persistent_path = PERSISTENT_CERT_DIR / filename
        persistent_path.parent.mkdir(parents=True, exist_ok=True)
        temp_persistent = persistent_path.with_suffix(f".tmp.{os.getpid()}.{secrets.token_hex(4)}")
        temp_persistent.write_bytes(pdf_bytes)
        os.replace(temp_persistent, persistent_path)
    except Exception:
        pass

    return filename


# ============================================================
# SEND CERTIFICATE EMAIL VIA SMTP
# ============================================================
def send_certificate_email(data, pdf_path=None, filename=None, pdf_bytes=None, base_url=None):
    """
    Sends the generated Certificate PDF to the student via SMTP,
    saves the email to the IMAP Sent folder, and returns status.
    Accepts pdf_path, in-memory pdf_bytes, or regenerates dynamically on demand.
    """
    recipient = str(data.get("student_email", "")).strip().lower()
    student_name = str(data.get("student_name", "Student")).strip()
    domain = str(data.get("domain") or data.get("internship_domain") or "Internship").strip()

    if not recipient:
        raise ValueError("Student email address is required.")

    # Retrieve or generate PDF bytes
    if pdf_bytes is None:
        if pdf_path is not None and hasattr(pdf_path, "exists") and pdf_path.exists():
            pdf_bytes = pdf_path.read_bytes()
        else:
            # Self-heal / generate dynamically in memory
            pdf_bytes, auto_filename = generate_certificate_pdf_bytes(data, base_url=base_url)
            if not filename:
                filename = auto_filename

    if not filename:
        filename = f"{safe_filename(student_name)}_Certificate.pdf"

    if not SENDER_EMAIL or not SENDER_PASSWORD:
        raise ValueError("Sender email credentials are not configured in environment variables.")

    # Build Email Message
    message = EmailMessage()
    message["From"] = f"Persevex LLP <{SENDER_EMAIL}>"
    message["To"] = recipient
    message["Subject"] = f"Internship Completion Certificate - {student_name} | Persevex LLP"

    escaped_student_name = html.escape(student_name)
    escaped_domain = html.escape(domain)
    current_year = datetime.now().year

    # Plain text version
    message.set_content(f"""Dear {student_name},

Greetings from Persevex!

🎉 Congratulations on successfully completing your Internship program in {domain} with Persevex!

We truly appreciate your dedication, enthusiasm, and contributions throughout your internship with us.

📜 Attached to this email is your official Certificate of Internship Completion in recognition of your internship experience, commitment, and the skills you have developed.

💼 Share Your Achievement!
Celebrate this milestone by sharing your internship experience and certificate on LinkedIn and other social media platforms. Let your network know about the skills you have developed and the experience you have gained during your internship with Persevex.

Tag @Persevex and use the hashtags #Persevex and #InternshipCompletion to share your achievement with your professional network and inspire other aspiring professionals.

🔎 Digital Certificate Verification:
Your certificate includes a unique, secure QR code. Anyone can scan this QR code to verify the certificate's authenticity online through our official verification portal.

We wish you continued success in your career and future learning endeavors!

Best regards,
Team Persevex
Persevex Education Consultancy LLP
📧 support@persevex.com
🌐 www.persevex.com
""")

    # HTML formatted version
    message.add_alternative(f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="X-UA-Compatible" content="IE=edge">
    <title>Internship Completion Certificate</title>
    <style type="text/css">
        body, table, td, p, a, li, blockquote {{
            -webkit-text-size-adjust: 100%;
            -ms-text-size-adjust: 100%;
        }}
        table, td {{
            mso-table-lspace: 0pt;
            mso-table-rspace: 0pt;
        }}
        img {{
            -ms-interpolation-mode: bicubic;
            border: 0;
            height: auto;
            line-height: 100%;
            outline: none;
            text-decoration: none;
            max-width: 100%;
        }}
        @media only screen and (max-width: 600px) {{
            .email-outer-table {{
                padding: 12px 6px !important;
            }}
            .email-container {{
                width: 100% !important;
                max-width: 100% !important;
                border-radius: 8px !important;
            }}
            .header-cell {{
                padding: 20px 18px !important;
            }}
            .content-cell {{
                padding: 20px 18px !important;
            }}
            .footer-cell {{
                padding: 14px 18px !important;
            }}
            .card-padding {{
                padding: 12px 14px !important;
            }}
            .header-title {{
                font-size: 20px !important;
            }}
            .body-text {{
                font-size: 14px !important;
                line-height: 1.55 !important;
            }}
        }}
    </style>
</head>
<body style="margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f4f6f9; color: #334155; line-height: 1.6;">
    <!-- Outer Background Wrapper Table -->
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" class="email-outer-table" style="background-color: #f4f6f9; padding: 24px 12px; margin: 0;">
        <tr>
            <td align="center">
                <!--[if (gte mso 9)|(IE)]>
                <table role="presentation" align="center" border="0" cellspacing="0" cellpadding="0" width="600" style="width: 600px;">
                <tr>
                <td align="center" valign="top" width="600" style="width: 600px;">
                <![endif]-->
                <!-- Centered Responsive Email Card Container -->
                <table role="presentation" align="center" width="600" cellpadding="0" cellspacing="0" border="0" class="email-container" style="max-width: 600px; width: 100%; margin: 0 auto; background-color: #ffffff; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.06); overflow: hidden; border: 1px solid #e2e8f0;">
                    <!-- Brand Header -->
                    <tr>
                        <td class="header-cell" style="padding: 24px 30px; background-color: #0f172a; background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%); text-align: left;">
                            <h2 class="header-title" style="margin: 0; color: #ffffff; font-size: 22px; font-weight: 700; letter-spacing: 0.5px; line-height: 1.2;">
                                persevex <span style="color: #60a5fa; font-size: 14px; font-weight: 500;">LLP</span>
                            </h2>
                            <p style="margin: 4px 0 0 0; color: #94a3b8; font-size: 11px; letter-spacing: 1px; text-transform: uppercase;">
                                LEARN, GROW, SUCCEED
                            </p>
                        </td>
                    </tr>
                    <!-- Main Body Content -->
                    <tr>
                        <td class="content-cell" style="padding: 28px 30px; background-color: #ffffff;">
                            <p class="body-text" style="font-size: 15px; margin: 0 0 12px 0; color: #1e293b;">
                                Dear <strong>{escaped_student_name}</strong>,
                            </p>

                            <p class="body-text" style="font-size: 15px; margin: 0 0 14px 0; color: #334155;">
                                Greetings from <strong>Persevex</strong>!
                            </p>

                            <!-- Congratulations Callout Box -->
                            <div class="card-padding" style="background-color: #f0fdf4; border-left: 4px solid #22c55e; border-radius: 6px; padding: 14px 16px; margin: 16px 0;">
                                <p style="margin: 0; font-size: 15px; line-height: 1.5; color: #15803d; font-weight: 600; word-break: break-word;">
                                    🎉 Congratulations on successfully completing your Internship program in <strong>{escaped_domain}</strong>!
                                </p>
                            </div>

                            <p class="body-text" style="font-size: 15px; margin: 0 0 14px 0; color: #334155;">
                                We truly appreciate your dedication, enthusiasm, and contributions throughout your internship with Persevex.
                            </p>

                            <p class="body-text" style="font-size: 15px; margin: 0 0 16px 0; color: #334155;">
                                📜 Please find attached your official <strong>Certificate of Internship Completion</strong> in recognition of your internship experience, commitment, and the skills you have developed.
                            </p>

                            <!-- Social Media & LinkedIn Sharing Card -->
                            <div class="card-padding" style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-left: 4px solid #0a66c2; border-radius: 6px; padding: 14px 16px; margin: 18px 0;">
                                <p style="margin: 0 0 8px 0; font-size: 15px; font-weight: 700; color: #0f172a;">
                                    💼 Share Your Achievement!
                                </p>
                                <p style="margin: 0 0 10px 0; font-size: 14px; line-height: 1.55; color: #334155;">
                                    Celebrate this milestone by sharing your internship experience and certificate on LinkedIn and other social media platforms. Let your network know about the skills you have developed and the experience you have gained during your internship with Persevex.
                                </p>
                                <p style="margin: 0; font-size: 13px; line-height: 1.55; color: #475569; word-break: break-word;">
                                    Tag <strong>@Persevex</strong> and use the hashtags <strong style="color: #0a66c2;">#Persevex</strong> and <strong style="color: #0a66c2;">#InternshipCompletion</strong> to share your achievement with your professional network and inspire other aspiring professionals.
                                </p>
                            </div>

                            <!-- Digital QR Code Verification Card -->
                            <div class="card-padding" style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-left: 4px solid #6366f1; border-radius: 6px; padding: 12px 16px; margin: 16px 0;">
                                <p style="margin: 0; font-size: 13px; line-height: 1.55; color: #475569; word-break: break-word;">
                                    <strong>🔎 Digital Verification:</strong> Your certificate includes a unique, secure QR code. Scanning the QR code allows anyone to verify the certificate's authenticity online on our official verification portal.
                                </p>
                            </div>

                            <p class="body-text" style="font-size: 15px; margin: 16px 0 0 0; color: #334155;">
                                We wish you continued success in your career and future learning endeavors!
                            </p>

                            <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 20px 0 16px 0;">

                            <p style="margin: 0; font-size: 14px; line-height: 1.6; color: #64748b;">
                                Best regards,<br>
                                <strong style="color: #0f172a; font-size: 15px;">Team Persevex</strong><br>
                                <span style="font-size: 13px; color: #64748b;">Persevex Education Consultancy LLP</span><br>
                                <span style="font-size: 13px;">📧 <a href="mailto:support@persevex.com" style="color: #2563eb; text-decoration: none;">support@persevex.com</a></span><br>
                                <span style="font-size: 13px;">🌐 <a href="https://www.persevex.com" style="color: #2563eb; text-decoration: none;">www.persevex.com</a></span>
                            </p>
                        </td>
                    </tr>
                    <!-- Footer -->
                    <tr style="background-color: #f8fafc; border-top: 1px solid #e2e8f0;">
                        <td class="footer-cell" style="padding: 14px 28px; text-align: center; color: #94a3b8; font-size: 12px; line-height: 1.4;">
                            © {current_year} Persevex Education Consultancy LLP. All rights reserved.
                        </td>
                    </tr>
                </table>
                <!--[if (gte mso 9)|(IE)]>
                </td>
                </tr>
                </table>
                <![endif]-->
            </td>
        </tr>
    </table>
</body>
</html>""", subtype="html")

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
        print("WARNING: Could not save certificate email to IMAP Sent folder:", repr(imap_err))

    return True
