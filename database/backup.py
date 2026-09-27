"""database/backup.py — snapshot MayaNode's SQLite database to a
separate file, and restore from one.

Uses sqlite3's built-in online backup API (Connection.backup()), NOT a
raw file copy. That distinction matters under WAL: a plain `cp` of the
.db file can grab an inconsistent snapshot while committed data still
sits in the -wal sidecar file, especially on a device that might crash
or lose power mid-copy. The backup API instead reads a transactionally
consistent snapshot page-by-page while the source database stays fully
usable (readers and the one writer keep working throughout).

Runs as a plain script, not a background scheduler — Phase 3 rules say
no schedulers live inside the app yet. Point cron/Termux:Boot/systemd
timers at it, or run it by hand.

Usage:
    python -m database.backup                 # create a backup, prune old ones
    python -m database.backup --list           # list existing backups
    python -m database.backup --restore FILE   # restore FILE over the live DB
"""
import argparse
import logging
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from node import config
from database.connection import connect

log = logging.getLogger("mayanode.backup")

_FILENAME_RE_PREFIX = "mayanode-"
_FILENAME_SUFFIX = ".db"


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def create_backup(dest_dir: Path | None = None) -> Path:
    """Write a consistent snapshot of config.DB_PATH into dest_dir
    (default config.BACKUP_DIR), named mayanode-<UTC timestamp>.db.
    Prunes older backups down to config.BACKUP_RETENTION afterward (0
    means keep everything). Returns the new backup's path."""
    dest_dir = dest_dir or config.BACKUP_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)

    if not config.DB_PATH.exists():
        raise FileNotFoundError(f"no database to back up at {config.DB_PATH}")

    dest_path = dest_dir / f"{_FILENAME_RE_PREFIX}{_timestamp()}{_FILENAME_SUFFIX}"
    if dest_path.exists():
        # Same-second collision (unlikely, but backups can be scripted
        # tightly in a test loop) — disambiguate rather than overwrite.
        n = 2
        while (dest_dir / f"{_FILENAME_RE_PREFIX}{_timestamp()}-{n}{_FILENAME_SUFFIX}").exists():
            n += 1
        dest_path = dest_dir / f"{_FILENAME_RE_PREFIX}{_timestamp()}-{n}{_FILENAME_SUFFIX}"

    src = connect()
    dst = sqlite3.connect(dest_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    log.info(f"backup written to {dest_path}")
    pruned = _prune_old_backups(dest_dir)
    for p in pruned:
        log.info(f"pruned old backup {p}")
    return dest_path


def list_backups(dest_dir: Path | None = None) -> list[Path]:
    """Existing backups under dest_dir, oldest first."""
    dest_dir = dest_dir or config.BACKUP_DIR
    if not dest_dir.exists():
        return []
    return sorted(dest_dir.glob(f"{_FILENAME_RE_PREFIX}*{_FILENAME_SUFFIX}"))


def _prune_old_backups(dest_dir: Path) -> list[Path]:
    if config.BACKUP_RETENTION <= 0:
        return []
    backups = list_backups(dest_dir)
    excess = len(backups) - config.BACKUP_RETENTION
    if excess <= 0:
        return []
    to_delete = backups[:excess]
    for p in to_delete:
        p.unlink(missing_ok=True)
    return to_delete


def restore_backup(backup_path: Path, *, confirm: bool = False) -> Path:
    """Restore backup_path over the live database at config.DB_PATH.

    Refuses to run unless confirm=True (the CLI requires --yes) — this
    is destructive and must only be run with the MayaNode process
    stopped, since it replaces the file SQLite connections are using.
    The current live database (plus its -wal/-shm sidecar files, if
    any) is moved aside to a .pre-restore backup first, so a mistaken
    restore is itself recoverable.
    """
    if not confirm:
        raise ValueError("restore_backup() requires confirm=True — this overwrites the live database")
    backup_path = Path(backup_path)
    if not backup_path.is_file():
        raise FileNotFoundError(f"backup file not found: {backup_path}")

    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    if config.DB_PATH.exists():
        ts = _timestamp()
        safety_copy = config.DB_PATH.with_name(f"{config.DB_PATH.stem}.pre-restore-{ts}{config.DB_PATH.suffix}")
        shutil.copy2(config.DB_PATH, safety_copy)
        log.warning(f"existing database saved to {safety_copy} before restore")
        # Carry along any not-yet-checkpointed WAL/shared-memory sidecar
        # files too, so the safety copy is restorable on its own.
        for suffix in ("-wal", "-shm"):
            sidecar = config.DB_PATH.with_name(config.DB_PATH.name + suffix)
            if sidecar.exists():
                shutil.copy2(sidecar, safety_copy.with_name(safety_copy.name + suffix))

    # Restoring via the backup API (rather than a raw file copy) so we
    # also get a clean WAL/journal state on the live path regardless of
    # what mode the backup file was captured in.
    src = sqlite3.connect(backup_path)
    dst = connect()
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    log.warning(f"database restored from {backup_path}")
    return config.DB_PATH


def _main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

    parser = argparse.ArgumentParser(description="Backup or restore MayaNode's SQLite database.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--list", action="store_true", help="list existing backups and exit")
    group.add_argument("--restore", metavar="FILE", help="restore FILE over the live database")
    parser.add_argument("--yes", action="store_true", help="required alongside --restore to confirm")
    args = parser.parse_args(argv)

    if args.list:
        backups = list_backups()
        if not backups:
            print("no backups found")
        for p in backups:
            print(p)
        return 0

    if args.restore:
        if not args.yes:
            print("refusing to restore without --yes (this overwrites the live database)", file=sys.stderr)
            print("stop the MayaNode server first, then re-run with --yes", file=sys.stderr)
            return 1
        restore_backup(Path(args.restore), confirm=True)
        print(f"restored {args.restore} -> {config.DB_PATH}")
        return 0

    path = create_backup()
    print(f"backup written to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
