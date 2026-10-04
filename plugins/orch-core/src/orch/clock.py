from datetime import datetime, timezone
from functools import lru_cache


def now() -> datetime:
    return datetime.now(timezone.utc)


def stamp(dt: datetime | None = None) -> str:
    return (dt or now()).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def stamp_s(dt: datetime | None = None) -> str:
    return (dt or now()).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@lru_cache(maxsize=65536)  # pages parse the same few thousand event stamps on every request; datetimes are immutable
def parse_stamp(s: str) -> datetime:
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%MZ"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    raise ValueError(f"not a timestamp: {s!r}")
