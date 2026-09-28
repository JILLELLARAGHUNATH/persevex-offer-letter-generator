import os
from pathlib import Path
from environment_config import load_application_environment

BASE_DIR = Path(__file__).resolve().parent.parent
load_application_environment(BASE_DIR)

# Database configuration
DATABASE_TYPE = os.getenv("DATABASE_TYPE", "supabase").strip().lower()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# Storage configuration
STORAGE_TYPE = os.getenv("STORAGE_TYPE", "local").strip().lower()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://persevex.vercel.app").strip().rstrip("/")

# Supabase configuration (Current default)
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()

# AWS S3 Configuration (Prepared for future AWS integration)
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID", "").strip()
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "").strip()
AWS_REGION = os.getenv("AWS_REGION", "us-east-1").strip()
AWS_S3_BUCKET = os.getenv("AWS_S3_BUCKET", "").strip()

# Local SQLite database path
SQLITE_DB_PATH = Path(os.getenv(
    "PERSEVEX_SQLITE_DB_PATH",
    str(BASE_DIR / "certificates_local.db"),
))
