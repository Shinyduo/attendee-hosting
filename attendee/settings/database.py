"""
Database configuration for attendee project.
Provides a safe fallback for database configuration when environment variables are missing.
"""

import os
from pathlib import Path

# Get the project base directory (adjust the number of .parent calls based on file location)
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# ---- Database Configuration ----
# Prefer a single URL (Railway style). Accept a few common variants.
_db_url = (
    os.getenv("DATABASE_URL")
    or os.getenv("POSTGRES_URL")
    or os.getenv("POSTGRESURL")
)

if _db_url:
    # Use dj-database-url if available
    try:
        import dj_database_url  # type: ignore
        DATABASES = {
            "default": dj_database_url.parse(_db_url, conn_max_age=600)
        }
    except ImportError:
        # Fallback if dj-database-url is not installed
        DATABASES = {
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": os.getenv("DB_NAME", "attendee_development"),
                "USER": os.getenv("DB_USER", os.getenv("POSTGRES_USER", "attendee_development_user")),
                "PASSWORD": os.getenv("DB_PASSWORD", os.getenv("POSTGRES_PASSWORD", "attendee_development_user")),
                "HOST": os.getenv("DB_HOST", os.getenv("POSTGRES_HOST", "localhost")),
                "PORT": os.getenv("DB_PORT", "5432"),
            }
        }
else:
    # Use explicitly defined database parameters if available
    postgres_host = os.getenv("POSTGRES_HOST")
    if postgres_host:
        DATABASES = {
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": os.getenv("POSTGRES_DB", "attendee_development"),
                "USER": os.getenv("POSTGRES_USER", "attendee_development_user"),
                "PASSWORD": os.getenv("POSTGRES_PASSWORD", "attendee_development_user"),
                "HOST": postgres_host,
                "PORT": os.getenv("POSTGRES_PORT", "5432"),
            }
        }
    else:
        # Final fallback to SQLite so the app never crashes with the dummy backend
        DATABASES = {
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": BASE_DIR / "db.sqlite3",
            }
        }