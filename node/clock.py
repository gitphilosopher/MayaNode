"""node/clock.py — the one place UTC timestamps are produced/parsed."""
from datetime import datetime, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    """Fixed-width UTC ISO-8601 (milliseconds), so text ordering == time ordering."""
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def utc_now_iso() -> str:
    return to_iso(utc_now())


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)
