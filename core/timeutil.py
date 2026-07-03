"""Small shared helpers for working in Singapore time (UTC+8, no DST)."""
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from core.config import TIMEZONE

SGT = ZoneInfo(TIMEZONE)


def sgt_now() -> datetime:
    return datetime.now(SGT)


def sgt_today() -> date:
    return sgt_now().date()


def to_iso(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def from_iso(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def sgt_display(dt: datetime) -> str:
    """Human-readable SGT timestamp."""
    return dt.strftime("%d %b %Y %H:%M SGT")
