"""Consistent backup of the SQLite database. Run daily (see DEPLOYMENT.md). Keeps 30 days."""
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

data = Path(os.getenv("DATA_DIR", "./data"))
db = data / "app.db"
if not db.exists():
    raise SystemExit("No SQLite database found (if you use PostgreSQL, back it up with pg_dump).")
out = data / "backups"
out.mkdir(exist_ok=True)
dest = out / f"app-{datetime.now():%Y%m%d-%H%M}.db"
src, dst = sqlite3.connect(db), sqlite3.connect(dest)
src.backup(dst)          # safe even while the app is running
dst.close(); src.close()
for f in out.glob("app-*.db"):
    if time.time() - f.stat().st_mtime > 30 * 86400:
        f.unlink()
print("backup written:", dest)
