"""A bounded, unverified warm start for display, never an input to poller decisions."""

from __future__ import annotations

import json
import logging
import math
import stat
from dataclasses import asdict, dataclass, fields

from orch.host import custody
from orch.host.errors import HostError

from . import ingest, token
from .poller import Snapshot

CAP = 4 * 1024 * 1024
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoadedCache:
    snapshot: Snapshot

    @property
    def as_of(self):
        return self.snapshot.as_of

    def __post_init__(self):
        if self.snapshot.verified:
            raise ValueError("cache cannot be verified")


def document(snapshot: Snapshot) -> dict:
    return {
        "pulls": [
            {
                "pull": asdict(p),
                "checks": [asdict(c) for c in snapshot.checks.get((p.repo, p.number), ())],
                "reviews": asdict(snapshot.reviews.get((p.repo, p.number), ingest.ReviewSummary())),
                "status": snapshot.statuses.get((p.repo, p.number), "unknown"),
            }
            for p in snapshot.pulls
        ],
        "issues": [asdict(i) for i in snapshot.issues],
    }


def _shape(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise ValueError("cache shape")


def _row(row, cls):
    _shape(row, (f.name for f in fields(cls)))
    for f in fields(cls):
        value = row[f.name]
        valid = {
            "str": lambda value=value: type(value) is str,
            "int": lambda value=value: ingest.integer(value),
            "bool": lambda value=value: type(value) is bool,
            "tuple[str, ...]": lambda value=value: (
                type(value) in (list, tuple) and len(value) <= 10 and all(type(x) is str for x in value)
            ),
        }
        if f.type in valid and not valid[f.type]():
            raise ValueError("cache field")


def _decode(doc, saved_at):
    _shape(doc, {"pulls", "issues"})
    if any(type(doc[k]) is not list or len(doc[k]) > 4000 for k in doc):
        raise ValueError("cache rows")
    pulls, issues, checks, reviews, statuses = [], [], {}, {}, {}
    for entry in doc["pulls"]:
        _shape(entry, {"pull", "checks", "reviews", "status"})
        row = entry["pull"]
        _row(row, ingest.Pull)
        p = ingest.saved_pull(row)
        if p is None or type(entry["checks"]) is not list or len(entry["checks"]) > 20:
            raise ValueError("cache pull")
        key = (p.repo, p.number)
        if key in checks:
            raise ValueError("duplicate pull")
        pulls.append(p)
        raw_checks = []
        for check in entry["checks"]:
            _shape(check, {"name", "status", "conclusion", "app"})
            if (
                any(type(check[k]) is not str for k in ("name", "status", "app"))
                or check["status"] not in {"queued", "in_progress", "completed", "unknown"}
                or check["conclusion"] is not None
                and (type(check["conclusion"]) is not str or check["conclusion"] not in ingest.CONCLUSIONS)
            ):
                raise ValueError("cache check")
            raw_checks.append(
                {**check, "name": ingest.saved_text(check["name"]), "app": {"name": ingest.saved_text(check["app"])}}
            )
        checks[key] = ingest.checks({"check_runs": raw_checks})
        summary = entry["reviews"]
        _shape(summary, {"approved", "changes_requested", "commented"})
        if any(not ingest.integer(v, 0, 10000) for v in summary.values()):
            raise ValueError("cache reviews")
        reviews[key] = ingest.saved_reviews(summary)
        status = entry["status"]
        if type(status) is not str or status not in {"error", "failure", "pending", "success", "unknown"}:
            raise ValueError("cache status")
        statuses[key] = ingest.combined_status({"state": status})
    for row in doc["issues"]:
        _row(row, ingest.Issue)
        item = ingest.saved_issue(row)
        if item is None:
            raise ValueError("cache issue")
        issues.append(item)
    if len({p.repo for p in (*pulls, *issues)}) > 20:
        raise ValueError("cache repos")
    return Snapshot(tuple(pulls), tuple(issues), checks, reviews, statuses, saved_at, False, "stale")


def save(state_dir, ws_id, host_uid, snapshot_doc, saved_at):
    """Only publish a complete bounded document, with the same custody as the token directory."""
    doc = {"v": 1, "saved_at": saved_at, "snapshot": snapshot_doc}
    if len(json.dumps(doc).encode()) > CAP:
        raise ValueError("cache too large")
    _timestamp(saved_at)
    _decode(snapshot_doc, saved_at)
    directory = token.github_dir(state_dir, ws_id)
    custody.ensure_private_dir(directory, host_uid, create=True)
    custody.write_private(directory / "cache.json", doc)


def _timestamp(value):
    if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
        raise ValueError("cache timestamp")


def load(state_dir, ws_id, host_uid) -> LoadedCache | None:
    try:
        directory = token.github_dir(state_dir, ws_id)
        if not directory.exists():
            return None
        custody.check_tree(directory, host_uid)
        if stat.S_IMODE(directory.lstat().st_mode) != 0o700:
            raise ValueError("cache directory mode")
        doc = custody.read_json(directory / "cache.json", host_uid, limit=CAP)
        if doc is None:
            return None
        _shape(doc, {"v", "saved_at", "snapshot"})
        if type(doc["v"]) is not int or doc["v"] != 1:
            raise ValueError("cache version")
        _timestamp(doc["saved_at"])
        return LoadedCache(_decode(doc["snapshot"], doc["saved_at"]))
    except (HostError, OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        log.warning("github cache rejected")
        return None


def remove(state_dir, ws_id, host_uid):
    """Disable removes display data but leaves credential custody to token.remove."""
    directory = token.github_dir(state_dir, ws_id)
    if directory.exists():
        custody.check_tree(directory, host_uid)
        (directory / "cache.json").unlink(missing_ok=True)
