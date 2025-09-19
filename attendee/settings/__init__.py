"""
Django settings module initialization.
This file loads the appropriate settings file based on the environment.
"""

import os
import importlib

# Get the environment setting from the environment variable or default to development
ENVIRONMENT = os.getenv("DJANGO_ENVIRONMENT", "development").lower()

# Map environment names to settings modules
SETTINGS_MODULE = {
    "development": "attendee.settings.development",
    "production": "attendee.settings.production",
    "production-gke": "attendee.settings.production-gke",
    "staging-gke": "attendee.settings.staging-gke",
    "test": "attendee.settings.test",
}.get(ENVIRONMENT, "attendee.settings.development")

# Set the settings module for Django to use
os.environ.setdefault("DJANGO_SETTINGS_MODULE", SETTINGS_MODULE)

# For direct imports in this module, make them from the base (development is the safest fallback)
from .development import *  # noqa
