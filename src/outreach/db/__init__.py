"""SQLite storage: `runs.db` (checkpoints and per-run tables) and `memory.db` (cross-run memory)."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def connect(path: Path | str, *, read_only: bool = False) -> sqlite3.Connection:
    """Opens a connection with the project's standard settings."""
    if read_only:
        conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, check_same_thread=False)
    else:
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def city_key(city: str) -> str:
    """Matching key for a city: lowercased, state suffix dropped ("Tucson, AZ" -> "tucson")."""
    return " ".join(city.split(",", 1)[0].lower().split())
