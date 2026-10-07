"""Keep the agent harnesses (Claude Code, Codex, Copilot CLI, …) current alongside orch-core.

Each harness the human can start (launch.json's `harnesses`, by default claude, codex and copilot) is looked up on
PATH, and how it was installed is read from where its binary really lives: a Homebrew cask or formula, a global
npm / pnpm / bun package, or Claude Code's own native installer. That decides both where the newest version is
looked up and the one command that updates it (`brew upgrade --cask claude-code@latest`, `claude update`, …). The
command runs only when the human says so: `orch serve` / `orch update` at their terminal, or the About card.

Nothing here comes from the workspace: the harness list is the per-user launch.json, and the update command is
built from a fixed template per install kind, its one variable (the package name) taken from the binary's path and
checked against a strict pattern. Every command is an argv list, never a shell string."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from urllib.parse import quote

from orch.addons import userfiles

# The harnesses that are not npm packages under their own name: where their newest version is published.
NPM_PACKAGES = {"claude": "@anthropic-ai/claude-code", "codex": "@openai/codex", "copilot": "@github/copilot"}
LABELS = {"claude": "Claude Code", "codex": "Codex", "copilot": "Copilot CLI"}
_PACKAGE = re.compile(r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._@+-]*", re.IGNORECASE)
_SEMVER = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?")
_CASK = re.compile(r"/Caskroom/([^/]+)/")
_CELLAR = re.compile(r"/Cellar/([^/]+)/")
_NODE = re.compile(r"/node_modules/((?:@[^/]+/)?[^/]+)/")
CHECK_TIMEOUT = 20
APPLY_TIMEOUT = 600


@dataclass(frozen=True)
class Harness:
    name: str                # the harness name in launch.json (claude, codex, …)
    path: str                # the binary, symlinks resolved
    via: str                 # brew cask | brew | npm | pnpm | bun | native | unknown
    package: str | None      # the cask, formula or npm package
    installed: str | None
    latest: str | None
    command: list[str] | None  # what updates it; None when orch cannot tell
    line: str                # what was found, for the human to read

    @property
    def label(self) -> str:
        return LABELS.get(self.name, self.name)

    @property
    def has_update(self) -> bool:
        return bool(self.command and newer(self.latest, self.installed))

    @property
    def summary(self) -> str:
        return f"{self.label} {self.installed} → {self.latest}"

    @property
    def command_text(self) -> str:
        return " ".join(self.command or [])


def _version(text: str | None) -> tuple[int, ...] | None:
    m = _SEMVER.search(text or "")
    return tuple(int(n) for n in m.group(0).split("-")[0].split("+")[0].split(".")) if m else None


def newer(latest: str | None, installed: str | None) -> bool:
    a, b = _version(latest), _version(installed)
    return bool(a and b and a > b)


# -- seams the tests replace ---------------------------------------------------------------------------------------

def _which(cmd: str) -> str | None:
    return shutil.which(cmd)


def _realpath(path: str) -> str:
    return os.path.realpath(path)


def _run(argv: list[str], timeout: int = CHECK_TIMEOUT, env: dict | None = None) -> tuple[int, str]:
    """(exit code, stdout or else stderr). Never raises: a missing program or a timeout is a non-zero exit."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           check=False, env={**os.environ, **env} if env else None)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return r.returncode, (r.stdout.strip() or r.stderr.strip())


def _npm_latest(package: str) -> str | None:
    """The `latest` dist-tag of an npm package, straight from the registry (no npm needed)."""
    url = f"https://registry.npmjs.org/{quote(package, safe='@')}/latest"
    try:
        with urllib.request.urlopen(url, timeout=CHECK_TIMEOUT) as r:
            data = json.loads(r.read(1_000_000))
    except (OSError, ValueError):
        return None
    v = data.get("version") if isinstance(data, dict) else None
    return v if isinstance(v, str) and _version(v) else None


# -- finding and checking ------------------------------------------------------------------------------------------

def harness_bins() -> dict[str, str]:
    """{harness name: program} for every harness the human can start, from the per-user launch settings."""
    from orch.dashboard import launch
    try:
        harnesses = launch.load_settings()["harnesses"]
    except Exception:  # noqa: BLE001 - a broken launch.json must not stop the update check
        harnesses = launch.DEFAULT_HARNESSES
    return {name: argv[0] for name, argv in harnesses.items() if argv and argv[0]}


def _how(name: str, real: str) -> tuple[str, str | None, list[str] | None]:
    """(via, package, update command) from where the binary really lives."""
    p = real.replace("\\", "/")
    if m := _CASK.search(p):
        brew = _which("brew")
        return "brew cask", m.group(1), [brew, "upgrade", "--cask", m.group(1)] if brew else None
    if m := _CELLAR.search(p):
        brew = _which("brew")
        return "brew", m.group(1), [brew, "upgrade", m.group(1)] if brew else None
    if m := _NODE.search(p):
        pkg = m.group(1)
        for via, marker, argv in (("pnpm", "/pnpm/", ["add", "-g"]), ("bun", "/.bun/", ["add", "-g"]),
                                  ("npm", "", ["install", "-g"])):
            if marker in p:
                tool = _which(via)
                return via, pkg, [tool, *argv, f"{pkg}@latest"] if tool else None
    if "/claude/versions/" in p or "/.claude/local/" in p:  # Claude Code's native installer updates itself
        return "native", NPM_PACKAGES["claude"], [real, "update"]
    return "unknown", None, None


def check(name: str, program: str) -> Harness | None:
    """The harness `name` started as `program`, or None when it is not installed."""
    found = _which(program)
    if not found:
        return None
    real = _realpath(found)
    via, package, command = _how(name, real)
    if package is not None and not _PACKAGE.fullmatch(package):
        via, package, command = "unknown", None, None
    code, said = _run([found, "--version"])
    installed = _SEMVER.search(said).group(0) if code == 0 and _SEMVER.search(said) else None
    latest = None
    if via == "brew cask":
        latest = _brew_latest(["--cask", package], lambda d: (d.get("casks") or [{}])[0].get("version"))
    elif via == "brew":
        latest = _brew_latest(["--formula", package],
                              lambda d: ((d.get("formulae") or [{}])[0].get("versions") or {}).get("stable"))
    elif package:
        latest = _npm_latest(package)
    if installed is None and via == "brew cask":  # a cask keeps its version in its folder name
        m = _SEMVER.search(real.partition(f"/Caskroom/{package}/")[2].split("/")[0])
        installed = m.group(0) if m else None
    h = Harness(name, real, via, package, installed, latest, command, "")
    return Harness(**{**asdict(h), "line": _line(h)})


def _brew_latest(args: list[str], pick) -> str | None:
    brew = _which("brew")
    if not brew:
        return None
    # what brew already knows: a check must not spend a minute on `brew update` (the upgrade itself still runs it)
    code, said = _run([brew, "info", "--json=v2", *args], env={"HOMEBREW_NO_AUTO_UPDATE": "1"})
    if code:
        return None
    try:
        v = pick(json.loads(said))
    except (ValueError, AttributeError, IndexError, TypeError):
        return None
    return str(v).split(",")[0] if v else None


def _line(h: Harness) -> str:
    where = f"{h.via} {h.package}" if h.package and h.via != "native" else h.via
    if h.via == "unknown":
        return f"{h.installed or 'installed'} at {h.path}; orch cannot tell how it was installed, so update it yourself"
    if h.installed is None:
        return f"not checked: `{Path(h.path).name} --version` said no version ({where})"
    if h.latest is None:
        return f"{h.installed} ({where}); not checked: could not look up the newest version (offline?)"
    if h.command is None:
        return f"{h.installed} → {h.latest} available ({where}), but {h.via.split()[0]} is not on PATH to update it"
    if newer(h.latest, h.installed):
        return f"{h.installed} → {h.latest} available ({where}): {h.command_text}"
    return f"up to date ({h.installed}, {where})"


def check_all() -> list[Harness]:
    """Every installed harness, checked side by side (each check waits on the network or brew)."""
    bins = harness_bins()
    if not bins:
        return []
    with ThreadPoolExecutor(max_workers=min(4, len(bins))) as pool:
        found = list(pool.map(lambda kv: check(*kv), bins.items()))
    seen, out = set(), []
    for h in found:
        if h is not None and h.path not in seen:  # two names for one binary are one harness
            seen.add(h.path)
            out.append(h)
    save(out)
    return out


def apply(h: Harness) -> str:
    """Run the harness's update command; returns what happened, for the human to read. Never raises."""
    if not h.command:
        return f"{h.label}: orch cannot update it ({h.line})"
    code, said = _run(h.command, timeout=APPLY_TIMEOUT)
    if code:
        tail = said.splitlines()[-1] if said else f"exit {code}"
        return f"{h.label}: the update failed ({tail}); run it yourself: {h.command_text}"
    again = check(h.name, harness_bins().get(h.name) or h.path) or h
    remember(again)
    now = again.installed or h.latest
    return f"{h.label} updated {h.installed} → {now}. Restart its running sessions to use it."


# -- the last check, for the About card (a page render never runs a command or touches the network) -------------------

def _state_path() -> Path:
    return userfiles._config_dir() / "harness-update.json"


def save(harnesses: list[Harness]) -> None:
    userfiles.update_json(_state_path(), lambda d: (d.clear(), d.update(
        checked_at=time.time(), harnesses=[asdict(h) for h in harnesses])))


def remember(h: Harness) -> None:
    """Replace one harness in the saved check (or add it) with a fresh look at it."""
    def mutate(d: dict) -> None:
        old = [x for x in d.get("harnesses") or [] if isinstance(x, dict)]
        new = [asdict(h) if x.get("name") == h.name else x for x in old]
        d["harnesses"] = new if any(x.get("name") == h.name for x in old) else [*old, asdict(h)]
    userfiles.update_json(_state_path(), mutate)


def last() -> tuple[list[Harness], str | None]:
    """(harnesses as last checked, when) from the saved check; ([], None) before the first one."""
    d = userfiles.read_json_object(_state_path())
    names = {f.name for f in fields(Harness)}
    out = []
    for x in d.get("harnesses") or []:
        if isinstance(x, dict) and names <= x.keys():
            try:
                out.append(Harness(**{k: x[k] for k in names}))
            except TypeError:
                continue
    at = d.get("checked_at")
    when = time.strftime("%a %d %b %H:%M", time.localtime(float(at))) if isinstance(at, (int, float)) else None
    return out, when
