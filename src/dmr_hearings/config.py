"""Where the database and the embedding model live on disk.

Set DMR_DATA_DIR to put them somewhere specific. Otherwise they go in the
per-user data directory (~/.local/share/dmr-hearings on Linux,
~/Library/Application Support/dmr-hearings on macOS, %LOCALAPPDATA%\\dmr-hearings on Windows).
"""

import os
import sys
from pathlib import Path


def data_dir() -> Path:
    if override := os.environ.get("DMR_DATA_DIR"):
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "dmr-hearings"


def db_path() -> Path:
    return data_dir() / "hearings.db"


def model_dir() -> Path:
    """Where fastembed caches the embedding model (~220 MB, downloaded on first use)."""
    return data_dir() / "models"
