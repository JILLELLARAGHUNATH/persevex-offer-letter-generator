import io
import re
import csv
import time
from datetime import datetime
from pathlib import Path

from database.repository import (
    save_certificate_record,
    bulk_save_certificate_records,
    find_existing_certificates_batch,
    get_previous_sent_certificate_by_email,
    save_bulk_job_record,
)
import certificate_service
from services.bulk_job_manager import bulk_job_manager

# Column Header Synonyms
HEADER_MAP = {
    "student_name": ["student name", "student_name", "name", "full name", "candidate name", "student fullname"],
    "student_email": ["student email", "student_email", "email", "email address", "mail", "student mail"],
    "domain": ["domain", "internship domain", "internship_domain", "track", "field", "stream"],
    "start_date": ["start date", "start_date", "from date", "start", "commencement date"],
    "end_date": ["end date", "end_date", "to date", "end", "completion date"],
    "issued_date": ["issued date", "issued_date", "issue date", "date of issue", "cert date", "certificate date"],
}


def normalize_column_name(header):
    if not header:
        return ""
    h = str(header).strip().lower().replace("_", " ")
    for canonical, synonyms in HEADER_MAP.items():
        if h in synonyms:
            return canonical
        for syn in synonyms:
            if syn in h:
                return canonical
    return h.replace(" ", "_")


def parse_and_validate_date(date_val):
    if not date_val:
        return None, "Date is missing."
    date_str = str(date_val).strip()

    # Supported date formats
    formats = [
        "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d",
        "%d-%b-%Y", "%d %b %Y", "%d-%B-%Y", "%d %B %Y",
        "%m/%d/%Y", "%m-%d-%Y"
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.strftime("%d-%m-%Y"), None
        except ValueError:
            continue

    # Try ISO date with timestamp
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

    # Detect delimiter
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
            continue  # Skip empty rows
        row_dict = {}
        for idx, val in enumerate(r):
            if idx < len(headers):
                row_dict[headers[idx]] = str(val).strip()
        parsed_rows.append(row_dict)

    return parsed_rows


def parse_excel_content(file_bytes):
    """
    Parse Excel (.xlsx / .xls) bytes using openpyxl if available.
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
        # Fallback to CSV parsing if openpyxl is not installed
        return parse_csv_content(file_bytes)


def validate_bulk_records(parsed_rows):
    """
    Validate parsed rows and return structured summary and validated records.
    """
    validated_rows = []
    seen_emails = set()
    total = len(parsed_rows)
    valid_count = 0
    invalid_count = 0
    duplicate_count = 0

    for idx, row in enumerate(parsed_rows, start=1):
        errors = []
        name = row.get("student_name", "").strip()
        email = row.get("student_email", "").strip().lower()
        domain = row.get("domain", "").strip()
        raw_start = row.get("start_date", "").strip()
        raw_end = row.get("end_date", "").strip()
        raw_issued = row.get("issued_date", "").strip()

        if not name:
            errors.append("Student Name is required.")
        if not email:
            errors.append("Student Email is required.")
        elif not is_valid_email(email):
            errors.append(f"Invalid email format: '{email}'")

        if not domain:
            errors.append("Internship Domain is required.")

        clean_start, start_err = parse_and_validate_date(raw_start)
        if start_err:
            errors.append(f"Start Date: {start_err}")

        clean_end, end_err = parse_and_validate_date(raw_end)
        if end_err:
            errors.append(f"End Date: {end_err}")

        clean_issued, issued_err = parse_and_validate_date(raw_issued)
        if issued_err:
            errors.append(f"Issued Date: {issued_err}")

        is_duplicate = False
        if email:
            if email in seen_emails:
                is_duplicate = True
                duplicate_count += 1
                errors.append("Duplicate email in uploaded file.")
            else:
                seen_emails.add(email)

        is_valid = len(errors) == 0
        if is_valid:
            valid_count += 1
        else:
            invalid_count += 1

        validated_rows.append({
            "row_index": idx,
            "student_name": name,
            "student_email": email,
            "domain": domain,
            "start_date": clean_start or raw_start,
            "end_date": clean_end or raw_end,
            "issued_date": clean_issued or raw_issued,
            "is_valid": is_valid,
            "is_duplicate": is_duplicate,
            "errors": errors,
            "error_message": "; ".join(errors) if errors else ""
        })

    return {
        "total_records": total,
        "valid_records_count": valid_count,
        "invalid_records_count": invalid_count,
        "duplicate_records_count": duplicate_count,
        "rows": validated_rows
    }


def get_sample_csv_template():
    """Generate sample CSV template content with UTF-8 BOM."""
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow(["Student Name", "Student Email", "Domain", "Start Date", "End Date", "Issued Date"])
    writer.writerow(["Vaikunthi Shivraj Jadhav", "vaikunthi@example.com", "Data Science", "11-01-2026", "11-02-2026", "15-02-2026"])
    writer.writerow(["Raghunath Reddy", "raghunath@example.com", "Python Development", "09-09-2026", "12-12-2026", "08-09-2026"])
    writer.writerow(["Aarav Sharma", "aarav@example.com", "Artificial Intelligence", "10-01-2026", "10-02-2026", "15-02-2026"])
    return output.getvalue()


def get_sample_excel_template():
    """Generate sample Excel (.xlsx) template with openpyxl styling."""
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Bulk_Certificates"

        headers = ["Student Name", "Student Email", "Domain", "Start Date", "End Date", "Issued Date"]
        ws.append(headers)

        header_fill = PatternFill(start_color="0F172A", end_color="0F172A", fill_type="solid")
        header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")

        for col_idx in range(1, len(headers) + 1):
            cell = ws.cell(row=1, column=col_idx)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")

        rows = [
            ["Vaikunthi Shivraj Jadhav", "vaikunthi@example.com", "Data Science", "11-01-2026", "11-02-2026", "15-02-2026"],
            ["Raghunath Reddy", "raghunath@example.com", "Python Development", "09-09-2026", "12-12-2026", "08-09-2026"],
            ["Aarav Sharma", "aarav@example.com", "Artificial Intelligence", "10-01-2026", "10-02-2026", "15-02-2026"],
        ]

        for r in rows:
            ws.append(r)

        for col in ws.columns:
            max_len = max(len(str(cell.value or '')) for cell in col)
            col_letter = openpyxl.utils.get_column_letter(col[0].column)
            ws.column_dimensions[col_letter].width = max(max_len + 4, 15)

        excel_buffer = io.BytesIO()
        wb.save(excel_buffer)
        excel_bytes = excel_buffer.getvalue()
        excel_buffer.close()
        return excel_bytes
    except Exception as exc:
        print("SAMPLE EXCEL GENERATION ERROR:", repr(exc))
        return None



def generate_bulk_certificates(valid_rows, output_dir, base_url="https://persevex.vercel.app", supabase_client=None, job_id=None):
    """
    Generate certificate PDFs for all valid rows with resume safety (idempotent):
    - Identifies and skips records with existing active certificates in DB
    - Generates new certificates only for missing records
    - Accumulates and returns granular failed_items
    - Bulk saves new records in a single database transaction/request
    """
    results = []
    already_exists_count = 0
    newly_generated_count = 0
    failed_count = 0
    failed_items = []
    records_to_save = []
    was_cancelled = False

    # 1. Batch lookup to detect existing certificates
    try:
        existing_lookup = find_existing_certificates_batch(valid_rows, supabase_client=supabase_client)
    except Exception as exc:
        print("EXISTING CERTIFICATES BATCH LOOKUP ERROR:", repr(exc))
        existing_lookup = {}

    for item in valid_rows:
        if job_id and bulk_job_manager.is_cancelled(job_id):
            was_cancelled = True
            break
        try:
            student_name = str(item.get("student_name") or "").strip()
            student_email = str(item.get("student_email") or "").strip()
            domain = str(item.get("domain") or "").strip()
            start_date = str(item.get("start_date") or "").strip()
            end_date = str(item.get("end_date") or "").strip()
            issued_date = str(item.get("issued_date") or "").strip()
            row_idx = item.get("row_index")

            email_clean = student_email.lower()
            domain_clean = domain.lower()

            # Check if this certificate already exists in DB
            existing = existing_lookup.get((email_clean, domain_clean, start_date, end_date))

            if existing and existing.get("certificate_id") and str(existing.get("certificate_status") or "").lower() != "revoked":
                # RESUME CASE: Certificate already exists in DB!
                cert_id = str(existing.get("certificate_id")).strip()
                filename = str(existing.get("pdf_filename") or f"{cert_id}.pdf").strip()

                # Ensure physical PDF exists locally for instant preview / download / email
                target_pdf = Path(output_dir) / filename
                if not target_pdf.exists():
                    try:
                        filename = certificate_service.generate_certificate_pdf(
                            {
                                "student_name": existing.get("student_name") or student_name,
                                "student_email": existing.get("student_email") or student_email,
                                "domain": existing.get("internship_domain") or domain,
                                "start_date": existing.get("start_date") or start_date,
                                "end_date": existing.get("end_date") or end_date,
                                "issued_date": existing.get("issued_date") or issued_date,
                                "certificate_id": cert_id,
                            },
                            output_dir,
                            base_url=base_url
                        )
                    except Exception as render_exc:
                        print(f"WARNING: Re-rendering existing certificate {cert_id} PDF failed:", repr(render_exc))

                already_exists_count += 1
                results.append({
                    "row_index": row_idx,
                    "certificate_id": cert_id,
                    "student_name": existing.get("student_name") or student_name,
                    "student_email": student_email,
                    "domain": existing.get("internship_domain") or domain,
                    "start_date": existing.get("start_date") or start_date,
                    "end_date": existing.get("end_date") or end_date,
                    "issued_date": existing.get("issued_date") or issued_date,
                    "filename": filename,
                    "url": f"/generated/{filename}",
                    "success": True,
                    "status": "already_exists",
                    "error": None
                })
            else:
                # NEW CERTIFICATE GENERATION CASE
                cert_id = certificate_service.generate_certificate_id(supabase_client)
                cert_data = {
                    "student_name": student_name,
                    "student_email": student_email,
                    "domain": domain,
                    "start_date": start_date,
                    "end_date": end_date,
                    "issued_date": issued_date,
                    "certificate_id": cert_id,
                }

                filename = certificate_service.generate_certificate_pdf(
                    cert_data,
                    output_dir,
                    base_url=base_url
                )

                record = {
                    "certificate_id": cert_id,
                    "student_name": student_name,
                    "student_email": student_email,
                    "internship_domain": domain,
                    "start_date": start_date,
                    "end_date": end_date,
                    "issued_date": issued_date,
                    "email_status": "pending",
                    "pdf_filename": filename,
                    "send_count": 0,
                    "certificate_status": "active",
                }
                records_to_save.append(record)

                newly_generated_count += 1
                results.append({
                    "row_index": row_idx,
                    "certificate_id": cert_id,
                    "student_name": student_name,
                    "student_email": student_email,
                    "domain": domain,
                    "start_date": start_date,
                    "end_date": end_date,
                    "issued_date": issued_date,
                    "filename": filename,
                    "url": f"/generated/{filename}",
                    "success": True,
                    "status": "newly_generated",
                    "error": None
                })
            if job_id:
                bulk_job_manager.update_progress(job_id, processed_inc=1)
        except Exception as exc:
            failed_count += 1
            if job_id:
                bulk_job_manager.update_progress(job_id, failed_inc=1)
            err_msg = str(exc)
            failed_items.append({
                "row_index": item.get("row_index"),
                "student_name": item.get("student_name", "Unknown"),
                "student_email": item.get("student_email", ""),
                "domain": item.get("domain", ""),
                "start_date": item.get("start_date", ""),
                "end_date": item.get("end_date", ""),
                "issued_date": item.get("issued_date", ""),
                "stage": "generation",
                "error": err_msg
            })
            results.append({
                "row_index": item.get("row_index"),
                "student_name": item.get("student_name"),
                "student_email": item.get("student_email"),
                "domain": item.get("domain"),
                "success": False,
                "status": "failed",
                "error": err_msg
            })

    # Bulk save accumulated new records in a single database transaction / request
    if records_to_save:
        try:
            bulk_save_certificate_records(records_to_save)
        except Exception as exc:
            print("BULK SAVE CERTIFICATES REPOSITORY WARNING:", repr(exc))
            for rec in records_to_save:
                try:
                    save_certificate_record(rec)
                except Exception:
                    pass

    return {
        "total": len(valid_rows),
        "generated_count": newly_generated_count + already_exists_count,
        "newly_generated_count": newly_generated_count,
        "already_exists_count": already_exists_count,
        "failed_count": failed_count,
        "results": results,
        "failed_items": failed_items,
        "cancelled": was_cancelled,
    }



def send_bulk_certificate_emails(certificate_items, output_dir, batch_size=5, delay_seconds=0.4, skip_duplicate_emails=None, send_again_all=False, job_id=None):
    """
    Send emails in safe, controlled batches with live progress tracking and failure visibility.
    Supports skipping historical duplicates and guarantees that the same email is never sent twice in one batch.
    """
    total = len(certificate_items)
    successful_count = 0
    failed_count = 0
    skipped_count = 0
    failed_items = []
    results = []
    was_cancelled = False

    skip_set = set()
    if skip_duplicate_emails and not send_again_all:
        for em in skip_duplicate_emails:
            if em:
                skip_set.add(str(em).strip().lower())

    seen_batch_emails = set()

    for i, item in enumerate(certificate_items):
        if job_id and bulk_job_manager.is_cancelled(job_id):
            was_cancelled = True
            break

        cert_id = item.get("certificate_id")
        student_email = str(item.get("student_email") or "").strip()
        student_name = str(item.get("student_name") or "Student").strip()
        domain = str(item.get("domain") or item.get("internship_domain") or "Internship").strip()
        filename = item.get("filename") or item.get("pdf_filename")
        norm_email = student_email.lower()

        # In-batch duplicate guard: Never send the same email twice in one bulk operation
        if norm_email in seen_batch_emails:
            skipped_count += 1
            if job_id:
                bulk_job_manager.update_progress(job_id, processed_inc=1)
            results.append({
                "certificate_id": cert_id,
                "student_email": student_email,
                "student_name": student_name,
                "domain": domain,
                "filename": filename,
                "status": "skipped_batch_duplicate",
                "error": None
            })
            continue

        # Historical duplicate skip guard (when "Send Only to New" is chosen)
        if norm_email in skip_set:
            skipped_count += 1
            if job_id:
                bulk_job_manager.update_progress(job_id, processed_inc=1)
            results.append({
                "certificate_id": cert_id,
                "student_email": student_email,
                "student_name": student_name,
                "domain": domain,
                "filename": filename,
                "status": "skipped_historical_duplicate",
                "error": None
            })
            continue

        seen_batch_emails.add(norm_email)

        prev_rec = get_previous_sent_certificate_by_email(student_email)
        is_resend = bool(prev_rec and str(prev_rec.get("email_status") or "").strip().lower() == "sent")
        target_id = prev_rec.get("id") if is_resend else None
        if is_resend and prev_rec:
            cert_id = str(prev_rec.get("certificate_id") or cert_id).strip()
            send_count = (int(prev_rec.get("send_count") or 1) + 1)
        else:
            send_count = 1

        email_data = {
            "student_email": student_email,
            "student_name": student_name,
            "domain": domain,
            "start_date": item.get("start_date") or (prev_rec.get("start_date") if prev_rec else ""),
            "end_date": item.get("end_date") or (prev_rec.get("end_date") if prev_rec else ""),
            "issued_date": item.get("issued_date") or (prev_rec.get("issued_date") if prev_rec else ""),
            "certificate_id": cert_id,
            "template_version": item.get("template_version") or "v1",
        }

        try:
            filename = certificate_service.generate_certificate_pdf(
                data=email_data,
                output_dir=Path(output_dir),
                template_version=email_data.get("template_version", "v1")
            )
            pdf_path = Path(output_dir) / filename

            certificate_service.send_certificate_email(email_data, pdf_path, filename)

            successful_count += 1
            if job_id:
                bulk_job_manager.update_progress(job_id, sent_inc=1)
            now_iso = datetime.now().isoformat()

            # Update DB record: in-place resend update if previous sent record exists, else insert
            save_payload = {
                "certificate_id": cert_id,
                "student_name": student_name,
                "student_email": student_email,
                "internship_domain": domain,
                "start_date": email_data["start_date"],
                "end_date": email_data["end_date"],
                "issued_date": email_data["issued_date"],
                "email_status": "sent",
                "sent_at": now_iso,
                "send_count": send_count,
                "pdf_filename": filename,
                "error_message": None,
                "template_version": email_data.get("template_version", "v1"),
            }
            if is_resend and target_id:
                save_payload["id"] = target_id
                save_payload["send_again"] = True

            save_certificate_record(save_payload, existing_id=target_id if is_resend else None)

            results.append({
                "certificate_id": cert_id,
                "student_email": student_email,
                "student_name": student_name,
                "domain": domain,
                "filename": filename,
                "status": "sent",
                "error": None
            })
        except Exception as exc:
            failed_count += 1
            if job_id:
                bulk_job_manager.update_progress(job_id, failed_inc=1)
            now_iso = datetime.now().isoformat()
            err_msg = str(exc)

            prev_rec = get_previous_sent_certificate_by_email(student_email)
            is_resend = bool(prev_rec and str(prev_rec.get("email_status") or "").strip().lower() == "sent")
            target_id = prev_rec.get("id") if is_resend else None
            prev_count = int(prev_rec.get("send_count") or 0) if prev_rec else 0

            fail_payload = {
                "certificate_id": cert_id,
                "student_name": student_name,
                "student_email": student_email,
                "internship_domain": domain,
                "start_date": item.get("start_date") or (prev_rec.get("start_date") if prev_rec else ""),
                "end_date": item.get("end_date") or (prev_rec.get("end_date") if prev_rec else ""),
                "issued_date": item.get("issued_date") or (prev_rec.get("issued_date") if prev_rec else ""),
                "email_status": "failed",
                "sent_at": now_iso,
                "send_count": prev_count + 1,
                "pdf_filename": filename,
                "error_message": err_msg,
                "template_version": item.get("template_version") or "v1",
            }
            if is_resend and target_id:
                fail_payload["id"] = target_id
                fail_payload["send_again"] = True

            save_certificate_record(fail_payload, existing_id=target_id if is_resend else None)

            failed_items.append({
                "certificate_id": cert_id,
                "student_name": student_name,
                "student_email": student_email,
                "domain": domain,
                "filename": filename,
                "stage": "email",
                "error": err_msg
            })

            results.append({
                "certificate_id": cert_id,
                "student_email": student_email,
                "student_name": student_name,
                "domain": domain,
                "filename": filename,
                "status": "failed",
                "error": err_msg
            })

        # Batch throttling delay
        if (i + 1) % batch_size == 0 and (i + 1) < total:
            time.sleep(delay_seconds)

    # Save job record
    job_status = "cancelled" if was_cancelled else "completed"
    save_bulk_job_record({
        "job_type": "certificate_bulk_email",
        "total_records": total,
        "successful_count": successful_count,
        "failed_count": failed_count,
        "status": job_status,
        "details": f"Sent {successful_count}/{total} certificate emails successfully." if not was_cancelled else f"Cancelled after sending {successful_count}/{total} certificate emails."
    })

    return {
        "total": total,
        "successful_count": successful_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "results": results,
        "failed_items": failed_items,
        "cancelled": was_cancelled,
    }
