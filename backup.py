"""Nightly backup: zips everything customers have saved, keeps the newest 7 zips.

    python3 backup.py            (makes backups/sellingteen-backup-YYYYMMDD-HHMMSS.zip)

What goes in the zip:
  - data/                     every app's database + uploads, and the Control Center records
  - apps/<app>/instance/      apps that keep their data in their own instance folder
  - env/ and site.env         settings (they hold secret keys, so the backup zip is PRIVATE)
Databases are copied with SQLite's own safe "backup" command, so a copy taken while someone is using the app is not broken.
"""
import os
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
KEEP = 7
SQLITE_MAGIC = b"SQLite format 3\x00"


def _is_sqlite(path):
    try:
        with open(path, "rb") as f:
            return f.read(16) == SQLITE_MAGIC
    except OSError:
        return False


def _files(root):
    sources = [root / "data", root / "env"]
    sources += sorted((root / "apps").glob("*/instance"))
    for src in sources:
        if src.is_dir():
            for p in sorted(src.rglob("*")):
                if p.is_file() and not p.name.endswith(("-journal", "-wal", "-shm")):
                    yield p
    if (root / "site.env").is_file():
        yield root / "site.env"
    if (root / "webhooks.json").is_file():
        yield root / "webhooks.json"


def make_backup(root=HERE, out_dir=None, keep=KEEP, now=None):
    root = Path(root)
    out_dir = Path(out_dir) if out_dir else root / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    target = out_dir / f"sellingteen-backup-{stamp}.zip"
    partial = target.with_suffix(".zip.part")
    count = 0
    with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as z, tempfile.TemporaryDirectory() as tmp:
        for path in _files(root):
            arc = path.relative_to(root).as_posix()
            if _is_sqlite(path):
                copy = Path(tmp) / f"db{count}"
                src = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
                dst = sqlite3.connect(copy)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
                    src.close()
                z.write(copy, arc)
            else:
                z.write(path, arc)
            count += 1
    os.replace(partial, target)
    old = sorted(out_dir.glob("sellingteen-backup-*.zip"))
    for extra in old[:-keep] if keep > 0 else []:
        extra.unlink()
    return target, count


if __name__ == "__main__":
    path, n = make_backup()
    print(f"Backup saved: {path} ({n} files). Keeping the newest {KEEP}.")
    sys.exit(0)
