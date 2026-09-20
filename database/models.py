import re
from datetime import datetime, timezone

def normalize_status(status_str):
    if not status_str:
        return "pending"
    s = str(status_str).strip().lower()
    if s in ("sent", "success", "successful"):
        return "sent"
    if s in ("failed", "failure", "error"):
        return "failed"
    return "pending"

def format_iso_timestamp(dt=None):
    if dt is None:
        dt = datetime.now(timezone.utc)
    return dt.isoformat()

def parse_display_date(raw_date):
    if not raw_date:
        return ""
    raw_str = str(raw_date).strip()
    # YYYY-MM-DD to DD-MM-YYYY
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_str):
        try:
            return datetime.strptime(raw_str, "%Y-%m-%d").strftime("%d-%m-%Y")
        except Exception:
            return raw_str
    return raw_str
