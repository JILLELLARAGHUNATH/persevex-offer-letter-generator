import os
import re
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from environment_config import load_application_environment

from database.config import (
    DATABASE_TYPE,
    SQLITE_DB_PATH,
    SUPABASE_URL,
    SUPABASE_KEY,
)

load_application_environment(Path(__file__).resolve().parent.parent)

# Global Supabase client singleton
_supabase_client = None


def get_supabase_client():
    global _supabase_client
    if _supabase_client is not None:
        return _supabase_client
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            from supabase import create_client
            _supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
            return _supabase_client
        except Exception as exc:
            print("SUPABASE CLIENT INIT WARNING (will use local fallback):", repr(exc))
    return None


PERSISTENT_CERT_DIR = Path(tempfile.gettempdir()) / "persevex_certificates"
try:
    PERSISTENT_CERT_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass


def init_database_tables():
    """Ensure all local SQLite fallback tables exist with proper schemas."""
    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        cursor = conn.cursor()

        # 1. Offer Letters table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS email_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_name TEXT NOT NULL,
                student_email TEXT NOT NULL,
                phone_number TEXT,
                college_name TEXT,
                internship_domain TEXT,
                internship_duration TEXT,
                start_date TEXT,
                end_date TEXT,
                offer_letter_type TEXT,
                email_status TEXT DEFAULT 'pending',
                sent_at TEXT,
                created_at TEXT,
                offer_letter_id TEXT,
                pdf_filename TEXT,
                send_count INTEGER DEFAULT 0,
                error_message TEXT
            )
        """)

        # 2. Certificate table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS certificate_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                certificate_id TEXT UNIQUE NOT NULL,
                student_name TEXT NOT NULL,
                student_email TEXT,
                internship_domain TEXT,
                start_date TEXT,
                end_date TEXT,
                issued_date TEXT,
                generated_date TEXT,
                created_at TEXT,
                certificate_status TEXT DEFAULT 'active',
                email_status TEXT DEFAULT 'pending',
                sent_at TEXT,
                send_count INTEGER DEFAULT 0,
                pdf_filename TEXT,
                error_message TEXT,
                template_version TEXT DEFAULT 'v1'
            )
        """)

        # Migration: ensure certificate_status and template_version exist if table was created previously
        cursor.execute("PRAGMA table_info(certificate_history)")
        columns = [row[1] for row in cursor.fetchall()]
        if "certificate_status" not in columns:
            cursor.execute("ALTER TABLE certificate_history ADD COLUMN certificate_status TEXT DEFAULT 'active'")
        if "template_version" not in columns:
            cursor.execute("ALTER TABLE certificate_history ADD COLUMN template_version TEXT DEFAULT 'v1'")

        # 3. Bulk Job history table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bulk_job_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT UNIQUE NOT NULL,
                job_type TEXT NOT NULL,
                total_records INTEGER DEFAULT 0,
                successful_count INTEGER DEFAULT 0,
                failed_count INTEGER DEFAULT 0,
                created_at TEXT,
                completed_at TEXT,
                status TEXT DEFAULT 'completed',
                details TEXT
            )
        """)

        # 4. Campus Ambassador table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS campus_ambassador_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_name TEXT NOT NULL,
                student_email TEXT NOT NULL,
                phone_number TEXT,
                college_name TEXT,
                internship_domain TEXT DEFAULT 'Campus Ambassador',
                internship_duration TEXT DEFAULT 'Tenure',
                start_date TEXT,
                end_date TEXT,
                offer_letter_type TEXT DEFAULT 'campus_ambassador',
                email_status TEXT DEFAULT 'pending',
                sent_at TEXT,
                created_at TEXT,
                offer_letter_id TEXT,
                pdf_filename TEXT,
                send_count INTEGER DEFAULT 0,
                error_message TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS campus_ambassador_certificate_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                participant_name TEXT NOT NULL,
                participant_email TEXT NOT NULL,
                program_date TEXT NOT NULL,
                document_id TEXT UNIQUE NOT NULL,
                pdf_filename TEXT,
                email_status TEXT DEFAULT 'pending',
                sent_at TEXT,
                send_count INTEGER DEFAULT 0,
                error_message TEXT,
                created_at TEXT,
                template_version TEXT DEFAULT 'v1',
                claim_token TEXT,
                claimed_at TEXT
            )
        """)
        cursor.execute("PRAGMA table_info(campus_ambassador_certificate_history)")
        ca_cert_columns = [row[1] for row in cursor.fetchall()]
        if "claim_token" not in ca_cert_columns:
            cursor.execute("ALTER TABLE campus_ambassador_certificate_history ADD COLUMN claim_token TEXT")
        if "claimed_at" not in ca_cert_columns:
            cursor.execute("ALTER TABLE campus_ambassador_certificate_history ADD COLUMN claimed_at TEXT")

        conn.commit()
        conn.close()
    except Exception as exc:
        print("LOCAL DB INIT ERROR:", repr(exc))


init_database_tables()


def get_ca_certificate_by_id(record_id):
    try:
        rec_id = int(record_id)
    except (TypeError, ValueError):
        return None
    sb = get_supabase_client()
    if sb:
        try:
            result = sb.table("campus_ambassador_certificate_history").select("*").eq("id", rec_id).limit(1).execute()
            return dict(result.data[0]) if result.data else None
        except Exception as exc:
            print("SUPABASE GET CA CERTIFICATE ERROR (using fallback):", repr(exc))
    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM campus_ambassador_certificate_history WHERE id = ?", (rec_id,)).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as exc:
        print("LOCAL DB GET CA CERTIFICATE ERROR:", repr(exc))
        return None


def get_previous_sent_ca_certificate(email):
    email = str(email or "").strip().lower()
    if not email:
        return None
    sb = get_supabase_client()
    if sb:
        try:
            result = (sb.table("campus_ambassador_certificate_history").select("*")
                      .ilike("participant_email", email).eq("email_status", "sent")
                      .order("created_at", desc=True).limit(1).execute())
            return dict(result.data[0]) if result.data else None
        except Exception as exc:
            print("SUPABASE FIND CA CERTIFICATE ERROR (using fallback):", repr(exc))
    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM campus_ambassador_certificate_history WHERE LOWER(TRIM(participant_email)) = ? AND email_status = 'sent' ORDER BY id DESC LIMIT 1",
            (email,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as exc:
        print("LOCAL DB FIND CA CERTIFICATE ERROR:", repr(exc))
        return None


def get_previous_sent_ca_certificates(emails):
    normalized = sorted({str(email or "").strip().lower() for email in emails if str(email or "").strip()})
    if not normalized:
        return {}
    sb = get_supabase_client()
    if sb:
        try:
            result = (sb.table("campus_ambassador_certificate_history").select("*")
                      .in_("participant_email", normalized).eq("email_status", "sent")
                      .order("created_at", desc=True).execute())
            records = {}
            for raw in result.data or []:
                email = str(raw.get("participant_email") or "").strip().lower()
                records.setdefault(email, dict(raw))
            return records
        except Exception as exc:
            print("SUPABASE FIND CA CERTIFICATE BATCH ERROR (using fallback):", repr(exc))
    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        conn.row_factory = sqlite3.Row
        placeholders = ", ".join("?" for _ in normalized)
        rows = conn.execute(
            f"SELECT * FROM campus_ambassador_certificate_history "
            f"WHERE LOWER(TRIM(participant_email)) IN ({placeholders}) AND email_status = 'sent' "
            "ORDER BY id DESC",
            tuple(normalized),
        ).fetchall()
        conn.close()
        records = {}
        for row in rows:
            record = dict(row)
            email = str(record.get("participant_email") or "").strip().lower()
            records.setdefault(email, record)
        return records
    except Exception as exc:
        print("LOCAL DB FIND CA CERTIFICATE BATCH ERROR:", repr(exc))
        return {}


def save_ca_certificate_record(data, existing_id=None):
    now = datetime.now(timezone.utc).isoformat()
    record = {
        "participant_name": str(data.get("participant_name") or "").strip(),
        "participant_email": str(data.get("participant_email") or "").strip().lower(),
        "program_date": str(data.get("program_date") or "").strip(),
        "document_id": str(data.get("document_id") or "").strip(),
        "pdf_filename": str(data.get("pdf_filename") or "").strip(),
        "email_status": str(data.get("email_status") or "pending").strip().lower(),
        "sent_at": data.get("sent_at"),
        "send_count": int(data.get("send_count") or 0),
        "error_message": data.get("error_message"),
        "created_at": data.get("created_at") or now,
        "template_version": str(data.get("template_version") or "v1"),
    }
    if not record["participant_name"] or not record["participant_email"] or not record["document_id"]:
        raise ValueError("CA Certificate record is missing required fields.")
    supabase_configured = bool(SUPABASE_URL and SUPABASE_KEY)
    supabase_saved = not supabase_configured
    supabase_error = None
    sb = get_supabase_client()
    if sb:
        try:
            if existing_id:
                result = sb.table("campus_ambassador_certificate_history").update(record).eq("id", int(existing_id)).execute()
            else:
                result = sb.table("campus_ambassador_certificate_history").upsert(
                    record, on_conflict="document_id"
                ).execute()
            if result.data is None or (existing_id and not result.data):
                raise RuntimeError("Supabase did not confirm the CA Certificate history write.")
            supabase_saved = True
        except Exception as exc:
            supabase_error = str(exc)
            print("SUPABASE SAVE CA CERTIFICATE ERROR (local reconciliation copy retained):", repr(exc))
    elif supabase_configured:
        supabase_error = "Supabase client could not be initialized."
    conn = sqlite3.connect(SQLITE_DB_PATH)
    if existing_id:
        columns = ", ".join(f"{key} = ?" for key in record)
        cursor = conn.execute(
            f"UPDATE campus_ambassador_certificate_history SET {columns} WHERE id = ?",
            tuple(record.values()) + (int(existing_id),),
        )
        if cursor.rowcount == 0:
            conn.execute(
                f"INSERT OR REPLACE INTO campus_ambassador_certificate_history ({', '.join(record)}) VALUES ({', '.join('?' for _ in record)})",
                tuple(record.values()),
            )
    else:
        columns = ", ".join(record)
        placeholders = ", ".join("?" for _ in record)
        update_columns = ", ".join(
            f"{key} = excluded.{key}" for key in record if key != "document_id"
        )
        conn.execute(
            f"INSERT INTO campus_ambassador_certificate_history ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(document_id) DO UPDATE SET {update_columns}",
            tuple(record.values()),
        )
    conn.commit()
    conn.close()
    if not supabase_configured:
        persistence_status = "local_only"
    elif supabase_saved:
        persistence_status = "supabase_and_local"
    else:
        persistence_status = "local_only"
    return {
        "saved": True,
        "supabase_saved": supabase_saved,
        "local_saved": True,
        "status": persistence_status,
        "error": supabase_error,
    }


def claim_ca_certificate_single_email(data, claim_token, allow_resend=False):
    sb = get_supabase_client()
    if not sb:
        raise RuntimeError("Supabase is required to safely claim single CA Certificate email delivery.")
    result = sb.rpc("claim_ca_certificate_single_email", {
        "p_participant_name": data["participant_name"],
        "p_participant_email": data["participant_email"],
        "p_program_date": data["program_date"],
        "p_document_id": data["document_id"],
        "p_pdf_filename": data["pdf_filename"],
        "p_claim_token": str(claim_token),
        "p_allow_resend": bool(allow_resend),
    }).execute()
    rows = result.data or []
    row = rows[0] if isinstance(rows, list) and rows else rows
    return dict(row) if row else {"claimed": False, "email_status": "unknown"}


def finish_ca_certificate_single_email(record_id, claim_token, data):
    sb = get_supabase_client()
    if not sb:
        raise RuntimeError("Supabase is required to finalize single CA Certificate email delivery.")
    now = datetime.now(timezone.utc).isoformat()
    values = {
        "participant_name": str(data["participant_name"]).strip(),
        "participant_email": str(data["participant_email"]).strip().lower(),
        "program_date": str(data["program_date"]).strip(),
        "document_id": str(data["document_id"]).strip(),
        "pdf_filename": str(data.get("pdf_filename") or "").strip(),
        "email_status": str(data["email_status"]).strip().lower(),
        "sent_at": data.get("sent_at"),
        "send_count": int(data.get("send_count") or 0),
        "error_message": data.get("error_message"),
        "template_version": "v1",
        "claim_token": None,
        "claimed_at": None,
    }
    result = (sb.table("campus_ambassador_certificate_history").update(values)
              .eq("id", int(record_id)).eq("claim_token", str(claim_token)).execute())
    if not result.data:
        return {
            "saved": False,
            "supabase_saved": False,
            "local_saved": False,
            "status": "claim_lost",
            "error": "Delivery claim expired or was replaced; reconcile its outcome before retrying.",
        }
    local_values = {
        **values,
        "created_at": now,
        "template_version": "v1",
    }
    conn = sqlite3.connect(SQLITE_DB_PATH)
    columns = ", ".join(local_values)
    placeholders = ", ".join("?" for _ in local_values)
    update_columns = ", ".join(
        f"{key} = excluded.{key}" for key in local_values if key != "document_id"
    )
    conn.execute(
        f"INSERT INTO campus_ambassador_certificate_history ({columns}) VALUES ({placeholders}) "
        f"ON CONFLICT(document_id) DO UPDATE SET {update_columns}",
        tuple(local_values.values()),
    )
    conn.commit()
    conn.close()
    return {
        "saved": True,
        "supabase_saved": True,
        "local_saved": True,
        "status": "supabase_and_local",
        "error": None,
    }


def delete_ca_certificate_record(record_id, document_id=None):
    identifiers = {str(value).strip() for value in (record_id, document_id) if str(value or "").strip()}
    if not identifiers:
        return False

    sb = get_supabase_client()
    if sb:
        deleted_count = 0
        for ident in identifiers:
            try:
                if ident.isdigit():
                    res = sb.table("campus_ambassador_certificate_history").delete().eq("id", int(ident)).execute()
                    if res and res.data:
                        deleted_count += len(res.data)
                else:
                    res = sb.table("campus_ambassador_certificate_history").delete().eq("document_id", ident).execute()
                    if res and res.data:
                        deleted_count += len(res.data)
            except Exception as exc:
                print("SUPABASE DELETE CA CERTIFICATE ERROR:", repr(exc))
                raise RuntimeError(f"Failed to delete CA certificate from Supabase: {exc}") from exc

        if deleted_count > 0:
            try:
                conn = sqlite3.connect(SQLITE_DB_PATH)
                for ident in identifiers:
                    if ident.isdigit():
                        conn.execute("DELETE FROM campus_ambassador_certificate_history WHERE id = ?", (int(ident),))
                    else:
                        conn.execute("DELETE FROM campus_ambassador_certificate_history WHERE document_id = ?", (ident,))
                conn.commit()
                conn.close()
            except Exception as exc:
                print("LOCAL DB DELETE CA CERTIFICATE SYNC WARNING:", repr(exc))
            return True

        return False

    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()
    total_local_deleted = 0
    for ident in identifiers:
        if ident.isdigit():
            cursor.execute("DELETE FROM campus_ambassador_certificate_history WHERE id = ?", (int(ident),))
            total_local_deleted += cursor.rowcount
        else:
            cursor.execute("DELETE FROM campus_ambassador_certificate_history WHERE document_id = ?", (ident,))
            total_local_deleted += cursor.rowcount
    conn.commit()
    conn.close()
    return total_local_deleted > 0


def create_ca_certificate_job(program_date, items, skipped_count=0):
    sb = get_supabase_client()
    if not sb:
        raise RuntimeError("Supabase is required for resumable CA Certificate jobs.")
    job = sb.table("campus_ambassador_certificate_jobs").insert({
        "program_date": program_date,
        "total_count": len(items) + int(skipped_count or 0),
        "skipped_count": int(skipped_count or 0),
        "status": "queued",
    }).execute()
    if not job.data:
        raise RuntimeError("Supabase did not create the CA Certificate job.")
    job_id = job.data[0]["id"]
    payload = [{
        "job_id": job_id,
        "source_row": int(item["source_row"]),
        "participant_name": item["participant_name"],
        "participant_email": item["participant_email"],
        "program_date": program_date,
    } for item in items]
    if payload:
        result = sb.table("campus_ambassador_certificate_job_items").insert(payload).execute()
        if not result.data:
            raise RuntimeError("Supabase did not create CA Certificate job items.")
    return job.data[0]


def get_ca_certificate_job(job_id):
    sb = get_supabase_client()
    if not sb:
        raise RuntimeError("Supabase is required for resumable CA Certificate jobs.")
    result = sb.table("campus_ambassador_certificate_jobs").select("*").eq("id", str(job_id)).limit(1).execute()
    return dict(result.data[0]) if result.data else None


def claim_ca_certificate_job_items(job_id, batch_size):
    sb = get_supabase_client()
    if not sb:
        raise RuntimeError("Supabase is required for resumable CA Certificate jobs.")
    token = str(uuid.uuid4())
    result = sb.rpc("claim_ca_certificate_job_items", {
        "target_job": str(job_id),
        "batch_size": min(max(int(batch_size), 1), 25),
        "token": token,
    }).execute()
    return [dict(item) for item in (result.data or [])]


def claim_ca_certificate_email_items(job_id, batch_size, retry_failed=False):
    sb = get_supabase_client()
    if not sb:
        raise RuntimeError("Supabase is required for resumable CA Certificate jobs.")
    token = str(uuid.uuid4())
    result = sb.rpc("claim_ca_certificate_email_items", {
        "target_job": str(job_id),
        "batch_size": 1,
        "token": token,
        "retry_failed": bool(retry_failed),
    }).execute()
    return [dict(item) for item in (result.data or [])]


def update_ca_certificate_job_item(item_id, values, claim_token=None):
    sb = get_supabase_client()
    if not sb:
        raise RuntimeError("Supabase is required for resumable CA Certificate jobs.")
    query = sb.table("campus_ambassador_certificate_job_items").update(values).eq("id", str(item_id))
    if claim_token:
        query = query.eq("claim_token", str(claim_token))
    result = query.execute()
    return bool(result.data)


def update_ca_certificate_job(job_id, values):
    sb = get_supabase_client()
    if not sb:
        raise RuntimeError("Supabase is required for resumable CA Certificate jobs.")
    result = sb.table("campus_ambassador_certificate_jobs").update(values).eq("id", str(job_id)).execute()
    return bool(result.data)


def get_ca_certificate_job_items(job_id, statuses=None):
    sb = get_supabase_client()
    if not sb:
        raise RuntimeError("Supabase is required for resumable CA Certificate jobs.")
    query = sb.table("campus_ambassador_certificate_job_items").select("*").eq("job_id", str(job_id))
    if statuses:
        query = query.in_("email_status", list(statuses))
    result = query.order("source_row").execute()
    return [dict(item) for item in (result.data or [])]


def reconcile_ca_certificate_job(job_id):
    items = get_ca_certificate_job_items(job_id)
    job = get_ca_certificate_job(job_id) or {}
    generation_terminal = {"generated", "failed", "skipped"}
    item_skipped = sum(
        1 for item in items
        if item.get("generation_status") == "skipped"
        or item.get("email_status") == "skipped"
    )
    skipped = max(int(job.get("skipped_count") or 0), item_skipped)
    processed = sum(1 for item in items if item.get("generation_status") in generation_terminal) + skipped
    generated = sum(1 for item in items if item.get("generation_status") == "generated")
    sent = sum(1 for item in items if item.get("email_status") == "sent")
    failed = sum(
        1 for item in items
        if item.get("generation_status") == "failed"
        or item.get("email_status") in {"failed", "uncertain"}
    )
    active = any(
        item.get("generation_status") in {"pending", "processing"}
        or item.get("email_status") in {"pending", "sending"}
        for item in items
    )
    status = "running" if active else ("completed_with_errors" if failed else "completed")
    values = {
        "status": status,
        "processed_count": processed,
        "generated_count": generated,
        "sent_count": sent,
        "failed_count": failed,
        "skipped_count": skipped,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if not active:
        values["completed_at"] = datetime.now(timezone.utc).isoformat()
    update_ca_certificate_job(job_id, values)
    return values


# ============================================================
# CAMPUS AMBASSADOR STORAGE
# ============================================================
def get_campus_ambassador_by_id(record_id):
    """
    Retrieve a Campus Ambassador record by primary key ID.
    Authoritative source: Supabase (when configured and operational).
    Fallback source: Local SQLite (used ONLY if Supabase is unconfigured or query throws exception).
    """
    if record_id is None or not str(record_id).strip():
        return None
    try:
        rec_id_int = int(record_id)
    except (ValueError, TypeError):
        return None

    sb = get_supabase_client()
    if sb:
        try:
            res = sb.table("campus_ambassador_history").select("*").eq("id", rec_id_int).limit(1).execute()
            if res.data and len(res.data) > 0:
                return dict(res.data[0])
            return None
        except Exception as exc:
            print("SUPABASE GET CA BY ID ERROR (using fallback):", repr(exc))

    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM campus_ambassador_history WHERE id = ? LIMIT 1", (rec_id_int,))
        row = cursor.fetchone()
        conn.close()
        if row:
            return dict(row)
    except Exception as exc:
        print("LOCAL DB GET CA BY ID ERROR:", repr(exc))

    return None


def save_campus_ambassador_record(record_data, existing_id=None):
    """
    Save or update Campus Ambassador record in Supabase & local SQLite.
    When send_again=True and existing_id/id is provided (resend), it UPDATES the existing record:
      - keeps the same primary key ID
      - increments send_count
      - updates sent_at
      - sets email_status = 'sent'
      - clears error_message
      - preserves existing metadata unless explicitly overridden.
    When first-time send (send_again=False), it INSERTS a new record with send_count=1 (or specified count).
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    send_again = bool(record_data.get("send_again"))
    target_id_raw = existing_id if existing_id is not None else (record_data.get("id") or record_data.get("record_id") or record_data.get("existing_id"))

    target_id = None
    if target_id_raw is not None and str(target_id_raw).strip():
        try:
            target_id = int(str(target_id_raw).strip())
        except (ValueError, TypeError):
            target_id = None

    is_resend = bool(send_again and target_id is not None)

    existing = None
    if is_resend:
        try:
            existing = get_campus_ambassador_by_id(target_id)
        except Exception:
            existing = None
    existing = existing or {}

    # Merge fields
    merged_student_name = str(record_data.get("student_name") or existing.get("student_name") or "").strip()
    merged_student_email = str(record_data.get("student_email") or existing.get("student_email") or "").strip().lower()
    merged_phone_number = record_data.get("phone_number") if record_data.get("phone_number") is not None else existing.get("phone_number")
    merged_college_name = record_data.get("college_name") if record_data.get("college_name") is not None else existing.get("college_name")
    merged_internship_domain = str(record_data.get("internship_domain") or existing.get("internship_domain") or "Campus Ambassador").strip()
    merged_internship_duration = str(record_data.get("internship_duration") or existing.get("internship_duration") or "Tenure").strip()
    merged_start_date = record_data.get("start_date") if record_data.get("start_date") is not None else existing.get("start_date")
    merged_end_date = record_data.get("end_date") if record_data.get("end_date") is not None else existing.get("end_date")
    merged_offer_letter_type = str(record_data.get("offer_letter_type") or existing.get("offer_letter_type") or "campus_ambassador").strip()
    merged_email_status = str(record_data.get("email_status") or ("sent" if is_resend else "pending")).strip().lower()
    merged_sent_at = record_data.get("sent_at") or (now_iso if merged_email_status == "sent" else existing.get("sent_at"))
    merged_created_at = existing.get("created_at") or record_data.get("created_at") or now_iso
    merged_offer_letter_id = record_data.get("offer_letter_id") or existing.get("offer_letter_id")
    merged_pdf_filename = record_data.get("pdf_filename") or existing.get("pdf_filename")

    if is_resend:
        if record_data.get("send_count") is not None:
            merged_send_count = int(record_data["send_count"])
        elif existing.get("send_count") is not None:
            merged_send_count = int(existing["send_count"]) + 1
        else:
            merged_send_count = 2
        merged_error_message = record_data.get("error_message") if merged_email_status == "failed" else None
    else:
        if record_data.get("send_count") is not None:
            merged_send_count = int(record_data["send_count"])
        else:
            merged_send_count = 1 if merged_email_status == "sent" else 0
        merged_error_message = record_data.get("error_message")

    sb = get_supabase_client()
    if sb:
        if is_resend and target_id is not None:
            update_payload = {
                "student_name": merged_student_name,
                "student_email": merged_student_email,
                "phone_number": merged_phone_number,
                "college_name": merged_college_name,
                "internship_domain": merged_internship_domain,
                "internship_duration": merged_internship_duration,
                "start_date": merged_start_date,
                "end_date": merged_end_date,
                "offer_letter_type": merged_offer_letter_type,
                "email_status": merged_email_status,
                "sent_at": merged_sent_at,
                "offer_letter_id": merged_offer_letter_id,
                "pdf_filename": merged_pdf_filename,
                "send_count": merged_send_count,
                "error_message": merged_error_message,
            }
            current_payload = dict(update_payload)
            for _ in range(5):
                try:
                    sb.table("campus_ambassador_history").update(current_payload).eq("id", target_id).execute()
                    break
                except Exception as mut_exc:
                    err_str = str(mut_exc)
                    match = re.search(r"Could not find the '([^']+)' column", err_str)
                    if match:
                        col_to_drop = match.group(1)
                        if col_to_drop in current_payload:
                            current_payload.pop(col_to_drop, None)
                            continue
                    print("SUPABASE UPDATE CA RECORD ERROR (using local DB):", repr(mut_exc))
                    break
        else:
            insert_payload = {
                "student_name": merged_student_name,
                "student_email": merged_student_email,
                "phone_number": merged_phone_number,
                "college_name": merged_college_name,
                "internship_domain": merged_internship_domain,
                "internship_duration": merged_internship_duration,
                "start_date": merged_start_date,
                "end_date": merged_end_date,
                "offer_letter_type": merged_offer_letter_type,
                "email_status": merged_email_status,
                "sent_at": merged_sent_at,
                "created_at": merged_created_at,
                "offer_letter_id": merged_offer_letter_id,
                "pdf_filename": merged_pdf_filename,
                "send_count": merged_send_count,
                "error_message": merged_error_message,
            }
            current_payload = dict(insert_payload)
            for _ in range(5):
                try:
                    sb.table("campus_ambassador_history").insert(current_payload).execute()
                    break
                except Exception as mut_exc:
                    err_str = str(mut_exc)
                    match = re.search(r"Could not find the '([^']+)' column", err_str)
                    if match:
                        col_to_drop = match.group(1)
                        if col_to_drop in current_payload:
                            current_payload.pop(col_to_drop, None)
                            continue
                    print("SUPABASE SAVE CA RECORD ERROR (using local DB):", repr(mut_exc))
                    break

    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        cursor = conn.cursor()
        if is_resend and target_id is not None:
            cursor.execute("""
                UPDATE campus_ambassador_history SET
                    student_name = ?,
                    student_email = ?,
                    phone_number = ?,
                    college_name = ?,
                    internship_domain = ?,
                    internship_duration = ?,
                    start_date = ?,
                    end_date = ?,
                    offer_letter_type = ?,
                    email_status = ?,
                    sent_at = ?,
                    offer_letter_id = ?,
                    pdf_filename = ?,
                    send_count = ?,
                    error_message = ?
                WHERE id = ?
            """, (
                merged_student_name,
                merged_student_email,
                merged_phone_number,
                merged_college_name,
                merged_internship_domain,
                merged_internship_duration,
                merged_start_date,
                merged_end_date,
                merged_offer_letter_type,
                merged_email_status,
                merged_sent_at,
                merged_offer_letter_id,
                merged_pdf_filename,
                merged_send_count,
                merged_error_message,
                target_id
            ))
            if cursor.rowcount == 0:
                cursor.execute("""
                    INSERT OR REPLACE INTO campus_ambassador_history (
                        id, student_name, student_email, phone_number, college_name,
                        internship_domain, internship_duration, start_date, end_date,
                        offer_letter_type, email_status, sent_at, created_at,
                        offer_letter_id, pdf_filename, send_count, error_message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    target_id,
                    merged_student_name,
                    merged_student_email,
                    merged_phone_number,
                    merged_college_name,
                    merged_internship_domain,
                    merged_internship_duration,
                    merged_start_date,
                    merged_end_date,
                    merged_offer_letter_type,
                    merged_email_status,
                    merged_sent_at,
                    merged_created_at,
                    merged_offer_letter_id,
                    merged_pdf_filename,
                    merged_send_count,
                    merged_error_message
                ))
        else:
            cursor.execute("""
                INSERT INTO campus_ambassador_history (
                    student_name, student_email, phone_number, college_name,
                    internship_domain, internship_duration, start_date, end_date,
                    offer_letter_type, email_status, sent_at, created_at,
                    offer_letter_id, pdf_filename, send_count, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                merged_student_name,
                merged_student_email,
                merged_phone_number,
                merged_college_name,
                merged_internship_domain,
                merged_internship_duration,
                merged_start_date,
                merged_end_date,
                merged_offer_letter_type,
                merged_email_status,
                merged_sent_at,
                merged_created_at,
                merged_offer_letter_id,
                merged_pdf_filename,
                merged_send_count,
                merged_error_message
            ))
        conn.commit()
        conn.close()
    except Exception as exc:
        print("LOCAL DB SAVE CA RECORD ERROR:", repr(exc))

    return True


def delete_campus_ambassador_record(record_id, document_id=None):
    """
    Delete a Campus Ambassador record from campus_ambassador_history table.
    Targets ONLY exact numeric ID or exact offer_letter_id.
    Never deletes from email_history.
    """
    identifiers = set()
    if record_id is not None and str(record_id).strip():
        identifiers.add(str(record_id).strip())
    if document_id is not None and str(document_id).strip():
        identifiers.add(str(document_id).strip())

    if not identifiers:
        return False

    sb = get_supabase_client()
    if sb:
        deleted_count = 0
        for ident in identifiers:
            try:
                if ident.isdigit():
                    res = sb.table("campus_ambassador_history").delete().eq("id", int(ident)).execute()
                    if res and res.data:
                        deleted_count += len(res.data)
                else:
                    res = sb.table("campus_ambassador_history").delete().eq("offer_letter_id", ident).execute()
                    if res and res.data:
                        deleted_count += len(res.data)
            except Exception as exc:
                print("SUPABASE DELETE CA ID ERROR:", repr(exc))
                raise RuntimeError(f"Failed to delete CA offer letter from Supabase: {exc}") from exc

        if deleted_count > 0:
            try:
                conn = sqlite3.connect(SQLITE_DB_PATH)
                cursor = conn.cursor()
                for ident in identifiers:
                    if ident.isdigit():
                        cursor.execute("DELETE FROM campus_ambassador_history WHERE id = ?", (int(ident),))
                    else:
                        cursor.execute("DELETE FROM campus_ambassador_history WHERE offer_letter_id = ?", (ident,))
                conn.commit()
                conn.close()
            except Exception as exc:
                print("LOCAL DB DELETE CA SYNC WARNING:", repr(exc))
            return True

        return False

    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()
    total_local_deleted = 0
    for ident in identifiers:
        if ident.isdigit():
            cursor.execute("DELETE FROM campus_ambassador_history WHERE id = ?", (int(ident),))
            total_local_deleted += cursor.rowcount
        else:
            cursor.execute("DELETE FROM campus_ambassador_history WHERE offer_letter_id = ?", (ident,))
            total_local_deleted += cursor.rowcount
    conn.commit()
    conn.close()
    return total_local_deleted > 0


def get_previous_sent_campus_ambassador_by_email(email):
    """
    Check if a successfully sent Campus Ambassador record already exists for this email.
    Queries ONLY public.campus_ambassador_history.
    Considers ONLY records where email_status = 'sent' (ignores 'pending', 'failed', etc.).
    Trims whitespace and performs case-insensitive comparison.
    Authoritative source: Supabase when configured and operational.
    Local SQLite is used ONLY if Supabase is not configured or throws an actual connection/query exception.
    """
    if not email:
        return None
    email_clean = str(email).strip().lower()

    sb = get_supabase_client()
    if sb:
        try:
            res = (
                sb.table("campus_ambassador_history")
                .select("*")
                .ilike("student_email", email_clean)
                .eq("email_status", "sent")
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            # When Supabase query succeeds, its result is authoritative
            if res.data and len(res.data) > 0:
                return res.data[0]
            return None
        except Exception as exc:
            print("SUPABASE GET PREVIOUS SENT CA ERROR (using fallback):", repr(exc))

    # Fallback to local SQLite ONLY if Supabase is unconfigured or threw an exception
    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM campus_ambassador_history WHERE LOWER(TRIM(student_email)) = ? AND email_status = 'sent' ORDER BY id DESC LIMIT 1",
            (email_clean,)
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            return dict(row)
    except Exception as exc:
        print("LOCAL DB GET PREVIOUS SENT CA ERROR:", repr(exc))

    return None


def find_existing_campus_ambassador_batch(candidate_rows_or_emails, supabase_client=None):
    """
    Efficient batch lookup to detect successfully sent Campus Ambassador records in public.campus_ambassador_history.
    Considers ONLY records where email_status = 'sent' (ignores 'pending', 'failed', etc.).
    Normalizes candidate emails and database emails using email.strip().lower().
    Authoritative source of truth: Supabase (when configured and operational).
    Fallback source: Local SQLite (used ONLY if Supabase is unconfigured or throws an exception).
    Returns: dict mapping lowercase trimmed email -> existing CA record dict
    """
    if not candidate_rows_or_emails:
        return {}

    # Extract clean unique candidate emails (normalized: trimmed & lowercase) + query variants
    clean_emails_set = set()
    query_variants_set = set()

    for item in candidate_rows_or_emails:
        if isinstance(item, dict):
            em = item.get("student_email") or item.get("email") or ""
        else:
            em = str(item or "")
        em_str = str(em).strip()
        em_norm = em_str.lower()
        if em_norm:
            clean_emails_set.add(em_norm)
            query_variants_set.add(em_norm)
            if em_str:
                query_variants_set.add(em_str)

    if not clean_emails_set:
        return {}

    query_variants_list = list(query_variants_set)
    clean_emails_list = list(clean_emails_set)
    existing_records = []
    supabase_success = False

    # 1. Authoritative batch query to Supabase
    sb = supabase_client or get_supabase_client()
    if sb:
        try:
            res = (
                sb.table("campus_ambassador_history")
                .select("*")
                .in_("student_email", query_variants_list)
                .eq("email_status", "sent")
                .execute()
            )
            if res.data is not None:
                supabase_success = True
                for row in res.data:
                    rec = dict(row)
                    st = str(rec.get("email_status") or "").strip().lower()
                    if st == "sent":
                        existing_records.append(rec)
        except Exception as exc:
            print("SUPABASE FIND EXISTING CA BATCH WARNING (falling back to local DB):", repr(exc))
            supabase_success = False

    # 2. Local SQLite fallback query — ONLY used if Supabase is unconfigured or query failed
    if not supabase_success:
        try:
            conn = sqlite3.connect(SQLITE_DB_PATH)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            placeholders = ",".join("?" for _ in clean_emails_list)
            cursor.execute(
                f"SELECT * FROM campus_ambassador_history WHERE LOWER(TRIM(student_email)) IN ({placeholders}) AND email_status = 'sent'",
                clean_emails_list
            )
            for row in cursor.fetchall():
                existing_records.append(dict(row))
            conn.close()
        except Exception as exc:
            print("LOCAL DB FIND EXISTING CA BATCH ERROR:", repr(exc))

    # 3. Build lookup map with normalized email keys (trimmed & lowercased)
    lookup = {}
    for r in existing_records:
        row_email = str(r.get("student_email") or "").strip().lower()
        if row_email and row_email in clean_emails_set:
            if row_email not in lookup or int(r.get("send_count") or 0) > int(lookup[row_email].get("send_count") or 0):
                lookup[row_email] = r

    return lookup


# ============================================================
# OFFER LETTER STORAGE
# ============================================================
def save_offer_letter_record(record_data):
    """
    Save or update Offer Letter record in Supabase and local SQLite.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    if not record_data.get("created_at"):
        record_data["created_at"] = now_iso

    sb = get_supabase_client()
    if sb:
        try:
            sb.table("email_history").insert(record_data).execute()
        except Exception as exc:
            print("SUPABASE SAVE OFFER LETTER ERROR (using local DB):", repr(exc))

    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO email_history (
                student_name, student_email, phone_number, college_name,
                internship_domain, internship_duration, start_date, end_date,
                offer_letter_type, email_status, sent_at, created_at,
                offer_letter_id, pdf_filename, send_count, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            record_data.get("student_name"),
            record_data.get("student_email"),
            record_data.get("phone_number"),
            record_data.get("college_name"),
            record_data.get("internship_domain"),
            record_data.get("internship_duration"),
            record_data.get("start_date"),
            record_data.get("end_date"),
            record_data.get("offer_letter_type"),
            record_data.get("email_status", "pending"),
            record_data.get("sent_at"),
            record_data.get("created_at"),
            record_data.get("offer_letter_id"),
            record_data.get("pdf_filename"),
            record_data.get("send_count", 0),
            record_data.get("error_message")
        ))
        conn.commit()
        conn.close()
    except Exception as exc:
        print("LOCAL DB SAVE OFFER LETTER ERROR:", repr(exc))

    return True


def get_previous_offer_letter_by_email(email):
    """
    Check if an offer letter already exists for this email.
    Authoritative source: Supabase when configured and operational.
    """
    if not email:
        return None
    email_clean = email.strip().lower()

    sb = get_supabase_client()
    if sb:
        try:
            res = (
                sb.table("email_history")
                .select("*")
                .eq("student_email", email_clean)
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            if res.data and len(res.data) > 0:
                return res.data[0]
            return None
        except Exception as exc:
            print("SUPABASE GET OFFER LETTER BY EMAIL ERROR (using fallback):", repr(exc))

    # Fallback to local SQLite ONLY if Supabase is unconfigured or threw an exception
    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM email_history WHERE LOWER(student_email) = ? ORDER BY id DESC LIMIT 1",
            (email_clean,)
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            return dict(row)
    except Exception as exc:
        print("LOCAL DB GET OFFER LETTER BY EMAIL ERROR:", repr(exc))

    return None


def find_existing_offer_letters_batch(candidate_rows_or_emails, supabase_client=None):
    """
    Efficient batch lookup to detect successfully sent Offer Letter records in public.email_history.
    Considers ONLY records where email_status = 'sent' (ignores 'pending', 'failed', etc.).
    Normalizes candidate emails and database emails using email.strip().lower().
    Authoritative source of truth: Supabase (when configured and operational).
    Fallback source: Local SQLite (used ONLY if Supabase is unconfigured or throws an exception).
    Returns: dict mapping lowercase trimmed email -> existing Offer Letter record dict
    """
    if not candidate_rows_or_emails:
        return {}

    clean_emails_set = set()
    query_variants_set = set()

    for item in candidate_rows_or_emails:
        if isinstance(item, dict):
            em = item.get("student_email") or item.get("email") or ""
        else:
            em = str(item or "")
        em_str = str(em).strip()
        em_norm = em_str.lower()
        if em_norm:
            clean_emails_set.add(em_norm)
            query_variants_set.add(em_norm)
            if em_str:
                query_variants_set.add(em_str)

    if not clean_emails_set:
        return {}

    query_variants_list = list(query_variants_set)
    clean_emails_list = list(clean_emails_set)
    existing_records = []
    supabase_success = False

    # 1. Authoritative batch query to Supabase
    sb = supabase_client or get_supabase_client()
    if sb:
        try:
            res = (
                sb.table("email_history")
                .select("*")
                .in_("student_email", query_variants_list)
                .eq("email_status", "sent")
                .execute()
            )
            if res.data is not None:
                supabase_success = True
                for row in res.data:
                    rec = dict(row)
                    st = str(rec.get("email_status") or "").strip().lower()
                    if st == "sent":
                        existing_records.append(rec)
        except Exception as exc:
            print("SUPABASE FIND EXISTING OFFER LETTERS BATCH WARNING (falling back to local DB):", repr(exc))
            supabase_success = False

    # 2. Local SQLite fallback query — ONLY used if Supabase is unconfigured or query failed
    if not supabase_success:
        try:
            conn = sqlite3.connect(SQLITE_DB_PATH)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            placeholders = ",".join("?" for _ in clean_emails_list)
            cursor.execute(
                f"SELECT * FROM email_history WHERE LOWER(TRIM(student_email)) IN ({placeholders}) AND email_status = 'sent'",
                clean_emails_list
            )
            for row in cursor.fetchall():
                existing_records.append(dict(row))
            conn.close()
        except Exception as exc:
            print("LOCAL DB FIND EXISTING OFFER LETTERS BATCH ERROR:", repr(exc))

    # 3. Build lookup map with normalized email keys (trimmed & lowercased)
    lookup = {}
    for r in existing_records:
        row_email = str(r.get("student_email") or "").strip().lower()
        if row_email and row_email in clean_emails_set:
            if row_email not in lookup or int(r.get("send_count") or 0) > int(lookup[row_email].get("send_count") or 0):
                lookup[row_email] = r

    return lookup


def delete_offer_letter_record(record_id, document_id=None):
    """
    Delete an offer letter record from history.
    Targets ONLY exact numeric ID or exact offer_letter_id (e.g. OL-123 or document filename stem).
    Never deletes by email address to prevent accidental cascades.
    """
    identifiers = set()
    if record_id is not None and str(record_id).strip():
        identifiers.add(str(record_id).strip())
    if document_id is not None and str(document_id).strip():
        identifiers.add(str(document_id).strip())

    if not identifiers:
        return False

    sb = get_supabase_client()
    if sb:
        deleted_count = 0
        for ident in identifiers:
            try:
                if ident.isdigit():
                    res = sb.table("email_history").delete().eq("id", int(ident)).execute()
                    if res and res.data:
                        deleted_count += len(res.data)
                else:
                    res = sb.table("email_history").delete().eq("offer_letter_id", ident).execute()
                    if res and res.data:
                        deleted_count += len(res.data)
            except Exception as exc:
                print("SUPABASE DELETE ID ERROR:", repr(exc))
                raise RuntimeError(f"Failed to delete offer letter from Supabase: {exc}") from exc

        if deleted_count > 0:
            try:
                conn = sqlite3.connect(SQLITE_DB_PATH)
                cursor = conn.cursor()
                for ident in identifiers:
                    if ident.isdigit():
                        cursor.execute("DELETE FROM email_history WHERE id = ?", (int(ident),))
                    else:
                        cursor.execute("DELETE FROM email_history WHERE offer_letter_id = ?", (ident,))
                conn.commit()
                conn.close()
            except Exception as exc:
                print("LOCAL DB DELETE OFFER LETTER SYNC WARNING:", repr(exc))
            return True

        return False

    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()
    total_local_deleted = 0
    for ident in identifiers:
        if ident.isdigit():
            cursor.execute("DELETE FROM email_history WHERE id = ?", (int(ident),))
            total_local_deleted += cursor.rowcount
        else:
            cursor.execute("DELETE FROM email_history WHERE offer_letter_id = ?", (ident,))
            total_local_deleted += cursor.rowcount
    conn.commit()
    conn.close()
    return total_local_deleted > 0


# ============================================================
# CERTIFICATE STORAGE
# ============================================================
def get_certificate_by_pk_id(record_id):
    """
    Retrieve a Certificate record by primary key ID (integer).
    Authoritative source: Supabase (when configured and operational).
    Fallback source: Local SQLite (used ONLY if Supabase is unconfigured or query throws exception).
    """
    if record_id is None or not str(record_id).strip():
        return None
    try:
        rec_id_int = int(record_id)
    except (ValueError, TypeError):
        return None

    sb = get_supabase_client()
    if sb:
        try:
            res = sb.table("certificate_history").select("*").eq("id", rec_id_int).limit(1).execute()
            if res.data and len(res.data) > 0:
                rec = dict(res.data[0])
                if not rec.get("certificate_status"):
                    rec["certificate_status"] = "active"
                if not rec.get("template_version"):
                    rec["template_version"] = "v1"
                return rec
            return None
        except Exception as exc:
            print("SUPABASE GET CERT BY PK ID ERROR (using fallback):", repr(exc))

    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM certificate_history WHERE id = ? LIMIT 1", (rec_id_int,))
        row = cursor.fetchone()
        conn.close()
        if row:
            rec = dict(row)
            if not rec.get("certificate_status"):
                rec["certificate_status"] = "active"
            if not rec.get("template_version"):
                rec["template_version"] = "v1"
            return rec
    except Exception as exc:
        print("LOCAL DB GET CERT BY PK ID ERROR:", repr(exc))

    return None


def save_certificate_record(record_data, existing_id=None):
    """
    Save or update Certificate record in Supabase and local SQLite.
    When send_again=True and existing_id/id is provided (resend), it UPDATES the existing record in-place:
      - keeps the same primary key ID
      - replaces old certificate details (certificate_id, domain, dates, name, pdf_filename) with CURRENT certificate details
      - increments send_count
      - updates sent_at
      - sets email_status = 'sent'
      - clears error_message
      - preserves created_at from original record
    When first-time send (send_again=False), it INSERTS a new record (or updates by certificate_id if already present).
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    send_again = bool(record_data.get("send_again"))
    target_id_raw = existing_id if existing_id is not None else (record_data.get("id") or record_data.get("record_id") or record_data.get("existing_id"))

    target_id = None
    if target_id_raw is not None and str(target_id_raw).strip():
        try:
            target_id = int(str(target_id_raw).strip())
        except (ValueError, TypeError):
            target_id = None

    is_resend = bool(send_again and target_id is not None)

    existing = None
    if is_resend:
        try:
            existing = get_certificate_by_pk_id(target_id)
        except Exception:
            existing = None
    elif record_data.get("certificate_id"):
        try:
            existing = get_certificate_by_id(str(record_data.get("certificate_id")).strip())
        except Exception:
            existing = None

    existing = existing or {}

    # Fields to persist (prioritize current record_data over existing)
    cert_id = str(record_data.get("certificate_id") or existing.get("certificate_id") or "").strip()
    if not cert_id and not target_id:
        return False

    merged_student_name = str(record_data.get("student_name") or existing.get("student_name") or "").strip()
    merged_student_email = str(record_data.get("student_email") or existing.get("student_email") or "").strip().lower()
    merged_domain = str(record_data.get("internship_domain") or record_data.get("domain") or existing.get("internship_domain") or "").strip()
    merged_start_date = str(record_data.get("start_date") or existing.get("start_date") or "").strip()
    merged_end_date = str(record_data.get("end_date") or existing.get("end_date") or "").strip()
    merged_issued_date = str(record_data.get("issued_date") or existing.get("issued_date") or "").strip()
    merged_generated_date = str(record_data.get("generated_date") or existing.get("generated_date") or now_iso).strip()
    merged_created_at = str(existing.get("created_at") or record_data.get("created_at") or now_iso).strip()
    merged_certificate_status = str(record_data.get("certificate_status") or existing.get("certificate_status") or "active").strip().lower()
    merged_email_status = str(record_data.get("email_status") or ("sent" if is_resend else "pending")).strip().lower()
    merged_sent_at = record_data.get("sent_at") or (now_iso if merged_email_status == "sent" else existing.get("sent_at"))

    if is_resend:
        if record_data.get("send_count") is not None:
            merged_send_count = int(record_data["send_count"])
        elif existing.get("send_count") is not None:
            merged_send_count = int(existing["send_count"]) + 1
        else:
            merged_send_count = 2
        merged_error_message = record_data.get("error_message") if merged_email_status == "failed" else None
    else:
        if record_data.get("send_count") is not None:
            merged_send_count = int(record_data["send_count"])
        else:
            merged_send_count = 1 if merged_email_status == "sent" else 0
        merged_error_message = record_data.get("error_message")

    merged_pdf_filename = str(record_data.get("pdf_filename") or existing.get("pdf_filename") or "").strip()
    merged_template_version = str(record_data.get("template_version") or existing.get("template_version") or "v1").strip()

    payload_full = {
        "certificate_id": cert_id,
        "student_name": merged_student_name,
        "student_email": merged_student_email,
        "internship_domain": merged_domain,
        "start_date": merged_start_date,
        "end_date": merged_end_date,
        "issued_date": merged_issued_date,
        "generated_date": merged_generated_date,
        "created_at": merged_created_at,
        "certificate_status": merged_certificate_status,
        "email_status": merged_email_status,
        "sent_at": merged_sent_at,
        "send_count": merged_send_count,
        "pdf_filename": merged_pdf_filename,
        "error_message": merged_error_message,
        "template_version": merged_template_version,
    }

    # Supabase Mutate
    sb = get_supabase_client()
    if sb:
        def _safe_supabase_mutate(action, payload, pk_id=None, c_id=None):
            current_payload = dict(payload)
            for _ in range(5):
                try:
                    if action == "update_pk":
                        return sb.table("certificate_history").update(current_payload).eq("id", pk_id).execute()
                    elif action == "update_cert_id":
                        return sb.table("certificate_history").update(current_payload).eq("certificate_id", c_id).execute()
                    else:
                        return sb.table("certificate_history").insert(current_payload).execute()
                except Exception as mut_exc:
                    err_str = str(mut_exc)
                    match = re.search(r"Could not find the '([^']+)' column", err_str)
                    if match:
                        col_to_drop = match.group(1)
                        if col_to_drop in current_payload:
                            current_payload.pop(col_to_drop, None)
                            continue
                    print("SUPABASE MUTATE CERTIFICATE ERROR (using fallback):", repr(mut_exc))
                    break

        try:
            if is_resend and target_id is not None:
                _safe_supabase_mutate("update_pk", payload_full, pk_id=target_id)
            else:
                existing_sb = sb.table("certificate_history").select("id").eq("certificate_id", cert_id).limit(1).execute()
                if existing_sb.data and len(existing_sb.data) > 0:
                    _safe_supabase_mutate("update_cert_id", payload_full, c_id=cert_id)
                else:
                    _safe_supabase_mutate("insert", payload_full)
        except Exception as exc:
            print("SUPABASE SAVE CERTIFICATE WARNING (will use local fallback):", repr(exc))

    # SQLite Mutate
    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        cursor = conn.cursor()
        if is_resend and target_id is not None:
            cursor.execute("""
                UPDATE certificate_history SET
                    certificate_id = ?,
                    student_name = ?,
                    student_email = ?,
                    internship_domain = ?,
                    start_date = ?,
                    end_date = ?,
                    issued_date = ?,
                    generated_date = ?,
                    created_at = ?,
                    certificate_status = ?,
                    email_status = ?,
                    sent_at = ?,
                    send_count = ?,
                    pdf_filename = ?,
                    error_message = ?,
                    template_version = ?
                WHERE id = ?
            """, (
                cert_id,
                merged_student_name,
                merged_student_email,
                merged_domain,
                merged_start_date,
                merged_end_date,
                merged_issued_date,
                merged_generated_date,
                merged_created_at,
                merged_certificate_status,
                merged_email_status,
                merged_sent_at,
                merged_send_count,
                merged_pdf_filename,
                merged_error_message,
                merged_template_version,
                target_id
            ))
            if cursor.rowcount == 0:
                cursor.execute("""
                    INSERT OR REPLACE INTO certificate_history (
                        id, certificate_id, student_name, student_email, internship_domain,
                        start_date, end_date, issued_date, generated_date, created_at,
                        certificate_status, email_status, sent_at, send_count,
                        pdf_filename, error_message, template_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    target_id,
                    cert_id,
                    merged_student_name,
                    merged_student_email,
                    merged_domain,
                    merged_start_date,
                    merged_end_date,
                    merged_issued_date,
                    merged_generated_date,
                    merged_created_at,
                    merged_certificate_status,
                    merged_email_status,
                    merged_sent_at,
                    merged_send_count,
                    merged_pdf_filename,
                    merged_error_message,
                    merged_template_version
                ))
        else:
            cursor.execute("SELECT id FROM certificate_history WHERE certificate_id = ? LIMIT 1", (cert_id,))
            row = cursor.fetchone()
            if row:
                cursor.execute("""
                    UPDATE certificate_history SET
                        student_name = ?,
                        student_email = ?,
                        internship_domain = ?,
                        start_date = ?,
                        end_date = ?,
                        issued_date = ?,
                        generated_date = ?,
                        created_at = ?,
                        certificate_status = ?,
                        email_status = ?,
                        sent_at = ?,
                        send_count = ?,
                        pdf_filename = ?,
                        error_message = ?,
                        template_version = ?
                    WHERE certificate_id = ?
                """, (
                    merged_student_name,
                    merged_student_email,
                    merged_domain,
                    merged_start_date,
                    merged_end_date,
                    merged_issued_date,
                    merged_generated_date,
                    merged_created_at,
                    merged_certificate_status,
                    merged_email_status,
                    merged_sent_at,
                    merged_send_count,
                    merged_pdf_filename,
                    merged_error_message,
                    merged_template_version,
                    cert_id
                ))
            else:
                cursor.execute("""
                    INSERT INTO certificate_history (
                        certificate_id, student_name, student_email, internship_domain,
                        start_date, end_date, issued_date, generated_date, created_at,
                        certificate_status, email_status, sent_at, send_count,
                        pdf_filename, error_message, template_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    cert_id,
                    merged_student_name,
                    merged_student_email,
                    merged_domain,
                    merged_start_date,
                    merged_end_date,
                    merged_issued_date,
                    merged_generated_date,
                    merged_created_at,
                    merged_certificate_status,
                    merged_email_status,
                    merged_sent_at,
                    merged_send_count,
                    merged_pdf_filename,
                    merged_error_message,
                    merged_template_version
                ))
        conn.commit()
        conn.close()
    except Exception as exc:
        print("LOCAL DB SAVE CERTIFICATE ERROR:", repr(exc))

    return True


def bulk_save_certificate_records(records_list):
    """
    Save or update a batch of Certificate records in Supabase and local SQLite in bulk.
    Significantly improves bulk generation speed by executing batch operations.
    """
    if not records_list:
        return True

    now_iso = datetime.now(timezone.utc).isoformat()
    clean_records = []
    for r in records_list:
        cid = str(r.get("certificate_id") or "").strip()
        if not cid:
            continue
        clean_records.append({
            "certificate_id": cid,
            "student_name": str(r.get("student_name") or "").strip(),
            "student_email": str(r.get("student_email") or "").strip(),
            "internship_domain": str(r.get("internship_domain") or r.get("domain") or "").strip(),
            "start_date": str(r.get("start_date") or "").strip(),
            "end_date": str(r.get("end_date") or "").strip(),
            "issued_date": str(r.get("issued_date") or "").strip(),
            "generated_date": str(r.get("generated_date") or now_iso).strip(),
            "created_at": str(r.get("created_at") or now_iso).strip(),
            "certificate_status": str(r.get("certificate_status") or "active").strip().lower(),
            "email_status": str(r.get("email_status") or "pending").strip().lower(),
            "sent_at": r.get("sent_at"),
            "send_count": int(r.get("send_count") or 0),
            "pdf_filename": str(r.get("pdf_filename") or "").strip(),
            "error_message": r.get("error_message"),
            "template_version": str(r.get("template_version") or "v1").strip(),
        })

    if not clean_records:
        return True

    # 1. Supabase Bulk Upsert with automatic schema adaptation
    sb = get_supabase_client()
    if sb:
        current_payloads = [dict(p) for p in clean_records]
        for _ in range(5):
            try:
                sb.table("certificate_history").upsert(current_payloads, on_conflict="certificate_id").execute()
                break
            except Exception as mut_exc:
                err_str = str(mut_exc)
                match = re.search(r"Could not find the '([^']+)' column", err_str)
                if match:
                    col_to_drop = match.group(1)
                    for p in current_payloads:
                        p.pop(col_to_drop, None)
                    continue
                else:
                    print("SUPABASE BULK UPSERT WARNING (falling back to per-record save):", repr(mut_exc))
                    for rec in clean_records:
                        try:
                            save_certificate_record(rec)
                        except Exception:
                            pass
                    break


    # 2. Local SQLite Bulk Upsert
    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        cursor = conn.cursor()
        tuples = [
            (
                r["certificate_id"],
                r["student_name"],
                r["student_email"],
                r["internship_domain"],
                r["start_date"],
                r["end_date"],
                r["issued_date"],
                r["generated_date"],
                r["created_at"],
                r["certificate_status"],
                r["email_status"],
                r["sent_at"],
                r["send_count"],
                r["pdf_filename"],
                r["error_message"],
                r["template_version"],
            )
            for r in clean_records
        ]
        cursor.executemany("""
            INSERT OR REPLACE INTO certificate_history (
                certificate_id, student_name, student_email, internship_domain,
                start_date, end_date, issued_date, generated_date, created_at,
                certificate_status, email_status, sent_at, send_count,
                pdf_filename, error_message, template_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, tuples)
        conn.commit()
        conn.close()
    except Exception as exc:
        print("LOCAL DB BULK SAVE ERROR:", repr(exc))

    return True


def _normalize_cert_date(date_str):
    """Normalize date strings to DD-MM-YYYY format for strict identity matching."""
    if not date_str:
        return ""
    date_str = str(date_str).strip()
    if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", date_str):
        try:
            return datetime.strptime(date_str, "%Y-%m-%d").strftime("%d-%m-%Y")
        except Exception:
            pass
    if re.fullmatch(r"\d{1,2}-\d{1,2}-\d{4}", date_str):
        try:
            return datetime.strptime(date_str, "%d-%m-%Y").strftime("%d-%m-%Y")
        except Exception:
            pass
    if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", date_str):
        try:
            return datetime.strptime(date_str, "%d/%m/%Y").strftime("%d-%m-%Y")
        except Exception:
            pass
    return date_str


def find_active_certificate(email, domain, start_date, end_date, supabase_client=None):
    """
    Lookup an existing active certificate matching the exact candidate identity:
    - normalized email (case-insensitive, trimmed)
    - normalized domain (case-insensitive, trimmed)
    - exact start_date (matching normalized date)
    - exact end_date (matching normalized date)
    - certificate_status != 'revoked'
    Candidate name is NOT part of the identity.
    Returns the most recent matching certificate record dict, or None.
    """
    if not email or not domain:
        return None

    email_clean = str(email).strip().lower()
    domain_clean = str(domain).strip().lower()
    start_norm = _normalize_cert_date(start_date)
    end_norm = _normalize_cert_date(end_date)

    if not email_clean or not domain_clean or not start_norm or not end_norm:
        return None

    sb = supabase_client or get_supabase_client()
    supabase_success = False
    candidates = []

    if sb:
        try:
            res = (
                sb.table("certificate_history")
                .select("*")
                .ilike("student_email", email_clean)
                .order("id", desc=True)
                .execute()
            )
            if res.data is not None:
                supabase_success = True
                for row in res.data:
                    candidates.append(dict(row))
        except Exception as exc:
            print("SUPABASE FIND ACTIVE CERTIFICATE WARNING (using fallback):", repr(exc))
            supabase_success = False

    if not supabase_success:
        try:
            conn = sqlite3.connect(SQLITE_DB_PATH)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM certificate_history WHERE LOWER(student_email) = ? ORDER BY id DESC",
                (email_clean,)
            )
            for row in cursor.fetchall():
                candidates.append(dict(row))
            conn.close()
        except Exception as exc:
            print("LOCAL DB FIND ACTIVE CERTIFICATE ERROR:", repr(exc))

    for rec in candidates:
        status = str(rec.get("certificate_status") or "active").strip().lower()
        if status == "revoked":
            continue

        rec_email = str(rec.get("student_email") or "").strip().lower()
        if rec_email != email_clean:
            continue

        rec_domain = str(rec.get("internship_domain") or rec.get("domain") or "").strip().lower()
        if rec_domain != domain_clean:
            continue

        rec_start_norm = _normalize_cert_date(rec.get("start_date"))
        rec_end_norm = _normalize_cert_date(rec.get("end_date"))

        if rec_start_norm == start_norm and rec_end_norm == end_norm:
            return rec

    return None


def find_existing_certificates_batch(candidate_rows, supabase_client=None):
    """
    Efficient batch lookup to detect active certificates that already exist for a batch of candidate records.
    Authoritative source of truth: Supabase (when configured and operational).
    Fallback source: Local SQLite (used ONLY if Supabase is unconfigured or unavailable).
    Returns: dict mapping candidate identity key -> existing certificate dict
    """
    if not candidate_rows:
        return {}

    # Extract clean unique candidate emails
    emails = list(set(
        str(r.get("student_email") or "").strip().lower()
        for r in candidate_rows
        if r.get("student_email")
    ))

    if not emails:
        return {}

    existing_records = []
    supabase_success = False

    # 1. Authoritative batch query to Supabase
    sb = supabase_client or get_supabase_client()
    if sb:
        try:
            res = (
                sb.table("certificate_history")
                .select("*")
                .in_("student_email", emails)
                .execute()
            )
            if res.data is not None:
                supabase_success = True
                for row in res.data:
                    rec = dict(row)
                    if str(rec.get("certificate_status") or "").lower() != "revoked":
                        existing_records.append(rec)
        except Exception as exc:
            print("SUPABASE FIND EXISTING CERTIFICATES WARNING (falling back to local DB):", repr(exc))
            supabase_success = False

    # 2. Local SQLite fallback query — ONLY used if Supabase is unconfigured or query failed
    if not supabase_success:
        try:
            conn = sqlite3.connect(SQLITE_DB_PATH)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            placeholders = ",".join("?" for _ in emails)
            cursor.execute(
                f"SELECT * FROM certificate_history WHERE LOWER(student_email) IN ({placeholders}) AND (certificate_status IS NULL OR certificate_status != 'revoked')",
                emails
            )
            for row in cursor.fetchall():
                rec = dict(row)
                existing_records.append(rec)
            conn.close()
        except Exception as exc:
            print("LOCAL DB FIND EXISTING CERTIFICATES ERROR:", repr(exc))

    # Build lookup map for fast O(1) matching using exact 4-tuple identity key
    lookup = {}
    for cert in existing_records:
        email_clean = str(cert.get("student_email") or "").strip().lower()
        domain_clean = str(cert.get("internship_domain") or cert.get("domain") or "").strip().lower()
        start_clean = str(cert.get("start_date") or "").strip()
        end_clean = str(cert.get("end_date") or "").strip()

        key_full = (email_clean, domain_clean, start_clean, end_clean)
        lookup[key_full] = cert

    return lookup


def get_previous_sent_certificate_by_email(email):
    """
    Check if a successfully sent Certificate record already exists for this email.
    Queries ONLY public.certificate_history.
    Considers ONLY records where email_status = 'sent' (ignores 'pending', 'failed', etc.).
    Trims whitespace and performs case-insensitive comparison.
    Authoritative source: Supabase when configured and operational.
    Local SQLite is used ONLY if Supabase is not configured or throws an actual connection/query exception.
    """
    if not email:
        return None
    email_clean = str(email).strip().lower()

    sb = get_supabase_client()
    if sb:
        try:
            res = (
                sb.table("certificate_history")
                .select("*")
                .ilike("student_email", email_clean)
                .eq("email_status", "sent")
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            # When Supabase query succeeds, its result is authoritative
            if res.data and len(res.data) > 0:
                for row in res.data:
                    rec = dict(row)
                    rec_email = str(rec.get("student_email") or "").strip().lower()
                    if rec_email == email_clean:
                        return rec
                return None
            return None
        except Exception as exc:
            print("SUPABASE GET PREVIOUS SENT CERT ERROR (using fallback):", repr(exc))

    # Fallback to local SQLite ONLY if Supabase is unconfigured or threw an exception
    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM certificate_history WHERE LOWER(TRIM(student_email)) = ? AND email_status = 'sent' ORDER BY id DESC LIMIT 1",
            (email_clean,)
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            return dict(row)
    except Exception as exc:
        print("LOCAL DB GET PREVIOUS SENT CERT ERROR:", repr(exc))

    return None


def find_existing_certificate_emails_batch(candidate_rows_or_emails, supabase_client=None):
    """
    Efficient batch lookup to detect successfully sent Certificate records in public.certificate_history.
    Considers ONLY records where email_status = 'sent' (ignores 'pending', 'failed', etc.).
    Normalizes candidate emails and database emails using email.strip().lower().
    Authoritative source of truth: Supabase (when configured and operational).
    Fallback source: Local SQLite (used ONLY if Supabase is unconfigured or throws an exception).
    Returns: dict mapping lowercase trimmed email -> existing Certificate record dict
    """
    if not candidate_rows_or_emails:
        return {}

    # Extract clean unique candidate emails (normalized: trimmed & lowercase) + query variants
    clean_emails_set = set()
    query_variants_set = set()

    for item in candidate_rows_or_emails:
        if isinstance(item, dict):
            em = item.get("student_email") or item.get("email") or ""
        else:
            em = str(item or "")
        em_str = str(em).strip()
        em_norm = em_str.lower()
        if em_norm:
            clean_emails_set.add(em_norm)
            query_variants_set.add(em_norm)
            if em_str:
                query_variants_set.add(em_str)

    if not clean_emails_set:
        return {}

    query_variants_list = list(query_variants_set)
    clean_emails_list = list(clean_emails_set)
    existing_records = []
    supabase_success = False

    # 1. Authoritative batch query to Supabase
    sb = supabase_client or get_supabase_client()
    if sb:
        try:
            res = (
                sb.table("certificate_history")
                .select("*")
                .in_("student_email", query_variants_list)
                .eq("email_status", "sent")
                .execute()
            )
            if res.data is not None:
                supabase_success = True
                for row in res.data:
                    rec = dict(row)
                    st = str(rec.get("email_status") or "").strip().lower()
                    if st == "sent":
                        existing_records.append(rec)
        except Exception as exc:
            print("SUPABASE FIND EXISTING CERT BATCH WARNING (falling back to local DB):", repr(exc))
            supabase_success = False

    # 2. Local SQLite fallback query — ONLY used if Supabase is unconfigured or query failed
    if not supabase_success:
        try:
            conn = sqlite3.connect(SQLITE_DB_PATH)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            placeholders = ",".join("?" for _ in clean_emails_list)
            cursor.execute(
                f"SELECT * FROM certificate_history WHERE LOWER(TRIM(student_email)) IN ({placeholders}) AND email_status = 'sent'",
                clean_emails_list
            )
            for row in cursor.fetchall():
                existing_records.append(dict(row))
            conn.close()
        except Exception as exc:
            print("LOCAL DB FIND EXISTING CERT BATCH ERROR:", repr(exc))

    # 3. Build lookup map with normalized email keys (trimmed & lowercased)
    lookup = {}
    for r in existing_records:
        row_email = str(r.get("student_email") or "").strip().lower()
        if row_email and row_email in clean_emails_set:
            if row_email not in lookup or int(r.get("send_count") or 0) > int(lookup[row_email].get("send_count") or 0):
                lookup[row_email] = r

    return lookup
def get_certificate_by_id(cert_id, return_error_detail=False):
    """
    Retrieve certificate record by ID.
    Queries Supabase first, falls back to local SQLite.
    Returns: dict representing record, or None (or tuple (None, error_str) if return_error_detail=True)
    """
    if not cert_id:
        return (None, "empty_id") if return_error_detail else None

    cert_clean = str(cert_id).strip()
    db_error = None

    sb = get_supabase_client()
    if sb:
        try:
            res = sb.table("certificate_history").select("*").eq("certificate_id", cert_clean).limit(1).execute()
            if res.data and len(res.data) > 0:
                rec = dict(res.data[0])
                if not rec.get("certificate_status"):
                    rec["certificate_status"] = "active"
                if not rec.get("template_version"):
                    rec["template_version"] = "v1"
                return (rec, None) if return_error_detail else rec
        except Exception as exc:
            db_error = repr(exc)
            # Fall through to check local fallback

    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM certificate_history WHERE certificate_id = ? LIMIT 1", (cert_clean,))
        row = cursor.fetchone()
        conn.close()
        if row:
            rec = dict(row)
            if not rec.get("certificate_status"):
                rec["certificate_status"] = "active"
            if not rec.get("template_version"):
                rec["template_version"] = "v1"
            return (rec, None) if return_error_detail else rec
    except Exception as exc:
        print("LOCAL DB GET CERT ERROR:", repr(exc))
        if not db_error:
            db_error = repr(exc)

    if return_error_detail:
        return (None, db_error)
    return None


def revoke_certificate_record(cert_id, reason="Revoked by administrator"):
    """
    Explicitly revoke a certificate.
    Sets certificate_status = 'revoked' and stores revocation reason.
    """
    if not cert_id:
        return False
    cert_clean = str(cert_id).strip()
    return save_certificate_record({
        "certificate_id": cert_clean,
        "certificate_status": "revoked",
        "error_message": reason
    })


def delete_certificate_record(record_id, document_id=None):
    """
    Delete a certificate record from history.
    Targets ONLY exact ID or exact certificate_id (PXL-CERT-...).
    Never deletes by email address to prevent accidental cascades.
    """
    identifiers = set()
    if record_id is not None and str(record_id).strip():
        identifiers.add(str(record_id).strip())
    if document_id is not None and str(document_id).strip():
        identifiers.add(str(document_id).strip())

    if not identifiers:
        return False

    sb = get_supabase_client()
    if sb:
        deleted_count = 0
        for ident in identifiers:
            try:
                if ident.isdigit():
                    res = sb.table("certificate_history").delete().eq("id", int(ident)).execute()
                    if res and res.data:
                        deleted_count += len(res.data)
                if ident.startswith("PXL-CERT-") or "CERT" in ident:
                    res = sb.table("certificate_history").delete().eq("certificate_id", ident).execute()
                    if res and res.data:
                        deleted_count += len(res.data)
            except Exception as exc:
                print("SUPABASE DELETE CERTIFICATE ERROR:", repr(exc))
                raise RuntimeError(f"Failed to delete certificate from Supabase: {exc}") from exc

        if deleted_count > 0:
            try:
                conn = sqlite3.connect(SQLITE_DB_PATH)
                cursor = conn.cursor()
                for ident in identifiers:
                    if ident.isdigit():
                        cursor.execute("DELETE FROM certificate_history WHERE id = ?", (int(ident),))
                    if ident.startswith("PXL-CERT-") or "CERT" in ident:
                        cursor.execute("DELETE FROM certificate_history WHERE certificate_id = ?", (ident,))
                conn.commit()
                conn.close()
            except Exception as exc:
                print("LOCAL DB DELETE CERTIFICATE SYNC WARNING:", repr(exc))
            return True

        return False

    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()
    total_local_deleted = 0
    for ident in identifiers:
        if ident.isdigit():
            cursor.execute("DELETE FROM certificate_history WHERE id = ?", (int(ident),))
            total_local_deleted += cursor.rowcount
        if ident.startswith("PXL-CERT-") or "CERT" in ident:
            cursor.execute("DELETE FROM certificate_history WHERE certificate_id = ?", (ident,))
            total_local_deleted += cursor.rowcount
    conn.commit()
    conn.close()
    return total_local_deleted > 0


def bulk_delete_records(items):
    """
    Safely delete multiple records from email_history, campus_ambassador_history,
    certificate_history, and campus_ambassador_certificate_history.
    items is a list of dicts: [{'record_type': 'offer_letter'|'ca_letter'|'certificate'|'ca_certificate', 'id': ..., 'document_id': ...}, ...]
    Returns: dict with total_deleted, offer_letters_deleted, ca_letters_deleted, certificates_deleted, ca_certificates_deleted.
    """
    if not items or not isinstance(items, list):
        return {"success": False, "error": "No items provided for deletion.", "total_deleted": 0}

    offer_items_to_delete = []
    ca_items_to_delete = []
    cert_items_to_delete = []
    ca_cert_items_to_delete = []

    for item in items:
        if isinstance(item, dict):
            rec_type = str(item.get("record_type") or "").strip().lower()
            rec_id = item.get("id") or item.get("record_id")
            doc_id = item.get("document_id") or item.get("certificate_id") or item.get("offer_letter_id")
            if not rec_id and not doc_id:
                continue
            if rec_type in ("ca_letter", "ca", "campus_ambassador", "ca_offer_letter", "ca_offer_letters"):
                ca_items_to_delete.append((rec_id, doc_id))
            elif rec_type in ("offer_letter", "offer", "email_history"):
                offer_items_to_delete.append((rec_id, doc_id))
            elif rec_type in ("ca_certificate", "ca_certificates", "ca_cert"):
                ca_cert_items_to_delete.append((rec_id, doc_id))
            elif rec_type in ("certificate", "cert", "certificate_history"):
                cert_items_to_delete.append((rec_id, doc_id))
        elif isinstance(item, (int, str)):
            item_str = str(item).strip()
            if item_str.startswith("PXL-CERT-") or item_str.startswith("CERT-"):
                cert_items_to_delete.append((None, item_str))
            else:
                offer_items_to_delete.append((item_str, None))

    deleted_offers = 0
    deleted_cas = 0
    deleted_certs = 0
    deleted_ca_certs = 0

    for rec_id, doc_id in offer_items_to_delete:
        if delete_offer_letter_record(rec_id, doc_id):
            deleted_offers += 1

    for rec_id, doc_id in ca_items_to_delete:
        if delete_campus_ambassador_record(rec_id, doc_id):
            deleted_cas += 1

    for rec_id, doc_id in cert_items_to_delete:
        if delete_certificate_record(rec_id, doc_id):
            deleted_certs += 1

    for rec_id, doc_id in ca_cert_items_to_delete:
        if delete_ca_certificate_record(rec_id, doc_id):
            deleted_ca_certs += 1

    return {
        "success": True,
        "total_deleted": deleted_offers + deleted_cas + deleted_certs + deleted_ca_certs,
        "offer_letters_deleted": deleted_offers,
        "ca_letters_deleted": deleted_cas,
        "certificates_deleted": deleted_certs,
        "ca_certificates_deleted": deleted_ca_certs,
    }


def delete_all_history_records(record_type="all"):
    """
    Safely purge all records from email_history, campus_ambassador_history,
    certificate_history, and/or campus_ambassador_certificate_history.
    """
    rec_type = str(record_type or "all").strip().lower()
    sb = get_supabase_client()
    deleted_offers = 0
    deleted_cas = 0
    deleted_certs = 0
    deleted_ca_certs = 0

    if sb:
        # 1. Purge Offer Letters if requested
        if rec_type in ("all", "offer_letter", "offer", "offer_letters"):
            try:
                res = sb.table("email_history").delete().neq("id", -999999).execute()
                deleted_offers = len(res.data) if res and res.data else 0
            except Exception as exc:
                print("SUPABASE DELETE ALL OFFER LETTERS ERROR:", repr(exc))
                raise RuntimeError(f"Failed to purge offer letters from Supabase: {exc}") from exc

        # 2. Purge CA Offer Letters if requested
        if rec_type in ("all", "ca_letter", "ca", "campus_ambassador", "ca_offer_letter", "ca_offer_letters"):
            try:
                res = sb.table("campus_ambassador_history").delete().neq("id", -999999).execute()
                deleted_cas = len(res.data) if res and res.data else 0
            except Exception as exc:
                print("SUPABASE DELETE ALL CA LETTERS ERROR:", repr(exc))
                raise RuntimeError(f"Failed to purge CA offer letters from Supabase: {exc}") from exc

        # 3. Purge Certificates if requested
        if rec_type in ("all", "certificate", "cert", "certificate_history", "certificates"):
            try:
                res = sb.table("certificate_history").delete().neq("id", -999999).execute()
                deleted_certs = len(res.data) if res and res.data else 0
            except Exception as exc:
                print("SUPABASE DELETE ALL CERTIFICATES ERROR:", repr(exc))
                raise RuntimeError(f"Failed to purge certificates from Supabase: {exc}") from exc

        # 4. Purge CA Certificates if requested
        if rec_type in ("all", "ca_certificate", "ca_certificates", "ca_cert"):
            try:
                res = sb.table("campus_ambassador_certificate_history").delete().neq("id", -999999).execute()
                deleted_ca_certs = len(res.data) if res and res.data else 0
            except Exception as exc:
                print("SUPABASE DELETE ALL CA CERTIFICATES ERROR:", repr(exc))
                raise RuntimeError(f"Failed to purge CA certificates from Supabase: {exc}") from exc

        # Sync purges to local SQLite mirror if local DB exists
        try:
            conn = sqlite3.connect(SQLITE_DB_PATH)
            cursor = conn.cursor()
            if rec_type in ("all", "offer_letter", "offer", "offer_letters"):
                cursor.execute("DELETE FROM email_history")
            if rec_type in ("all", "ca_letter", "ca", "campus_ambassador", "ca_offer_letter", "ca_offer_letters"):
                cursor.execute("DELETE FROM campus_ambassador_history")
            if rec_type in ("all", "certificate", "cert", "certificate_history", "certificates"):
                cursor.execute("DELETE FROM certificate_history")
            if rec_type in ("all", "ca_certificate", "ca_certificates", "ca_cert"):
                cursor.execute("DELETE FROM campus_ambassador_certificate_history")
            conn.commit()
            conn.close()
        except Exception as exc:
            print("LOCAL DB PURGE SYNC WARNING:", repr(exc))

        return {
            "success": True,
            "total_deleted": deleted_offers + deleted_cas + deleted_certs + deleted_ca_certs,
            "offer_letters_deleted": deleted_offers,
            "ca_letters_deleted": deleted_cas,
            "certificates_deleted": deleted_certs,
            "ca_certificates_deleted": deleted_ca_certs,
        }

    # Offline / local fallback only when no Supabase client is configured
    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()
    if rec_type in ("all", "offer_letter", "offer", "offer_letters"):
        cursor.execute("SELECT count(*) FROM email_history")
        deleted_offers = cursor.fetchone()[0]
        cursor.execute("DELETE FROM email_history")
    if rec_type in ("all", "ca_letter", "ca", "campus_ambassador", "ca_offer_letter", "ca_offer_letters"):
        cursor.execute("SELECT count(*) FROM campus_ambassador_history")
        deleted_cas = cursor.fetchone()[0]
        cursor.execute("DELETE FROM campus_ambassador_history")
    if rec_type in ("all", "certificate", "cert", "certificate_history", "certificates"):
        cursor.execute("SELECT count(*) FROM certificate_history")
        deleted_certs = cursor.fetchone()[0]
        cursor.execute("DELETE FROM certificate_history")
    if rec_type in ("all", "ca_certificate", "ca_certificates", "ca_cert"):
        cursor.execute("SELECT count(*) FROM campus_ambassador_certificate_history")
        deleted_ca_certs = cursor.fetchone()[0]
        cursor.execute("DELETE FROM campus_ambassador_certificate_history")
    conn.commit()
    conn.close()

    return {
        "success": True,
        "total_deleted": deleted_offers + deleted_cas + deleted_certs + deleted_ca_certs,
        "offer_letters_deleted": deleted_offers,
        "ca_letters_deleted": deleted_cas,
        "certificates_deleted": deleted_certs,
        "ca_certificates_deleted": deleted_ca_certs,
    }


# ============================================================
# BULK JOB HISTORY
# ============================================================
def save_bulk_job_record(job_data):
    job_id = job_data.get("job_id") or f"JOB-{uuid.uuid4().hex[:8].upper()}"
    job_data["job_id"] = job_id
    now_iso = datetime.now(timezone.utc).isoformat()
    if not job_data.get("created_at"):
        job_data["created_at"] = now_iso

    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO bulk_job_history (
                job_id, job_type, total_records, successful_count,
                failed_count, created_at, completed_at, status, details
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            job_id,
            job_data.get("job_type", "certificate_bulk"),
            job_data.get("total_records", 0),
            job_data.get("successful_count", 0),
            job_data.get("failed_count", 0),
            job_data.get("created_at"),
            job_data.get("completed_at"),
            job_data.get("status", "completed"),
            str(job_data.get("details", ""))
        ))
        conn.commit()
        conn.close()
    except Exception as exc:
        print("LOCAL DB SAVE BULK JOB ERROR:", repr(exc))

    return job_id


# ============================================================
# UNIFIED HISTORY ENGINE
# ============================================================
def _fetch_all_raw_offer_letters():
    sb = get_supabase_client()
    if sb:
        try:
            res = sb.table("email_history").select("*").order("created_at", desc=True).execute()
            return [dict(r) for r in (res.data or [])]
        except Exception as exc:
            print("SUPABASE FETCH OFFER LETTERS ERROR:", repr(exc))
            raise RuntimeError(f"Failed to fetch offer letters from Supabase: {exc}") from exc

    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM email_history ORDER BY id DESC")
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as exc:
        print("LOCAL DB FETCH OFFER LETTERS ERROR:", repr(exc))
        return []


def _fetch_all_raw_campus_ambassador():
    sb = get_supabase_client()
    if sb:
        try:
            res = sb.table("campus_ambassador_history").select("*").order("created_at", desc=True).execute()
            return [dict(r) for r in (res.data or [])]
        except Exception as exc:
            print("SUPABASE FETCH CA ERROR:", repr(exc))
            raise RuntimeError(f"Failed to fetch CA offer letters from Supabase: {exc}") from exc

    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM campus_ambassador_history ORDER BY id DESC")
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as exc:
        print("LOCAL DB FETCH CA ERROR:", repr(exc))
        return []


def _fetch_all_raw_certificates():
    sb = get_supabase_client()
    if sb:
        try:
            res = sb.table("certificate_history").select("*").order("created_at", desc=True).execute()
            return [dict(r) for r in (res.data or [])]
        except Exception as exc:
            print("SUPABASE FETCH CERTIFICATES ERROR:", repr(exc))
            raise RuntimeError(f"Failed to fetch certificates from Supabase: {exc}") from exc

    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM certificate_history ORDER BY id DESC")
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as exc:
        print("LOCAL DB FETCH CERTIFICATES ERROR:", repr(exc))
        return []


def _fetch_all_raw_ca_certificates():
    sb = get_supabase_client()
    if sb:
        try:
            result = sb.table("campus_ambassador_certificate_history").select("*").order("created_at", desc=True).execute()
            return [dict(row) for row in (result.data or [])]
        except Exception as exc:
            print("SUPABASE FETCH CA CERTIFICATES ERROR:", repr(exc))
            raise RuntimeError(f"Failed to fetch CA certificates from Supabase: {exc}") from exc

    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM campus_ambassador_certificate_history ORDER BY id DESC").fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except Exception as exc:
        print("LOCAL DB FETCH CA CERTIFICATES ERROR:", repr(exc))
        return []


def get_campus_ambassador_stats():
    """
    Get live success and failed counts querying ONLY campus_ambassador_history.
    """
    raw_cas = _fetch_all_raw_campus_ambassador()
    successful = 0
    failed = 0
    for r in raw_cas:
        status_raw = str(r.get("email_status") or "").strip().lower()
        if status_raw in ("sent", "success", "successful"):
            successful += 1
        elif status_raw in ("failed", "failure", "error"):
            failed += 1
    return {
        "success": True,
        "successful": successful,
        "failed": failed,
        "total": successful + failed,
    }


def format_ist_timestamp(ts_str):
    """Format ISO timestamp into Indian Standard Time (IST): DD Mon YYYY, hh:mm AM/PM."""
    if not ts_str or not str(ts_str).strip() or str(ts_str).strip().lower() in ("none", "null", "-", "—", ""):
        return "—"
    ts_clean = str(ts_str).strip()
    try:
        if ts_clean.endswith("Z"):
            dt = datetime.fromisoformat(ts_clean.replace("Z", "+00:00"))
        elif "+" in ts_clean or ("-" in ts_clean[10:] and len(ts_clean) > 10):
            dt = datetime.fromisoformat(ts_clean)
        else:
            dt = datetime.fromisoformat(ts_clean.replace(" ", "T")).replace(tzinfo=timezone.utc)

        ist_tz = timezone(timedelta(hours=5, minutes=30))
        ist_dt = dt.astimezone(ist_tz)
        return ist_dt.strftime("%d %b %Y, %I:%M %p")
    except Exception:
        return ts_clean[:19].replace("T", " ") if ts_clean else "—"


def get_unified_history(
    search="",
    record_type="all",  # 'all', 'offer_letter', 'certificate'
    status_filter="all",  # 'all', 'sent', 'failed', 'pending'
    month_filter="",  # YYYY-MM
    date_filter="all",  # 'today', 'week', 'month', 'custom'
    date_from="",  # YYYY-MM-DD
    date_to="",  # YYYY-MM-DD
    page=1,
    per_page=25,
):
    """
    Unified query across Offer Letters and Certificates.
    Returns:
      - paginated list of unified records
      - global summary counts (calculated on ALL matching database records)
      - pagination metadata
      - available dynamic months
    """
    raw_offers = _fetch_all_raw_offer_letters()
    raw_cas = _fetch_all_raw_campus_ambassador()
    raw_certs = _fetch_all_raw_certificates()
    raw_ca_certs = _fetch_all_raw_ca_certificates()

    unified_list = []

    # 1. Transform Offer Letters (from email_history)
    for r in raw_offers:
        status_raw = str(r.get("email_status") or "").strip().lower()
        if status_raw in ("sent", "success", "successful"):
            status_clean = "sent"
        elif status_raw in ("failed", "failure", "error"):
            status_clean = "failed"
        else:
            # Generated-only / unsent records must NOT inflate counts or sent history
            continue

        created_ts = str(r.get("created_at") or r.get("sent_at") or "")
        sent_ts = str(r.get("sent_at") or "")
        filename = r.get("pdf_filename") or ""
        doc_id = r.get("offer_letter_id") or (Path(filename).stem if filename else f"OL-{r.get('id')}")

        l_type = str(r.get("offer_letter_type") or "").strip().lower()
        domain_val = str(r.get("internship_domain") or "").strip()
        # Backward compatibility for legacy CA records prior to manual SQL migration
        is_ca = l_type in ("campus_ambassador", "ca_letter") or domain_val.lower() == "campus ambassador"
        rec_type = "ca_letter" if is_ca else "offer_letter"
        type_lbl = "CA Offer Letter" if is_ca else "Offer Letter"
        letter_disp = "Campus Ambassador" if is_ca else (r.get("offer_letter_type") or "Offer Letter")

        unified_list.append({
            "id": r.get("id"),
            "record_type": rec_type,
            "type_label": type_lbl,
            "document_id": doc_id,
            "offer_letter_id": doc_id,
            "student_name": r.get("student_name") or "",
            "student_email": r.get("student_email") or "",
            "domain": r.get("internship_domain") or ("Campus Ambassador" if is_ca else ""),
            "duration": r.get("internship_duration") or "",
            "start_date": r.get("start_date") or "",
            "end_date": r.get("end_date") or "",
            "issued_date": "",
            "letter_type": letter_disp,
            "email_status": status_clean,
            "sent_at": sent_ts,
            "sent_at_display": format_ist_timestamp(sent_ts),
            "created_at": created_ts,
            "send_count": r.get("send_count") or 0,
            "pdf_filename": filename,
            "download_url": f"/generated/{filename}" if filename else "",
            "error_message": r.get("error_message") or "",
            "sort_key": created_ts or sent_ts or "0"
        })

    # 2. Transform Dedicated Campus Ambassador Letters (from campus_ambassador_history)
    for r in raw_cas:
        status_raw = str(r.get("email_status") or "").strip().lower()
        if status_raw in ("sent", "success", "successful"):
            status_clean = "sent"
        elif status_raw in ("failed", "failure", "error"):
            status_clean = "failed"
        else:
            continue

        created_ts = str(r.get("created_at") or r.get("sent_at") or "")
        sent_ts = str(r.get("sent_at") or "")
        filename = r.get("pdf_filename") or ""
        doc_id = r.get("offer_letter_id") or (Path(filename).stem if filename else f"CA-{r.get('id')}")

        unified_list.append({
            "id": r.get("id"),
            "record_type": "ca_letter",
            "type_label": "CA Offer Letter",
            "document_id": doc_id,
            "offer_letter_id": doc_id,
            "student_name": r.get("student_name") or "",
            "student_email": r.get("student_email") or "",
            "domain": r.get("internship_domain") or "Campus Ambassador",
            "duration": r.get("internship_duration") or "Tenure",
            "start_date": r.get("start_date") or "",
            "end_date": r.get("end_date") or "",
            "issued_date": "",
            "letter_type": "Campus Ambassador",
            "email_status": status_clean,
            "sent_at": sent_ts,
            "sent_at_display": format_ist_timestamp(sent_ts),
            "created_at": created_ts,
            "send_count": r.get("send_count") or 0,
            "pdf_filename": filename,
            "download_url": f"/generated/{filename}" if filename else "",
            "error_message": r.get("error_message") or "",
            "sort_key": created_ts or sent_ts or "0"
        })

    # 3. Transform Certificates (from certificate_history)
    for r in raw_certs:
        status_raw = str(r.get("email_status") or "").strip().lower()
        if status_raw in ("sent", "success", "successful"):
            status_clean = "sent"
        elif status_raw in ("failed", "failure", "error"):
            status_clean = "failed"
        else:
            status_clean = "pending"

        created_ts = str(r.get("created_at") or r.get("generated_date") or r.get("sent_at") or "")
        sent_ts = str(r.get("sent_at") or "")
        filename = r.get("pdf_filename") or ""
        doc_id = r.get("certificate_id") or f"CERT-{r.get('id')}"

        # On-demand download URL for certificates
        cert_dl_url = f"/verify/{doc_id}/download" if doc_id else (f"/generated/{filename}" if filename else "")

        unified_list.append({
            "id": r.get("id"),
            "record_type": "certificate",
            "type_label": "Certificate",
            "document_id": doc_id,
            "certificate_id": doc_id,
            "student_name": r.get("student_name") or "",
            "student_email": r.get("student_email") or "",
            "domain": r.get("internship_domain") or "",
            "duration": "",
            "start_date": r.get("start_date") or "",
            "end_date": r.get("end_date") or "",
            "issued_date": r.get("issued_date") or "",
            "letter_type": "Certificate",
            "email_status": status_clean,
            "sent_at": sent_ts,
            "sent_at_display": format_ist_timestamp(sent_ts),
            "created_at": created_ts,
            "send_count": r.get("send_count") or 0,
            "pdf_filename": filename,
            "download_url": cert_dl_url,
            "error_message": r.get("error_message") or "",
            "sort_key": created_ts or sent_ts or "0"
        })

    for r in raw_ca_certs:
        status_raw = str(r.get("email_status") or "").strip().lower()
        status_clean = "sent" if status_raw in ("sent", "success", "successful") else (
            "uncertain" if status_raw == "uncertain"
            else ("failed" if status_raw in ("failed", "failure", "error") else "pending")
        )
        created_ts = str(r.get("created_at") or r.get("sent_at") or "")
        sent_ts = str(r.get("sent_at") or "")
        document_id = r.get("document_id") or f"CA-CERT-{r.get('id')}"
        filename = r.get("pdf_filename") or ""
        unified_list.append({
            "id": r.get("id"),
            "record_type": "ca_certificate",
            "type_label": "CA Certificate",
            "document_id": document_id,
            "student_name": r.get("participant_name") or "",
            "student_email": r.get("participant_email") or "",
            "domain": "Campus Ambassador",
            "duration": "",
            "start_date": r.get("program_date") or "",
            "end_date": "",
            "issued_date": "",
            "letter_type": "CA Certificate",
            "email_status": status_clean,
            "sent_at": sent_ts,
            "sent_at_display": format_ist_timestamp(sent_ts),
            "created_at": created_ts,
            "send_count": r.get("send_count") or 0,
            "pdf_filename": filename,
            "download_url": "",
            "error_message": r.get("error_message") or "",
            "sort_key": created_ts or sent_ts or "0"
        })

    # Sort descending by sort_key
    unified_list.sort(key=lambda x: x.get("sort_key", ""), reverse=True)

    # Dynamic Month Extraction
    month_set = set()
    for rec in unified_list:
        ts = str(rec.get("created_at") or rec.get("sent_at") or "")
        if len(ts) >= 7 and ts[:4].isdigit() and ts[5:7].isdigit():
            month_set.add(ts[:7])

    now_ym = datetime.now().strftime("%Y-%m")
    month_set.add(now_ym)
    sorted_yms = sorted(list(month_set), reverse=True)
    available_months = []
    for ym in sorted_yms:
        try:
            dt_obj = datetime.strptime(ym, "%Y-%m")
            available_months.append({"value": ym, "label": dt_obj.strftime("%B %Y")})
        except Exception:
            available_months.append({"value": ym, "label": ym})

    # Apply Filters
    records = list(unified_list)

    # 1. Filter: Record Type
    if record_type and record_type != "all":
        rec_type_clean = record_type.strip().lower()
        if rec_type_clean in ("offer", "offer_letter", "offer_letters"):
            records = [r for r in records if r["record_type"] == "offer_letter"]
        elif rec_type_clean in ("ca", "ca_letter", "ca_offer", "ca_offer_letter", "ca_offer_letters", "campus_ambassador"):
            records = [r for r in records if r["record_type"] == "ca_letter"]
        elif rec_type_clean in ("cert", "certificate", "certificates"):
            records = [r for r in records if r["record_type"] == "certificate"]
        elif rec_type_clean in ("ca_certificate", "ca_certificates", "ca_cert"):
            records = [r for r in records if r["record_type"] == "ca_certificate"]

    # 2. Filter: Live Search
    if search:
        s_low = search.strip().lower()
        records = [
            r for r in records
            if (
                s_low in str(r.get("student_name") or "").lower()
                or s_low in str(r.get("student_email") or "").lower()
                or s_low in str(r.get("domain") or "").lower()
                or s_low in str(r.get("document_id") or "").lower()
            )
        ]

    # 3. Filter: Status
    if status_filter and status_filter != "all":
        st_clean = status_filter.strip().lower()
        records = [
            r for r in records
            if r["email_status"] == st_clean
            or (st_clean == "failed" and r["record_type"] == "ca_certificate"
                and r["email_status"] == "uncertain")
        ]

    # 4. Filter: Month (YYYY-MM)
    if month_filter and month_filter != "all":
        mf_clean = month_filter.strip()
        records = [
            r for r in records
            if (
                str(r.get("created_at") or "").startswith(mf_clean)
                or str(r.get("sent_at") or "").startswith(mf_clean)
            )
        ]

    # 5. Filter: Date Pre-sets & Custom Range
    today_str = datetime.now().strftime("%Y-%m-%d")
    if date_filter == "today":
        records = [
            r for r in records
            if (
                str(r.get("created_at") or "")[:10] == today_str
                or str(r.get("sent_at") or "")[:10] == today_str
            )
        ]
    elif date_filter == "week":
        week_ago_str = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        records = [
            r for r in records
            if (
                (str(r.get("created_at") or "")[:10] >= week_ago_str and str(r.get("created_at") or "")[:10] <= today_str)
                or (str(r.get("sent_at") or "")[:10] >= week_ago_str and str(r.get("sent_at") or "")[:10] <= today_str)
            )
        ]
    elif date_filter == "month":
        records = [
            r for r in records
            if (
                str(r.get("created_at") or "").startswith(now_ym)
                or str(r.get("sent_at") or "").startswith(now_ym)
            )
        ]
    elif date_from or date_to:
        filtered_by_range = []
        for r in records:
            rec_date = str(r.get("created_at") or r.get("sent_at") or "")[:10]
            if date_from and rec_date and rec_date < date_from:
                continue
            if date_to and rec_date and rec_date > date_to:
                continue
            filtered_by_range.append(r)
        records = filtered_by_range

    # Global Summary Counts calculated across ALL matching records (not just this page!)
    total_records = len(records)
    total_offer_letters = sum(1 for r in records if r["record_type"] == "offer_letter")
    total_ca_letters = sum(1 for r in records if r["record_type"] == "ca_letter")
    total_certificates = sum(1 for r in records if r["record_type"] == "certificate")
    total_ca_certificates = sum(1 for r in records if r["record_type"] == "ca_certificate")
    total_sent = sum(1 for r in records if r["email_status"] == "sent")
    total_failed = sum(
        1 for r in records
        if r["email_status"] in {"failed", "uncertain"}
    )
    total_pending = sum(1 for r in records if r["email_status"] == "pending")

    # Pagination
    try:
        per_page = int(per_page)
        if per_page not in (10, 25, 50, 100, 200, 100000):
            per_page = 25
    except Exception:
        per_page = 25

    total_pages = max(1, (total_records + per_page - 1) // per_page)
    try:
        page = int(page)
    except Exception:
        page = 1
    if page < 1:
        page = 1
    if page > total_pages:
        page = total_pages

    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    paginated_records = records[start_idx:end_idx]

    return {
        "records": paginated_records,
        "all_filtered_records": records,
        "total_records": total_records,
        "total_offer_letters": total_offer_letters,
        "total_ca_letters": total_ca_letters,
        "total_certificates": total_certificates,
        "total_ca_certificates": total_ca_certificates,
        "total_sent": total_sent,
        "total_failed": total_failed,
        "total_pending": total_pending,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "available_months": available_months,
    }
