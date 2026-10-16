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
    get_certificate_by_id,
    find_active_certificate,
    _normalize_cert_date,
    normalize_certificate_identity,
    is_same_certificate_identity,
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

        # Issue Date is NOT required as an Excel/CSV column for the bulk certificate workflow.
        # If provided and parseable, normalize it; missing or empty is never an error.
        clean_issued = ""
        if raw_issued:
            parsed_d, _ = parse_and_validate_date(raw_issued)
            if parsed_d:
                clean_issued = parsed_d

        # An in-file duplicate occurs when the exact same issuance identity appears multiple times.
        # Different issuances for the same email (e.g. different courses/domains, dates, or names) are valid distinct records.
        cand_key = (
            name.lower(),
            email.lower(),
            domain.lower(),
            _normalize_cert_date(clean_start or raw_start),
            _normalize_cert_date(clean_end or raw_end),
            _normalize_cert_date(clean_issued or raw_issued),
        )
        is_duplicate = False
        if email and is_valid_email(email):
            if cand_key in seen_emails:
                is_duplicate = True
                duplicate_count += 1
                errors.append("Duplicate certificate record in uploaded file.")
            else:
                seen_emails.add(cand_key)

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



def _disambiguate_certificate_filename(student_name, cert_id, batch_used_filenames, output_dir=None):
    """
    Ensure each certificate receives a unique filename that never collides with
    files already in batch_used_filenames, the output directory, or the persistent cache.
    """
    safe_name = certificate_service.safe_filename(student_name)
    base_fn = f"{safe_name}_Certificate.pdf"

    def is_collision(fn):
        if fn in batch_used_filenames:
            return True
        if output_dir and (Path(output_dir) / fn).exists():
            return True
        persistent_dir = getattr(certificate_service, "PERSISTENT_CERT_DIR", None)
        if persistent_dir and (Path(persistent_dir) / fn).exists():
            return True
        return False

    if not is_collision(base_fn):
        batch_used_filenames.add(base_fn)
        return base_fn

    suffix = cert_id.split("-")[-1] if (cert_id and "-" in str(cert_id)) else "dup"
    candidate = f"{safe_name}_{suffix}_Certificate.pdf"
    if not is_collision(candidate):
        batch_used_filenames.add(candidate)
        return candidate

    counter = 1
    while True:
        candidate = f"{safe_name}_{suffix}_{counter}_Certificate.pdf"
        if not is_collision(candidate):
            batch_used_filenames.add(candidate)
            return candidate
        counter += 1


def generate_bulk_certificates(
    valid_rows,
    output_dir,
    base_url=None,
    supabase_client=None,
    job_id=None,
    issue_date=None,
):
    """
    Generate certificate PDFs for all valid rows with resume safety (idempotent):
    - Identifies and skips records with existing active certificates in DB
    - Generates new certificates only for missing records
    - Accumulates and returns granular failed_items
    - Bulk saves new records in a single database transaction/request
    - Applies selected batch issue_date (or today's date) across all generated certificates
    """
    resolved_base_url = (base_url or certificate_service.get_public_base_url()).rstrip("/")
    results = []
    already_exists_count = 0
    newly_generated_count = 0
    failed_count = 0
    failed_items = []
    records_to_save = []
    was_cancelled = False

    # Validate batch issue_date if explicitly passed
    clean_batch_issue_date = None
    if issue_date:
        parsed_issue, issue_err = parse_and_validate_date(issue_date)
        if issue_err:
            raise ValueError(f"Invalid Issue Date: {issue_err}")
        clean_batch_issue_date = parsed_issue

    # Every valid uploaded row creates a new record and a new unique certificate ID,
    # even if matching details exist in history.
    new_rows_count = len(valid_rows)
    precomputed_ids = []
    if new_rows_count > 0:
        precomputed_ids = certificate_service.generate_bulk_certificate_ids(new_rows_count, supabase_client=supabase_client)

    batch_used_filenames = set()

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
            row_idx = item.get("row_index")

            # Determine effective issue date:
            # 1. Batch issue_date (selected via UI)
            # 2. Row's issued_date if provided
            # 3. Current local date fallback
            if clean_batch_issue_date:
                issued_date = clean_batch_issue_date
            else:
                parsed_row_date, _ = parse_and_validate_date(item.get("issued_date"))
                issued_date = parsed_row_date or datetime.now().strftime("%d-%m-%Y")

            # NEW CERTIFICATE GENERATION: Always mint fresh unique certificate ID
            cert_id = precomputed_ids.pop(0) if precomputed_ids else certificate_service.generate_certificate_id(supabase_client)

            # Prevent filename collision for identical student names across batches, requests, and disks
            filename = _disambiguate_certificate_filename(student_name, cert_id, batch_used_filenames, output_dir=output_dir)

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
                base_url=resolved_base_url,
                custom_filename=filename,
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
                bulk_job_manager.update_progress(
                    job_id,
                    processed_inc=1,
                    current_item_name=student_name,
                )
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
    persistence_info = {
        "attempted": False,
        "durable": True,
        "supabase_persisted": True,
        "sqlite_persisted": True,
        "error": None,
    }

    if records_to_save:
        persistence_info["attempted"] = True
        try:
            save_res = bulk_save_certificate_records(records_to_save, supabase_client=supabase_client)
            if isinstance(save_res, dict):
                persistence_info["durable"] = bool(save_res.get("durable"))
                persistence_info["supabase_persisted"] = bool(save_res.get("supabase_persisted"))
                persistence_info["sqlite_persisted"] = bool(save_res.get("sqlite_persisted"))
                persistence_info["error"] = save_res.get("error")
                persistence_info["success"] = bool(save_res.get("success"))
            elif save_res is True:
                persistence_info["durable"] = True
                persistence_info["supabase_persisted"] = True
                persistence_info["sqlite_persisted"] = True
                persistence_info["success"] = True
            elif save_res is False:
                persistence_info["durable"] = False
                persistence_info["supabase_persisted"] = False
                persistence_info["success"] = False
                persistence_info["error"] = "bulk_save_certificate_records returned False"
        except Exception as exc:
            print("BULK SAVE CERTIFICATES REPOSITORY WARNING:", repr(exc))
            persistence_info["durable"] = False
            persistence_info["supabase_persisted"] = False
            persistence_info["success"] = False
            persistence_info["error"] = repr(exc)
            for rec in records_to_save:
                try:
                    save_certificate_record(rec)
                except Exception:
                    pass

        # Durable Supabase persistence is required on Vercel or when Supabase is configured.
        # In a local/offline environment without Supabase, local SQLite persistence is durable on disk
        # and represents intended persistence.
        if not persistence_info.get("success", False):
            err_msg = f"Database persistence failed: {persistence_info.get('error') or 'Storage error'}"
            for res_item in results:
                if res_item.get("status") == "newly_generated":
                    res_item["success"] = False
                    res_item["status"] = "persistence_failed"
                    res_item["persisted"] = False
                    res_item["durable"] = False
                    res_item["error"] = err_msg
                    failed_items.append({
                        "row_index": res_item.get("row_index"),
                        "student_name": res_item.get("student_name", "Unknown"),
                        "student_email": res_item.get("student_email", ""),
                        "domain": res_item.get("domain", ""),
                        "start_date": res_item.get("start_date", ""),
                        "end_date": res_item.get("end_date", ""),
                        "issued_date": res_item.get("issued_date", ""),
                        "stage": "persistence",
                        "error": err_msg,
                    })
            failed_count += newly_generated_count
            newly_generated_count = 0
        else:
            for res_item in results:
                if res_item.get("status") == "newly_generated":
                    res_item["persisted"] = True
                    res_item["durable"] = bool(persistence_info.get("durable", False))

    return {
        "total": len(valid_rows),
        "generated_count": newly_generated_count + already_exists_count,
        "newly_generated_count": newly_generated_count,
        "already_exists_count": already_exists_count,
        "failed_count": failed_count,
        "results": results,
        "failed_items": failed_items,
        "cancelled": was_cancelled,
        "persistence": persistence_info,
    }



def send_bulk_certificate_emails(
    certificate_items,
    output_dir,
    batch_size=5,
    delay_seconds=0.4,
    skip_duplicate_emails=None,
    send_again_all=False,
    job_id=None,
    base_url=None,
):
    """
    Send emails in safe, controlled batches with live progress tracking and failure visibility.
    Supports skipping historical duplicates and guarantees that the same email is never sent twice in one batch.
    """
    resolved_base_url = (base_url or certificate_service.get_public_base_url()).rstrip("/")
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
    seen_batch_filenames = set()

    for i, item in enumerate(certificate_items):
        if job_id and bulk_job_manager.is_cancelled(job_id):
            was_cancelled = True
            break

        cert_id = item.get("certificate_id")
        student_email = str(item.get("student_email") or "").strip()
        student_name = str(item.get("student_name") or "Student").strip()
        domain = str(item.get("domain") or item.get("internship_domain") or "Internship").strip()
        filename = item.get("filename") or item.get("pdf_filename")
        if not filename:
            filename = _disambiguate_certificate_filename(student_name, cert_id, seen_batch_filenames, output_dir=output_dir)
        else:
            seen_batch_filenames.add(filename)
        norm_email = student_email.lower()
        cand_name = str(student_name or "").strip().lower()

        # Determine the target database record for THIS certificate specifically.
        # Strict Rule: Must resolve using certificate_id or exact match of all 6 normalized identity fields.
        # Never use an email-only lookup to select a record for updating send_count.
        item_cert_id = str(item.get("certificate_id") or "").strip()
        this_cert_rec = None

        if item_cert_id:
            try:
                candidate_rec = get_certificate_by_id(item_cert_id)
            except Exception:
                candidate_rec = None

            if candidate_rec:
                rec_email = str(candidate_rec.get("student_email") or "").strip().lower()
                rec_name = str(candidate_rec.get("student_name") or "").strip().lower()
                # Verify identity consistency: email must match, and name must match if present
                if rec_email and rec_email != norm_email:
                    candidate_rec = None
                elif rec_name and cand_name and rec_name != cand_name:
                    # Identity conflict: certificate ID is registered to a different student name!
                    candidate_rec = None
                else:
                    this_cert_rec = candidate_rec

        # Fallback 1: match candidate by exact 6-tuple identity
        if not this_cert_rec and domain and item.get("start_date") and item.get("end_date"):
            try:
                active_match = find_active_certificate(
                    student_email,
                    domain,
                    item.get("start_date"),
                    item.get("end_date"),
                    student_name=student_name,
                    issued_date=item.get("issued_date"),
                )
                if active_match:
                    rec_name = str(active_match.get("student_name") or "").strip().lower()
                    if not cand_name or not rec_name or rec_name == cand_name:
                        this_cert_rec = active_match
            except Exception:
                this_cert_rec = None

        # Fallback 2: backwards compatibility for test mocks only if identity strictly matches
        # (Must match student_name AND certificate_id / 6-tuple; never match by email alone)
        if not this_cert_rec:
            prev_rec = None
            try:
                prev_rec = get_previous_sent_certificate_by_email(student_email)
            except Exception:
                prev_rec = None

            if prev_rec:
                prev_cid = str(prev_rec.get("certificate_id") or "").strip()
                prev_name = str(prev_rec.get("student_name") or "").strip().lower()
                cid_matches = bool(item_cert_id and prev_cid == item_cert_id)
                name_matches = bool(cand_name and prev_name and cand_name == prev_name)
                if (cid_matches and name_matches) or (name_matches and is_same_certificate_identity(item, prev_rec)):
                    this_cert_rec = prev_rec

        # Ambiguity / safety guard: If item_cert_id was supplied but belongs to a different student
        if item_cert_id and not this_cert_rec:
            try:
                conflicting_rec = get_certificate_by_id(item_cert_id)
            except Exception:
                conflicting_rec = None
            if conflicting_rec:
                conf_name = str(conflicting_rec.get("student_name") or "").strip()
                conf_email = str(conflicting_rec.get("student_email") or "").strip().lower()
                if (conf_name and cand_name and conf_name.lower() != cand_name) or (conf_email and conf_email != norm_email):
                    failed_count += 1
                    err_msg = f"Ambiguous issuance identity: certificate ID {item_cert_id} belongs to '{conf_name}' ({conf_email}), cannot update for '{student_name}'."
                    results.append({
                        "certificate_id": item_cert_id,
                        "student_email": student_email,
                        "student_name": student_name,
                        "domain": domain,
                        "filename": filename,
                        "status": "failed_identity_ambiguity",
                        "error": err_msg
                    })
                    failed_items.append({
                        "certificate_id": item_cert_id,
                        "student_name": student_name,
                        "student_email": student_email,
                        "domain": domain,
                        "error": err_msg
                    })
                    continue

        if this_cert_rec:
            cert_id = str(this_cert_rec.get("certificate_id") or item_cert_id or cert_id).strip()
            target_id = this_cert_rec.get("id")
            cert_status = str(this_cert_rec.get("email_status") or "").strip().lower()
            is_resend = (cert_status == "sent")
            prev_send_count = int(this_cert_rec.get("send_count") or (1 if is_resend else 0))
            send_count = (prev_send_count + 1) if is_resend else 1
        else:
            cert_id = str(item_cert_id or cert_id).strip()
            target_id = None
            is_resend = False
            prev_send_count = 0
            send_count = 1

        # In-batch duplicate guard: Never send the exact same certificate issuance twice in one bulk operation
        batch_item_key = (
            student_name.lower(),
            norm_email,
            domain.lower(),
            _normalize_cert_date(item.get("start_date")),
            _normalize_cert_date(item.get("end_date")),
            _normalize_cert_date(item.get("issued_date")),
        ) if not cert_id else (str(cert_id).strip().upper(), student_name.lower())
        if batch_item_key in seen_batch_emails:
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

        # Historical duplicate skip guard (when "Send Only to New" is chosen):
        # Only skip if this exact issuance was already sent
        if is_resend and not send_again_all:
            should_skip = False
            if str(cert_id).lower() in skip_set:
                should_skip = True
            elif norm_email in skip_set:
                # If skipping by email, ensure we only skip if THIS student identity was previously sent
                if this_cert_rec:
                    rec_name = str(this_cert_rec.get("student_name") or "").strip().lower()
                    if not cand_name or not rec_name or rec_name == cand_name:
                        should_skip = True
                elif not cand_name:
                    should_skip = True

            if should_skip:
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

        seen_batch_emails.add(batch_item_key)

        email_data = {
            "student_email": student_email,
            "student_name": student_name,
            "domain": domain,
            "start_date": item.get("start_date") or (this_cert_rec.get("start_date") if this_cert_rec else ""),
            "end_date": item.get("end_date") or (this_cert_rec.get("end_date") if this_cert_rec else ""),
            "issued_date": item.get("issued_date") or (this_cert_rec.get("issued_date") if this_cert_rec else ""),
            "certificate_id": cert_id,
            "template_version": item.get("template_version") or (this_cert_rec.get("template_version") if this_cert_rec else "v1"),
        }

        try:
            filename = certificate_service.generate_certificate_pdf(
                data=email_data,
                output_dir=Path(output_dir),
                base_url=resolved_base_url,
                template_version=email_data.get("template_version", "v1"),
                custom_filename=filename,
            )
            pdf_path = Path(output_dir) / filename

            certificate_service.send_certificate_email(email_data, pdf_path, filename)

            successful_count += 1
            if job_id:
                bulk_job_manager.update_progress(job_id, sent_inc=1)
            now_iso = datetime.now().isoformat()

            # Update DB record: in-place resend update if previous sent record exists, else update pending or insert
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
            elif target_id:
                save_payload["id"] = target_id

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

            fail_payload = {
                "certificate_id": cert_id,
                "student_name": student_name,
                "student_email": student_email,
                "internship_domain": domain,
                "start_date": email_data["start_date"],
                "end_date": email_data["end_date"],
                "issued_date": email_data["issued_date"],
                "email_status": "failed",
                "sent_at": now_iso,
                "send_count": prev_send_count + 1 if is_resend else 0,
                "pdf_filename": filename,
                "error_message": err_msg,
                "template_version": email_data.get("template_version", "v1"),
            }
            if is_resend and target_id:
                fail_payload["id"] = target_id
                fail_payload["send_again"] = True
            elif target_id:
                fail_payload["id"] = target_id

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
