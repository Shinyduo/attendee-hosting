"""
Redis and Celery configuration for attendee project.
Provides a safe fallback for Redis configuration when environment variables are missing.
"""

import os

# ---- Redis/Celery Configuration ----
# Try to get Redis URL from environment variables
REDIS_URL = os.getenv("REDIS_URL")

# Configure Redis URL with SSL options if needed
if REDIS_URL:
    if os.getenv("DISABLE_REDIS_SSL"):
        REDIS_CELERY_URL = REDIS_URL + "?ssl_cert_reqs=none"
    else:
        REDIS_CELERY_URL = REDIS_URL
else:
    # Use a default Redis URL for development if none is provided
    REDIS_CELERY_URL = "redis://localhost:6379/0"

# Configure Celery
CELERY_BROKER_URL = REDIS_CELERY_URL
CELERY_RESULT_BACKEND = REDIS_CELERY_URL
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"

# Celery 6+ compatible startup retry flag to keep behavior consistent
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
