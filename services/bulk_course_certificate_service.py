"""
Bulk Course Completion Certificate (CCC) Service
=================================================
Parallel but independent implementation of bulk_certificate_service.py,
adapted for Course Completion Certificates.

Key differences from bulk_certificate_service.py:
- Table: course_certificate_history (not certificate_history)
- Certificate ID format: PXL-CCC-YYYY-XXXXXX
- Fields: student_name, student_email, course_name, issued_date (REQUIRED)
- No start_date / end_date fields
- No bulk_save_certificate_records — use save_course_certificate_record per row
- Duplicate identity: 4-tuple (name, email, course_name, issued_date_normalized)
"""

import io
import re
import csv
import time
from datetime import datetime
from pathlib import Path

from database.repository import (
    save_course_certificate_record,
    get_course_certificate_by_email,
    _normalize_cert_date,
    save_bulk_job_record,
)
from services import course_certificate_service
from services.bulk_job_manager import bulk_job_manager

# Column header synonyms for Course Certificates
HEADER_MAP_CCC = {
    "student_name": [
        "student name", "student_name", "name", "full name",
        "candidate name", "student fullname",
    ],
    "student_email": [
        "student email", "student_email", "email", "email address",
        "mail", "student mail",
    ],
    "course_name": [
        "course name", "course_name", "course", "program name",
        "program", "course title", "course_title", "domain", "domains", "track",
    ],
    "issued_date": [
        "issued date", "issued_date", "issue date", "issue_date",
        "date of issue", "cert date", "certificate date",
        "completion date", "date",
    ],
}


def normalize_column_name(header):
    """Map a raw spreadsheet column header to its canonical field name."""
    if not header:
        return ""
    h = str(header).strip().lower().replace("_", " ")
    # Pass 1: exact match
    for canonical, synonyms in HEADER_MAP_CCC.items():
        if h == canonical or h in synonyms:
            return canonical
    # Pass 2: substring match against longest synonyms first to avoid "name" matching "course name"
    candidates = []
    for canonical, synonyms in HEADER_MAP_CCC.items():
        for syn in synonyms:
            if syn in h:
                candidates.append((len(syn), canonical))
    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]
    return h.replace(" ", "_")


def parse_and_validate_date(date_val):
    """
    Parse and normalize a date string to DD-MM-YYYY.
    Returns (clean_str, None) on success, (None, error_message) on failure.
    """
    if not date_val:
        return None, "Date is missing."
    date_str = str(date_val).strip()

    formats = [
        "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d",
        "%d-%b-%Y", "%d %b %Y", "%d-%B-%Y", "%d %B %Y",
        "%m/%d/%Y", "%m-%d-%Y",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.strftime("%d-%m-%Y"), None
        except ValueError:
            continue

    if "T" in date_str or " " in date_str:
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00")[:10])
            return dt.strftime("%d-%m-%Y"), None
        except Exception:
            pass

    return None, f"Invalid date format: '{date_str}'. Use DD-MM-YYYY or YYYY-MM-DD."


def is_valid_email(email_str):
    if not email_str:
        return False
    email_regex = r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$"
    return bool(re.match(email_regex, str(email_str).strip()))


def parse_csv_content(file_bytes):
    """
    Parse CSV bytes with auto-detection of encoding (UTF-8, Latin-1) and delimiter.
    Returns a list of row dicts with canonical field names.
    """
    text = ""
    for enc in ["utf-8-sig", "utf-8", "latin-1", "cp1252"]:
        try:
            text = file_bytes.decode(enc)
            break
        except Exception:
            continue

    if not text:
        raise ValueError("Could not decode file. Please ensure it is a valid UTF-8 CSV.")

    first_line = text.split("\n")[0] if "\n" in text else text
    delimiter = ","
    if "\t" in first_line:
        delimiter = "\t"
    elif ";" in first_line:
        delimiter = ";"

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = list(reader)
    if not rows:
        return []

    headers = [normalize_column_name(h) for h in rows[0]]
    parsed_rows = []
    for r in rows[1:]:
        if not any(str(c).strip() for c in r):
            continue
        row_dict = {}
        for idx, val in enumerate(r):
            if idx < len(headers):
                row_dict[headers[idx]] = str(val).strip()
        parsed_rows.append(row_dict)

    return parsed_rows


def parse_excel_content(file_bytes):
    """
    Parse Excel (.xlsx / .xls) bytes using openpyxl.
    Returns a list of row dicts with canonical field names.
    """
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
        sheet = wb.active
        rows = list(sheet.iter_rows(values_only=True))
        if not rows:
            return []

        headers = [normalize_column_name(h) for h in rows[0] if h is not None]
        parsed_rows = []
        for r in rows[1:]:
            if not any(c is not None and str(c).strip() for c in r):
                continue
            row_dict = {}
            for idx, val in enumerate(r):
                if idx < len(headers):
                    if isinstance(val, datetime):
                        row_dict[headers[idx]] = val.strftime("%d-%m-%Y")
                    else:
                        row_dict[headers[idx]] = str(val).strip() if val is not None else ""
            parsed_rows.append(row_dict)
        return parsed_rows
    except ImportError:
        return parse_csv_content(file_bytes)


def validate_bulk_ccc_records(parsed_rows):
    """
    Validate parsed rows for Course Certificate bulk upload.
    Detects in-file duplicates using a 4-tuple identity key:
    (name.lower(), email.lower(), course_name.lower(), issued_date_normalized)

    Returns a dict with:
      total_records, valid_records_count, invalid_records_count,
      duplicate_records_count, rows (list of validated row dicts)
    """
    validated_rows = []
    seen_keys = set()
    total = len(parsed_rows)
    valid_count = 0
    invalid_count = 0
    duplicate_count = 0

    for idx, row in enumerate(parsed_rows, start=1):
        errors = []
        name = row.get("student_name", "").strip()
        email = row.get("student_email", "").strip().lower()
        course_name = row.get("course_name", "").strip()
        raw_issued = row.get("issued_date", "").strip()

        if not name:
            errors.append("Student Name is required.")
        if not email:
            errors.append("Student Email is required.")
        elif not is_valid_email(email):
            errors.append(f"Invalid email format: '{email}'")
        if not course_name:
            errors.append("Course Name is required.")

        clean_issued = ""
        if raw_issued:
            parsed_d, date_err = parse_and_validate_date(raw_issued)
            if date_err:
                errors.append(f"Issue Date: {date_err}")
            else:
                clean_issued = parsed_d or ""
        else:
            errors.append("Issue Date is required.")

        # 4-tuple identity key for duplicate detection
        cand_key = (
            name.lower(),
            email.lower(),
            course_name.lower(),
            _normalize_cert_date(clean_issued or raw_issued),
        )
        is_duplicate = False
        if email and is_valid_email(email) and name and course_name:
            if cand_key in seen_keys:
                is_duplicate = True
                duplicate_count += 1
                errors.append("Duplicate certificate record in uploaded file.")
            else:
                seen_keys.add(cand_key)

        is_valid = len(errors) == 0
        if is_valid:
            valid_count += 1
        else:
            invalid_count += 1

        validated_rows.append({
            "row_index": idx,
            "student_name": name,
            "student_email": email,
            "course_name": course_name,
            "issued_date": clean_issued or raw_issued,
            "is_valid": is_valid,
            "is_duplicate": is_duplicate,
            "errors": errors,
            "error_message": "; ".join(errors) if errors else "",
        })

    return {
        "total_records": total,
        "valid_records_count": valid_count,
        "invalid_records_count": invalid_count,
        "duplicate_records_count": duplicate_count,
        "rows": validated_rows,
    }


def get_sample_csv_template_ccc():
    """Generate a sample CSV template for Course Certificate bulk upload (UTF-8 BOM)."""
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow(["Student Name", "Student Email", "Course Name", "Issue Date"])
    writer.writerow(["Priya Sharma", "priya@example.com", "Python Full Stack Web Development", "15-06-2026"])
    writer.writerow(["Rohan Mehta", "rohan@example.com", "Data Science & Machine Learning", "20-07-2026"])
    writer.writerow(["Ananya Singh", "ananya@example.com", "Artificial Intelligence", "01-08-2026"])
    return output.getvalue()


def get_sample_excel_template_ccc():
    """Generate a sample Excel (.xlsx) template for Course Certificate bulk upload."""
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Bulk_Course_Certificates"

        headers = ["Student Name", "Student Email", "Course Name", "Issue Date"]
        ws.append(headers)

        header_fill = PatternFill(start_color="0F172A", end_color="0F172A", fill_type="solid")
        header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")

        for col_idx in range(1, len(headers) + 1):
            cell = ws.cell(row=1, column=col_idx)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")

        sample_rows = [
            ["Priya Sharma", "priya@example.com", "Python Full Stack Web Development", "15-06-2026"],
            ["Rohan Mehta", "rohan@example.com", "Data Science & Machine Learning", "20-07-2026"],
            ["Ananya Singh", "ananya@example.com", "Artificial Intelligence", "01-08-2026"],
        ]
        for r in sample_rows:
            ws.append(r)

        for col in ws.columns:
            max_len = max(len(str(cell.value or "")) for cell in col)
            col_letter = openpyxl.utils.get_column_letter(col[0].column)
            ws.column_dimensions[col_letter].width = max(max_len + 4, 18)

        excel_buffer = io.BytesIO()
        wb.save(excel_buffer)
        excel_bytes = excel_buffer.getvalue()
        excel_buffer.close()
        return excel_bytes
    except Exception as exc:
        print("CCC SAMPLE EXCEL GENERATION ERROR:", repr(exc))
        return None


def generate_bulk_course_certificates(
    valid_rows,
    output_dir,
    base_url=None,
    supabase_client=None,
    job_id=None,
):
    """
    Generate Course Completion Certificate PDFs for all valid rows.

    - Each row gets a fresh unique certificate ID (PXL-CCC-YYYY-XXXXXX).
    - Each record is persisted individually via save_course_certificate_record.
    - Progress is tracked via bulk_job_manager when job_id is provided.
    - Accumulates and returns granular failed_items for retry support.
    """
    resolved_base_url = (base_url or course_certificate_service.get_public_base_url()).rstrip("/")
    results = []
    newly_generated_count = 0
    failed_count = 0
    failed_items = []
    was_cancelled = False

    for item in valid_rows:
        if job_id and bulk_job_manager.is_cancelled(job_id):
            was_cancelled = True
            break

        student_name = str(item.get("student_name") or "").strip()
        student_email = str(item.get("student_email") or "").strip().lower()
        course_name = str(item.get("course_name") or "").strip()
        raw_issued = str(item.get("issued_date") or "").strip()
        row_idx = item.get("row_index")

        try:
            # Normalize issue date — use today as ultimate fallback
            parsed_issued, _ = parse_and_validate_date(raw_issued)
            issued_date = parsed_issued or datetime.now().strftime("%d-%m-%Y")

            # Format for display (e.g. "15 June 2026")
            formatted_issued = course_certificate_service.format_display_date(issued_date)

            # Mint a fresh unique certificate ID
            cert_id = course_certificate_service.generate_certificate_id(supabase_client)

            cert_data = {
                "student_name": student_name,
                "student_email": student_email,
                "course_name": course_name,
                "issued_date": formatted_issued,
                "certificate_id": cert_id,
            }

            # Generate PDF file — returns the filename
            filename = course_certificate_service.generate_certificate_pdf(
                data=cert_data,
                output_dir=output_dir,
                base_url=resolved_base_url,
            )

            # Persist pending record immediately after PDF creation
            pending_record = {
                "certificate_id": cert_id,
                "student_name": student_name,
                "student_email": student_email,
                "course_name": course_name,
                "issued_date": formatted_issued,
                "email_status": "pending",
                "sent_at": None,
                "send_count": 0,
                "pdf_filename": filename,
                "certificate_status": "active",
                "error_message": None,
                "template_version": "v1",
            }
            save_course_certificate_record(pending_record)

            newly_generated_count += 1
            if job_id:
                bulk_job_manager.update_progress(
                    job_id,
                    processed_inc=1,
                    current_item_name=student_name,
                )

            results.append({
                "row_index": row_idx,
                "certificate_id": cert_id,
                "student_name": student_name,
                "student_email": student_email,
                "course_name": course_name,
                "issued_date": formatted_issued,
                "filename": filename,
                "url": f"/generated/{filename}",
                "success": True,
                "status": "newly_generated",
                "persisted": True,
                "error": None,
            })

        except Exception as exc:
            failed_count += 1
            if job_id:
                bulk_job_manager.update_progress(
                    job_id,
                    failed_inc=1,
                    processed_inc=1,
                    current_item_name=str(item.get("student_name") or "Unknown"),
                )
            err_msg = str(exc)
            failed_items.append({
                "row_index": row_idx,
                "student_name": student_name,
                "student_email": student_email,
                "course_name": course_name,
                "issued_date": raw_issued,
                "stage": "generation",
                "error": err_msg,
            })
            results.append({
                "row_index": row_idx,
                "student_name": student_name,
                "student_email": student_email,
                "course_name": course_name,
                "issued_date": raw_issued,
                "success": False,
                "status": "failed",
                "error": err_msg,
            })

    return {
        "total": len(valid_rows),
        "generated_count": newly_generated_count,
        "newly_generated_count": newly_generated_count,
        "failed_count": failed_count,
        "results": results,
        "failed_items": failed_items,
        "cancelled": was_cancelled,
    }


def send_bulk_course_certificate_emails(
    certificate_items,
    output_dir,
    batch_size=5,
    delay_seconds=0.4,
    send_again_all=False,
    job_id=None,
    base_url=None,
):
    """
    Send Course Completion Certificate emails in controlled batches.

    - Tracks per-item success/failure with granular results.
    - Updates the course_certificate_history record after each send.
    - Respects job cancellation via bulk_job_manager.
    - Applies batch throttling to protect the SMTP server.
    """
    resolved_base_url = (base_url or course_certificate_service.get_public_base_url()).rstrip("/")
    total = len(certificate_items)
    successful_count = 0
    failed_count = 0
    skipped_count = 0
    failed_items = []
    results = []
    was_cancelled = False

    # In-batch 4-tuple duplicate guard
    seen_batch_keys = set()

    for i, item in enumerate(certificate_items):
        if job_id and bulk_job_manager.is_cancelled(job_id):
            was_cancelled = True
            break

        cert_id = str(item.get("certificate_id") or "").strip()
        student_email = str(item.get("student_email") or "").strip().lower()
        student_name = str(item.get("student_name") or "Student").strip()
        course_name = str(item.get("course_name") or "").strip()
        raw_issued = str(item.get("issued_date") or "").strip()
        filename = item.get("filename") or item.get("pdf_filename") or ""

        # Build in-batch duplicate key
        batch_item_key = (
            student_name.lower(),
            student_email,
            course_name.lower(),
            _normalize_cert_date(raw_issued),
        ) if not cert_id else (str(cert_id).strip().upper(), student_name.lower())

        if batch_item_key in seen_batch_keys:
            skipped_count += 1
            if job_id:
                bulk_job_manager.update_progress(job_id, processed_inc=1)
            results.append({
                "certificate_id": cert_id,
                "student_email": student_email,
                "student_name": student_name,
                "course_name": course_name,
                "filename": filename,
                "status": "skipped_batch_duplicate",
                "error": None,
            })
            continue

        seen_batch_keys.add(batch_item_key)

        # Resolve existing DB record for this certificate
        existing_rec = None
        if cert_id:
            try:
                existing_rec = get_course_certificate_by_email(student_email)
                # Verify it matches this specific certificate
                if existing_rec:
                    rec_cid = str(existing_rec.get("certificate_id") or "").strip()
                    if rec_cid and rec_cid != cert_id:
                        existing_rec = None  # Different cert ID — don't conflate
            except Exception:
                existing_rec = None

        is_resend = bool(
            existing_rec
            and str(existing_rec.get("email_status") or "").strip().lower() == "sent"
        )

        if is_resend:
            target_id = existing_rec.get("id")
            prev_send_count = int(existing_rec.get("send_count") or 1)
            send_count = prev_send_count + 1
        else:
            target_id = existing_rec.get("id") if existing_rec else None
            prev_send_count = 0
            send_count = 1

        # Normalize issued date for the email template
        parsed_issued, _ = parse_and_validate_date(raw_issued)
        formatted_issued = course_certificate_service.format_display_date(
            parsed_issued or raw_issued or datetime.now().strftime("%d-%m-%Y")
        )

        cert_data = {
            "student_name": student_name,
            "student_email": student_email,
            "course_name": course_name,
            "issued_date": formatted_issued,
            "certificate_id": cert_id,
        }

        try:
            # Re-generate PDF in case it was cleaned up, then send email
            filename = course_certificate_service.generate_certificate_pdf(
                data=cert_data,
                output_dir=Path(output_dir),
                base_url=resolved_base_url,
            )
            pdf_path = Path(output_dir) / filename

            course_certificate_service.send_certificate_email(
                data=cert_data,
                pdf_path=pdf_path,
                filename=filename,
                base_url=resolved_base_url,
            )

            successful_count += 1
            if job_id:
                bulk_job_manager.update_progress(job_id, sent_inc=1, processed_inc=1)
            now_iso = datetime.now().isoformat()

            success_record = {
                "certificate_id": cert_id,
                "student_name": student_name,
                "student_email": student_email,
                "course_name": course_name,
                "issued_date": formatted_issued,
                "email_status": "sent",
                "sent_at": now_iso,
                "send_count": send_count,
                "pdf_filename": filename,
                "error_message": None,
                "template_version": "v1",
            }
            if is_resend and target_id:
                success_record["id"] = target_id
                success_record["send_again"] = True
            elif target_id:
                success_record["id"] = target_id

            save_course_certificate_record(
                success_record,
                existing_id=target_id if is_resend else None,
            )

            results.append({
                "certificate_id": cert_id,
                "student_email": student_email,
                "student_name": student_name,
                "course_name": course_name,
                "filename": filename,
                "status": "sent",
                "error": None,
            })

        except Exception as exc:
            failed_count += 1
            if job_id:
                bulk_job_manager.update_progress(job_id, failed_inc=1, processed_inc=1)
            now_iso = datetime.now().isoformat()
            err_msg = str(exc)

            fail_record = {
                "certificate_id": cert_id,
                "student_name": student_name,
                "student_email": student_email,
                "course_name": course_name,
                "issued_date": formatted_issued,
                "email_status": "failed",
                "sent_at": None,
                "send_count": prev_send_count + 1 if is_resend else 0,
                "pdf_filename": filename,
                "error_message": err_msg,
                "template_version": "v1",
            }
            if is_resend and target_id:
                fail_record["id"] = target_id
                fail_record["send_again"] = True
            elif target_id:
                fail_record["id"] = target_id

            save_course_certificate_record(
                fail_record,
                existing_id=target_id if is_resend else None,
            )

            failed_items.append({
                "certificate_id": cert_id,
                "student_name": student_name,
                "student_email": student_email,
                "course_name": course_name,
                "filename": filename,
                "stage": "email",
                "error": err_msg,
            })
            results.append({
                "certificate_id": cert_id,
                "student_email": student_email,
                "student_name": student_name,
                "course_name": course_name,
                "filename": filename,
                "status": "failed",
                "error": err_msg,
            })

        # Batch throttling delay
        if (i + 1) % batch_size == 0 and (i + 1) < total:
            time.sleep(delay_seconds)

    # Persist bulk job record for audit trail
    job_status = "cancelled" if was_cancelled else "completed"
    try:
        save_bulk_job_record({
            "job_type": "course_certificate_bulk_email",
            "total_records": total,
            "successful_count": successful_count,
            "failed_count": failed_count,
            "status": job_status,
            "details": (
                f"Sent {successful_count}/{total} course certificate emails successfully."
                if not was_cancelled
                else f"Cancelled after sending {successful_count}/{total} course certificate emails."
            ),
        })
    except Exception as exc:
        print("CCC BULK EMAIL JOB RECORD SAVE WARNING:", repr(exc))

    return {
        "total": total,
        "successful_count": successful_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "results": results,
        "failed_items": failed_items,
        "cancelled": was_cancelled,
    }


def parse_ccc_file_for_ui(file_bytes, filename):
    """
    Parse an uploaded CSV/Excel file for the CCC Bulk textarea UI.

    Extracts Student Name, Student Email, and Domain (course name) columns.
    Returns a dict with:
      success, names_text, emails_text, domains_text,
      total_rows, valid_count, invalid_count, duplicate_count,
      filename, candidates, invalid_rows
    """
    ext = Path(filename).suffix.lower()
    if ext not in (".csv", ".xlsx", ".xls"):
        return {
            "success": False,
            "error": "Please upload a CSV or Excel file (.csv, .xlsx, .xls).",
        }

    try:
        if ext in (".xlsx", ".xls"):
            parsed_rows = parse_excel_content(file_bytes)
        else:
            parsed_rows = parse_csv_content(file_bytes)
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    if not parsed_rows:
        return {"success": False, "error": "The uploaded file is empty or has invalid/missing headers."}

    names_lines = []
    emails_lines = []
    domains_lines = []
    candidates = []
    invalid_rows = []
    seen_emails = set()
    duplicate_count = 0
    valid_count = 0
    invalid_count = 0

    for idx, row in enumerate(parsed_rows, start=1):
        name = row.get("student_name", "").strip()
        email = row.get("student_email", "").strip().lower()
        domain = row.get("course_name", "").strip()

        # Always add to textarea lines (preserves line alignment)
        names_lines.append(name)
        emails_lines.append(email)
        domains_lines.append(domain)

        errors = []
        if not name:
            errors.append("Student Name is missing")
        if not email:
            errors.append("Email is missing")
        elif not is_valid_email(email):
            errors.append(f"Invalid email: '{email}'")
        if not domain:
            errors.append("Domain is missing")

        if errors:
            invalid_count += 1
            invalid_rows.append({
                "row_num": idx,
                "name": name or "—",
                "email": email or "—",
                "domain": domain or "—",
                "reason": "; ".join(errors),
            })
        else:
            if email in seen_emails:
                duplicate_count += 1
            else:
                seen_emails.add(email)
                valid_count += 1
                candidates.append({
                    "row_num": idx,
                    "name": name,
                    "email": email,
                    "domain": domain,
                })

    return {
        "success": True,
        "filename": filename,
        "names_text": "\n".join(names_lines),
        "emails_text": "\n".join(emails_lines),
        "domains_text": "\n".join(domains_lines),
        "total_rows": len(parsed_rows),
        "valid_count": valid_count,
        "invalid_count": invalid_count,
        "duplicate_count": duplicate_count,
        "candidates": candidates,
        "invalid_rows": invalid_rows,
    }
