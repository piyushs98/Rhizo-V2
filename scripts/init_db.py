#!/usr/bin/env python3
"""Create the database and its schema. Idempotent."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.db.connection import init_db, query_one

if __name__ == "__main__":
    init_db()
    row = query_one("SELECT COUNT(*) n FROM positions")
    print(f"Database ready at {settings.db_path} ({row['n']} positions).")
