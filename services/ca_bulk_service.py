import csv
import io
import re
from datetime import datetime
from pathlib import Path


def normalize_header(header_val):
    """Normalize header string for fuzzy matching (lowercase, alphanumeric only)."""
    if header_val is None:
        return ""
    # Convert to string, lowercase, remove underscores, spaces, hyphens
    s = str(header_val).strip().lower()
    return re.sub(r"[\s_\-\.\:]+", "", s)


NAME_HEADER_PATTERNS = {
    "name",
    "candidatename",
    "studentname",
    "fullname",
    "candidatefullname",
    "studentfullname",
    "applicantname",
    "candidatenames",
    "studentnames",
    "names",
}

EMAIL_HEADER_PATTERNS = {
    "email",
    "emailaddress",
    "emailid",
    "candidateemail",
    "studentemail",
    "candidateemailaddress",
    "studentemailaddress",
    "applicantemail",
    "mail",
    "mailid",
    "emailaddresses",
    "candidateemailid",
    "studentemailid",
}


def find_column_index(headers, pattern_set):
    """Find the index of the first header that matches any pattern in pattern_set."""
    for idx, raw_h in enumerate(headers):
        norm = normalize_header(raw_h)
        if norm in pattern_set:
            return idx
    # Partial matching fallback: e.g. "candidate_name_2026" or "student_email_id"
    for idx, raw_h in enumerate(headers):
        norm = normalize_header(raw_h)
        if any(p in norm for p in ("email", "mail")) and pattern_set == EMAIL_HEADER_PATTERNS:
            return idx
        if any(p in norm for p in ("name", "student", "candidate")) and pattern_set == NAME_HEADER_PATTERNS:
            # ensure it's not email
            if not any(e in norm for e in ("email", "mail")):
                return idx
    return -1


def is_valid_email(email_str):
    """Simple robust validation for email structure."""
    if not email_str or not isinstance(email_str, str):
        return False
    s = email_str.strip()
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", s))


def parse_ca_file(file_bytes, filename):
    """
    Parse uploaded Excel (.xlsx, .xls) or CSV (.csv) file bytes.
    Extracts ONLY Candidate Name and Candidate Email columns.
    Preserves exact uploaded row order.
    Returns structured result dict.
    """
    ext = Path(filename).suffix.lower()
    if ext not in (".csv", ".xlsx", ".xls"):
        return {
            "success": False,
            "error": "Please upload a CSV or Excel file (.csv, .xlsx, .xls)."
        }

    raw_rows = []

    # 1. Parse based on extension
    if ext == ".csv":
        # Handle potential encoding issues (UTF-8, UTF-8-BOM, Latin-1)
        decoded_text = None
        for enc in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
            try:
                decoded_text = file_bytes.decode(enc)
                break
            except (UnicodeDecodeError, LookupError):
                continue
        if decoded_text is None:
            decoded_text = file_bytes.decode("utf-8", errors="replace")

        # Detect delimiter (comma, tab, semicolon)
        sample = decoded_text[:2048]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
            delimiter = dialect.delimiter
        except Exception:
            delimiter = ","

        reader = csv.reader(io.StringIO(decoded_text), delimiter=delimiter)
        for r in reader:
            raw_rows.append(r)

    elif ext == ".xlsx":
        import openpyxl
        try:
            wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
            sheet = wb.active
            for r in sheet.iter_rows(values_only=True):
                raw_rows.append([str(c).strip() if c is not None else "" for c in r])
        except Exception as exc:
            return {
                "success": False,
                "error": f"Failed to read Excel (.xlsx) file: {str(exc)}"
            }

    elif ext == ".xls":
        try:
            import xlrd
            book = xlrd.open_workbook(file_contents=file_bytes)
            sheet = book.sheet_by_index(0)
            for row_idx in range(sheet.nrows):
                row_vals = [str(sheet.cell_value(row_idx, col_idx)).strip() for col_idx in range(sheet.ncols)]
                raw_rows.append(row_vals)
        except ImportError:
            # Fallback to openpyxl in case it's actually an XML-based file with .xls extension
            try:
                import openpyxl
                wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
                sheet = wb.active
                for r in sheet.iter_rows(values_only=True):
                    raw_rows.append([str(c).strip() if c is not None else "" for c in r])
            except Exception:
                return {
                    "success": False,
                    "error": "Older Excel (.xls) parsing requires xlrd library or saving as .xlsx / .csv."
                }
        except Exception as exc:
            return {
                "success": False,
                "error": f"Failed to read Excel (.xls) file: {str(exc)}"
            }

    if not raw_rows:
        return {
            "success": False,
            "error": "The uploaded file is empty."
        }

    # 2. Identify Headers
    headers = raw_rows[0]
    name_col_idx = find_column_index(headers, NAME_HEADER_PATTERNS)
    email_col_idx = find_column_index(headers, EMAIL_HEADER_PATTERNS)

    name_found = name_col_idx >= 0
    email_found = email_col_idx >= 0

    if not name_found or not email_found:
        missing_parts = []
        if not name_found:
            missing_parts.append("Candidate Name: Not found")
        else:
            missing_parts.append("Candidate Name: Found")

        if not email_found:
            missing_parts.append("Email: Not found")
        else:
            missing_parts.append("Email: Found")

        status_detail = ", ".join(missing_parts)
        return {
            "success": False,
            "error": f"Could not find Candidate Name and Email columns ({status_detail}). Please upload a file containing recognizable Name and Email columns.",
            "name_found": name_found,
            "email_found": email_found,
            "headers_detected": [str(h) for h in headers if str(h).strip()]
        }

    # 3. Extract Rows (Ignore all other columns!)
    unique_candidates = []
    all_valid_candidates = []
    invalid_rows = []
    seen_emails = set()
    duplicate_count = 0

    data_rows = raw_rows[1:]
    total_data_rows = 0

    for idx, r in enumerate(data_rows, start=2):  # Row 2 in Excel is first data row
        # Skip completely blank rows
        if not any(c is not None and str(c).strip() for c in r):
            continue

        total_data_rows += 1

        raw_name = str(r[name_col_idx]).strip() if name_col_idx < len(r) and r[name_col_idx] is not None else ""
        raw_email = str(r[email_col_idx]).strip() if email_col_idx < len(r) and r[email_col_idx] is not None else ""

        # Clean email (remove trailing decimals from numeric conversions e.g. "123.0")
        if raw_email.endswith(".0") and "@" not in raw_email:
            raw_email = raw_email[:-2]

        is_name_ok = bool(raw_name)
        is_email_ok = is_valid_email(raw_email)

        if is_name_ok and is_email_ok:
            norm_email = raw_email.strip().lower()
            all_valid_candidates.append({
                "row_num": idx,
                "name": raw_name,
                "email": norm_email,
                "valid": True
            })
            if norm_email in seen_emails:
                duplicate_count += 1
            else:
                seen_emails.add(norm_email)
                unique_candidates.append({
                    "row_num": idx,
                    "name": raw_name,
                    "email": norm_email,
                    "valid": True
                })
        else:
            reason_parts = []
            if not is_name_ok:
                reason_parts.append("Candidate Name is missing")
            if not raw_email:
                reason_parts.append("Email is missing")
            elif not is_email_ok:
                reason_parts.append(f"Invalid email format: '{raw_email}'")

            invalid_rows.append({
                "row_num": idx,
                "name": raw_name or "—",
                "email": raw_email or "—",
                "reason": " & ".join(reason_parts)
            })

    if total_data_rows == 0:
        return {
            "success": False,
            "error": "The uploaded file has headers but contains no data rows."
        }

    names_text = "\n".join(c["name"] for c in all_valid_candidates)
    emails_text = "\n".join(c["email"] for c in all_valid_candidates)

    return {
        "success": True,
        "filename": filename,
        "total_rows": total_data_rows,
        "valid_count": len(unique_candidates),
        "total_valid_rows": len(all_valid_candidates),
        "unique_count": len(unique_candidates),
        "invalid_count": len(invalid_rows),
        "duplicate_count": duplicate_count,
        "candidates": unique_candidates,
        "invalid_rows": invalid_rows,
        "names_text": names_text,
        "emails_text": emails_text,
    }
