"""Turn `orch init` answer flags (used by the orch-setup skill) into a validated config dict."""
from __future__ import annotations

from orch.config.load import DEFAULTS, check_prefix, deep_merge, validate_schema
from orch.errors import ValidationError

_AGENT_MAY = {"commit": "commit", "push": "push", "review": "open_review"}


def parse_tracker(spec: str) -> dict:
    parts = spec.split("=", 2)
    if len(parts) != 3 or not all(p.strip() for p in parts):
        raise ValidationError(f"--tracker {spec!r}: expected PREFIX=PATTERN=URL, e.g. ABC=ABC-\\d+=https://jira/browse/{{key}} "
                              f"(or GH=GH-(?P<id>\\d+)=https://github.com/o/r/issues/{{id}})")
    prefix, pattern, url = (p.strip() for p in parts)
    from orch.core.trackers import tracker_problem
    problem = tracker_problem({"prefix": prefix, "pattern": pattern, "url": url})
    if problem:
        raise ValidationError(f"--tracker {spec!r}: {problem}")
    return {"prefix": prefix, "pattern": pattern, "url": url}


def parse_repo(spec: str) -> tuple[str, str | None]:
    name, sep, path = spec.partition("=")
    name = name.strip()
    if not name:
        raise ValidationError(f"--repo {spec!r}: expected NAME or NAME=PATH")
    return name, (path.strip() or None) if sep else None


def parse_agent_may(spec: str) -> dict:
    items = {s.strip().lower() for s in spec.split(",") if s.strip()}
    if items in (set(), {"none"}):
        items = set()
    unknown = items - set(_AGENT_MAY)
    if unknown:
        raise ValidationError(f"--agent-may: unknown {', '.join(sorted(unknown))}; use commit, push, review or none")
    return {key: name in items for name, key in _AGENT_MAY.items()}


def build_config(*, customer: str, prefix: str, harnesses: list[str], trackers: list[dict], git_type: str | None,
                 git_base_url: str | None, review_term: str | None, agent_may: dict | None,
                 repos: list[tuple[str, str | None]], check_since: str | None = None) -> dict:
    prefix = prefix.upper()
    check_prefix(prefix)
    git: dict = {}
    if git_type:
        git["type"] = git_type
    if git_base_url:
        git["base_url"] = git_base_url
    if review_term:
        git["review_term"] = review_term.upper()
    if agent_may is not None:
        git["agent_may"] = agent_may
    if repos:
        git["repos"] = {name: ({"path": path} if path else {}) for name, path in repos}
    cfg = deep_merge(DEFAULTS, {"customer": customer, "id": {"prefix": prefix, "pad": 4},
                                "harnesses": list(dict.fromkeys(harnesses)), "external_trackers": trackers,
                                "git": git, **({"check": {"since": check_since}} if check_since else {})})
    errors = validate_schema(cfg)
    if errors:
        raise ValidationError("invalid setup answers: " + "; ".join(errors))
    return cfg
