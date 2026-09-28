import os
from pathlib import Path

from dotenv import load_dotenv


def load_application_environment(base_dir=None):
    configured_mode = os.getenv("PERSEVEX_ENV", "").strip().lower()
    is_vercel = os.getenv("VERCEL") == "1"

    if is_vercel:
        if configured_mode and configured_mode != "production":
            raise RuntimeError("PERSEVEX_ENV must be production when running on Vercel.")
        return "production"

    mode = configured_mode or "development"
    if mode == "development":
        root = Path(base_dir) if base_dir else Path(__file__).resolve().parent
        env_path = root / ".env.development"
        if not env_path.is_file():
            raise RuntimeError("Development requires an explicit .env.development file.")
        load_dotenv(env_path, override=True)
    elif mode not in {"production", "test"}:
        raise RuntimeError("PERSEVEX_ENV must be development, production, or test.")

    return mode


def require_environment_variable(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} must be configured.")
    return value


def get_environment_info():
    import urllib.parse

    mode = os.getenv("PERSEVEX_ENV", "development").strip().lower()
    supabase_url = os.getenv("SUPABASE_URL", "")
    ref = ""
    host = ""
    if supabase_url:
        parsed = urllib.parse.urlparse(supabase_url)
        host = parsed.netloc
        ref = host.split(".")[0] if "." in host else host
    return {
        "mode": mode or "development",
        "supabase_host": host or "none",
        "supabase_ref": ref or "unconfigured",
        "is_production": mode == "production",
    }
