"""Per-user app preferences, stored as JSON beside the control log."""
import json
import logging
import math
import os
from pathlib import Path
import tempfile

import sdk_setup

DEFAULTS = {"check_for_updates": True, "skipped_version": "", "last_update_check": 0.0}


def location():
    return sdk_setup.data_folder() / "preferences.json"


def valid(value, default):
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    return isinstance(value, type(default))


def load():
    """Return saved preferences; missing, damaged, or mistyped values fall back to defaults."""
    values = dict(DEFAULTS)
    try:
        document = json.loads(location().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return values
    if isinstance(document, dict):
        for key, default in DEFAULTS.items():
            if valid(document.get(key), default):
                values[key] = type(default)(document[key])
    return values


def save(values):
    """Atomically persist known preferences. Returns False instead of raising on disk errors."""
    document = {key: values.get(key, default) if valid(values.get(key, default), default) else default
                for key, default in DEFAULTS.items()}
    folder = location().parent
    try:
        folder.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix="preferences-", suffix=".tmp", dir=folder)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(document, stream, indent=2)
            os.replace(temporary, location())
        finally:
            Path(temporary).unlink(missing_ok=True)
        return True
    except OSError:
        logging.warning("Could not save preferences", exc_info=True)
        return False
