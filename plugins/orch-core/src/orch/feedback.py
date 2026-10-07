"""Feedback about orch itself (#37). An agent that finds an orch command confusing or broken runs `orch feedback add`
once and carries on. The report is redacted when it is written (no workspace paths, customer, repo or tracker names,
ticket ids, titles or text, URLs, emails or token-like strings) and stays on this machine, in the orch config dir,
outside every repository. Only the human reads the queue and, with `orch feedback file`, sends one report to the
orch-core repository after seeing the exact text. Agents never file issues: they work in customer workspaces, and an
issue on orch-core is an outward action that could carry customer details."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orch.errors import NotFoundError, OrchError, UsageError

REPO = "severinlindenmann/orch-core"
DAILY_LIMIT = 10  # new reports per workspace in 24 hours; repeats of one report only raise its count
MAX_COMMAND = 300
MAX_PROBLEM = 2000
MAX_ERROR = 3000
_ID = re.compile(r"fb-[0-9a-f]{12}")
_URL = re.compile(r"\b(?:https?|ssh|git|file)://\S+|\bgit@[\w.-]+:\S+", re.I)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_JWT = re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]*")
# name=value / name: value secrets ("Authorization: Bearer x" too); the name stays, the value goes
_SECRET_VALUE = re.compile(r"(?i)(?<![A-Za-z0-9])(authorization|api[_-]?key|token|secret|passwd|password|pwd|key|auth"
                           r"|bearer)(\s*[=:]\s*)(?:bearer\s+)?\S+")
_BEARER = re.compile(r"(?i)(?<![A-Za-z0-9])bearer\s+\S+")
# well-known credential prefixes, whatever their length (GitHub, Databricks, AWS, Slack, OpenAI-style, GitLab)
_SECRET_PREFIX = re.compile(r"(?<![A-Za-z0-9])(?:(?:ghp_|gho_|ghs_|ghu_|ghr_|github_pat_|xox[abpr]-|sk-|glpat-)"
                            r"[A-Za-z0-9_\-]+|(?:dapi|dose)[0-9a-f]{8,}|AKIA[0-9A-Z]{8,})")
_UNC_PATH = re.compile(r"\\\\[^\s\\'\"]+(?:\\[^\s'\"]*)?")
_WIN_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:\\[^\s'\"]*")
_POSIX_PATH = re.compile(r"(?<![\w.<>~])/(?:[^\s/'\"]+/)+[^\s/'\"]*")
# a bare host name without a scheme (adb-….azuredatabricks.net, jira.customer.ch): labels plus a known TLD
_TLDS = ("com|net|org|io|ch|de|at|li|fr|it|nl|be|lu|uk|eu|us|ca|se|no|dk|fi|pl|es|pt|cz|cloud|dev|app|ai|co|info|biz"
         "|tech|online|site|local|internal|corp|lan|intra")
_HOST = re.compile(rf"(?<![\w.@/\\-])(?:[A-Za-z0-9](?:[A-Za-z0-9-]{{0,61}}[A-Za-z0-9])?\.)+(?:{_TLDS})(?![\w-])", re.I)
_TOKEN = re.compile(r"\b[A-Za-z0-9_\-+/=]{32,}\b")
# words a company name shares with everyday text: never redacted on their own (the full name still is)
_COMMON_WORDS = frozenset({"bank", "group", "gmbh", "corp", "company", "holding", "the", "and", "services", "service",
                           "solutions", "systems", "partners", "international", "insurance", "consulting", "digital",
                           "data", "cloud", "software", "technologies", "global", "swiss", "schweiz", "germany",
                           "deutschland", "limited", "inc", "ltd"})
_NOT_ALNUM_BEFORE, _NOT_ALNUM_AFTER = r"(?<![A-Za-z0-9])", r"(?![A-Za-z0-9])"


def feedback_dir() -> Path:
    from orch.dashboard.launch import config_dir
    return config_dir() / "feedback"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _workspace_key(ws) -> str:
    """A stable id for the rate limit that does not reveal the workspace's path or name."""
    return hashlib.sha256(str(Path(ws.root).resolve()).encode("utf-8")).hexdigest()[:12] if ws else "none"


def _ticket_texts(ws) -> tuple[list[str], list[str]]:
    """(titles, longer body lines) of the workspace's tickets, to keep ticket text out of a report."""
    from orch.core import store
    titles, lines = [], []
    for e in store.scan(ws):
        title = str((e.meta or {}).get("title") or "").strip()
        if len(title) >= 4:
            titles.append(title)
        with contextlib.suppress(OSError, UnicodeDecodeError):
            for line in e.path.read_text(encoding="utf-8").splitlines():
                line = line.strip().lstrip("-*#> ").strip()
                if len(line) >= 24 and not line.startswith(("title:", "id:", "status:")):
                    lines.append(line)
    return titles, lines


def _name_pattern(name: str) -> str | None:
    """`name` as a regex in which "-", "_" and spaces are alike (claims-etl, claims_etl, claims etl), never inside
    a longer word."""
    parts = [re.escape(p) for p in re.split(r"[-_\s]+", name.strip()) if p]
    return _NOT_ALNUM_BEFORE + r"[-_\s]+".join(parts) + _NOT_ALNUM_AFTER if parts else None


def _names(cfg: dict) -> list[tuple[str, str]]:
    """(regex, placeholder) for the customer, its distinctive words, the repos and the tracker prefixes; longest
    first, so a full name is replaced before one of its words."""
    out = []
    customer = str(cfg.get("customer") or "")
    if (pat := _name_pattern(customer)) and len(customer.strip()) >= 3:
        out.append((pat, "<customer>"))
    for word in re.split(r"[-_\s]+", customer):
        if len(word) >= 4 and word.lower() not in _COMMON_WORDS:
            out.append((_NOT_ALNUM_BEFORE + re.escape(word) + _NOT_ALNUM_AFTER, "<customer>"))
    for repo in cfg.get("git", {}).get("repos") or {}:
        if (pat := _name_pattern(str(repo))) and len(str(repo)) >= 3:
            out.append((pat, "<repo>"))
    for t in cfg.get("external_trackers") or []:
        prefix = str(t.get("prefix") or "")
        if len(prefix) >= 2:
            out.append((_NOT_ALNUM_BEFORE + re.escape(prefix) + _NOT_ALNUM_AFTER, "<tracker>"))
    return sorted(out, key=lambda item: len(item[0]), reverse=True)


def redact(text: str, ws=None) -> str:
    """`text` without anything that identifies the workspace, its customer or its tickets, or any credential."""
    text = str(text or "")
    if ws is not None:
        titles, body = _ticket_texts(ws)
        for line in sorted(set(body), key=len, reverse=True):
            text = text.replace(line, "<ticket text>")
        for title in sorted(set(titles), key=len, reverse=True):
            text = re.sub(re.escape(title), "<ticket title>", text, flags=re.I)
    text = _JWT.sub("<redacted>", text)  # before URLs and hosts: a JWT's dots are not a host name
    text = _URL.sub("<url>", text)
    text = _EMAIL.sub("<email>", text)
    text = _SECRET_VALUE.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", text)
    text = _BEARER.sub("Bearer <redacted>", text)
    text = _SECRET_PREFIX.sub("<redacted>", text)
    if ws is not None:
        for base in {str(Path(ws.root)), str(Path(ws.root).resolve())}:
            text = text.replace(base, "<workspace>")
    home = str(Path.home())
    if len(home) > 1:
        text = text.replace(home, "~")
    text = re.sub(r"~[/\\][^\s'\"]+", "<path>", text)  # below the home folder: folder names can name customers
    text = _UNC_PATH.sub("<path>", text)
    text = _WIN_PATH.sub("<path>", text)
    text = _POSIX_PATH.sub("<path>", text)
    text = _HOST.sub("<host>", text)
    text = _TOKEN.sub("<redacted>", text)
    if ws is not None:
        cfg = ws.config
        for t in cfg.get("external_trackers") or []:
            with contextlib.suppress(re.error):
                text = re.sub(str(t.get("pattern") or "(?!)"), "<tracker-key>", text)
        text = re.sub(_NOT_ALNUM_BEFORE + re.escape(cfg["id"]["prefix"]) + r"-\d+" + _NOT_ALNUM_AFTER, "<ticket>", text)
        for pattern, label in _names(cfg):
            text = re.sub(pattern, label, text, flags=re.I)
    return text


def _cap(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + "…"


def _load(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and _ID.fullmatch(str(data.get("id", ""))) else None


def reports() -> list[dict]:
    """Every report on this machine, oldest first."""
    try:
        paths = sorted(feedback_dir().glob("fb-*.json"))
    except OSError:
        return []
    found = [r for r in (_load(p) for p in paths) if r is not None]
    return sorted(found, key=lambda r: r.get("created", ""))


def get(report_id: str) -> dict:
    if not _ID.fullmatch(report_id or ""):
        raise UsageError(f"no feedback report {report_id!r}", hint="ids look like fb-0123456789ab; see `orch feedback list`")
    found = _load(feedback_dir() / f"{report_id}.json")
    if found is None:
        raise NotFoundError(f"no feedback report {report_id}", hint="see `orch feedback list`")
    return found


def _save(report: dict) -> None:
    from orch.core.fsutil import atomic_write_text
    try:
        atomic_write_text(feedback_dir() / f"{report['id']}.json", json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    except OSError as e:
        raise OrchError(f"could not save the feedback report: {e}") from e


def add(ws, *, command: str, problem: str, error: str = "", harness: str | None = None) -> tuple[str, dict | None]:
    """("added" | "counted" | "limit" | "off", report). Never raises for the limit: the agent must carry on."""
    from filelock import FileLock

    from orch import __version__
    if ws is not None and not ws.config.get("feedback", {}).get("enabled", True):
        return "off", None
    if not str(problem or "").strip():
        raise UsageError("say what happened: -m \"expected …, got …\"")
    clean = {"command": _cap(redact(command, ws).strip(), MAX_COMMAND),
             "problem": _cap(redact(problem, ws).strip(), MAX_PROBLEM),
             "error": _cap(redact(error, ws).strip(), MAX_ERROR)}
    fingerprint = hashlib.sha256(f"{clean['command']}\n{clean['problem']}".encode("utf-8")).hexdigest()[:12]
    key, now = _workspace_key(ws), _now()
    feedback_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(feedback_dir(), 0o700)  # reports are the user's own notes: nobody else on the machine reads them
    with FileLock(str(feedback_dir() / ".lock"), timeout=10):
        existing = reports()
        same = next((r for r in existing if r.get("id") == f"fb-{fingerprint}"), None)
        if same is not None:
            same["count"] = int(same.get("count") or 1) + 1
            same["last"] = _stamp(now)
            _save(same)
            return "counted", same
        since = _stamp(now - timedelta(hours=24))
        if sum(1 for r in existing if r.get("workspace") == key and r.get("created", "") >= since) >= DAILY_LIMIT:
            return "limit", None
        report = {"id": f"fb-{fingerprint}", "created": _stamp(now), "last": _stamp(now), "count": 1,
                  "status": "open", "workspace": key, "orch_version": __version__,
                  "python": platform.python_version(), "platform": sys.platform, "harness": harness or "unknown",
                  **clean}
        _save(report)
        return "added", report


def title(report: dict) -> str:
    first = report.get("problem", "").splitlines()[0] if report.get("problem") else "orch feedback"
    return _cap(f"Agent feedback: {first}", 90)


def body(report: dict, note: str = "") -> str:
    parts = [f"Reported by an agent through `orch feedback` ({report.get('count', 1)} time(s), "
             f"first {report.get('created')}, last {report.get('last')}).", "",
             "## Command", "", "```", report.get("command") or "(not given)", "```", "",
             "## What happened", "", report.get("problem") or ""]
    if report.get("error"):
        parts += ["", "## Output", "", "```", report["error"], "```"]
    parts += ["", "## Environment", "",
              f"- orch {report.get('orch_version')}, Python {report.get('python')}, {report.get('platform')}",
              f"- harness: {report.get('harness')}"]
    if note.strip():
        parts += ["", "## Note from the user", "", note.strip()]
    return "\n".join(parts) + "\n"


def set_status(report: dict, status: str, url: str | None = None) -> None:
    report["status"] = status
    if url:
        report["filed_url"] = url
    _save(report)


def file_issue(report: dict, note: str = "", repo: str = REPO) -> str:
    """Create the issue with gh (the human's own login). Returns the issue URL gh prints."""
    if shutil.which("gh") is None:
        raise UsageError("filing needs the GitHub CLI (gh)", hint="install gh and run `gh auth login`, or copy the "
                                                                  "text above into a new issue by hand")
    fd, tmp = tempfile.mkstemp(prefix="orch-feedback-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body(report, note))
        r = subprocess.run(["gh", "issue", "create", "-R", repo, "--title", title(report), "--body-file", tmp],
                           capture_output=True, text=True, encoding="utf-8", check=False, timeout=120)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
    if r.returncode != 0:
        raise OrchError(f"gh issue create failed: {(r.stderr or r.stdout).strip()[:500]}")
    url = (r.stdout or "").strip().splitlines()[-1] if (r.stdout or "").strip() else ""
    set_status(report, "filed", url or None)
    return url
