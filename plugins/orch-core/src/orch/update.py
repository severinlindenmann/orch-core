"""Keep orch-core and its custom addons current without the human running five commands.

`orch serve` (and `orch update`) check, ask once, then apply: core first (pull the clone it was installed from, reinstall
the uv tool, re-exec), then each custom addon (apply, and trust it again when it asks for nothing new). Everything here
runs for the human at a terminal; the addon steps keep the human-only checks in `orch.addons.manage`."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

from orch.addons import manage, userfiles
from orch.errors import OrchError, ValidationError

CHECK_EVERY = 24 * 3600  # a snoozed or clean check is not repeated for a day
CONTINUE_ENV = "ORCH_UPDATE_CONTINUE"  # set across the re-exec after a core update: skip the question, do the addons


@dataclass(frozen=True)
class CoreUpdate:
    repo: Path       # the git clone the installed tool was built from
    package: Path    # the orch-core folder inside it
    behind: int      # commits on the upstream branch the installed build does not have


def _state_path() -> Path:
    return userfiles._config_dir() / "update.json"


def _state() -> dict:
    return userfiles.read_json_object(_state_path())


def _set_state(**fields) -> None:
    userfiles.update_json(_state_path(), lambda d: d.update(fields))


def core_source() -> tuple[Path, Path] | None:
    """(clone, package folder) the installed tool was built from, or None (a wheel, an editable dev checkout, no git)."""
    from importlib import metadata
    try:
        raw = metadata.distribution("orch-core").read_text("direct_url.json")
        url = json.loads(raw or "{}")
        if (url.get("dir_info") or {}).get("editable"):
            return None
        package = Path(unquote(urlparse(url["url"]).path))
        repo = Path(manage._git("rev-parse", "--show-toplevel", cwd=package, timeout=10).strip())
    except (metadata.PackageNotFoundError, KeyError, ValueError, OSError, OrchError):
        return None
    return repo, package


def core_check() -> CoreUpdate | None:
    src = core_source()
    if src is None:
        return None
    repo, package = src
    try:
        manage._git("fetch", "--quiet", cwd=repo, timeout=20)
        head = manage._git("rev-parse", "HEAD", cwd=repo, timeout=10).strip()
        # The build is the commit recorded when we last installed it; before the first update, assume the clone's HEAD.
        built = _state().get("core_commit") or head
        try:
            behind = int(manage._git("rev-list", "--count", f"{built}..@{{u}}", cwd=repo, timeout=10).strip())
        except ValidationError:  # the recorded commit is gone: fall back to the clone's own position
            behind = int(manage._git("rev-list", "--count", "HEAD..@{u}", cwd=repo, timeout=10).strip())
    except (OrchError, ValueError):
        return None  # offline, no upstream: say nothing rather than nag
    return CoreUpdate(repo, package, behind) if behind else None


def core_apply(c: CoreUpdate) -> str:
    """Pull and reinstall; returns what happened to the Claude plugin, for the human to read."""
    uv = shutil.which("uv")
    if uv is None:
        raise ValidationError("uv is not on PATH, so orch-core cannot be reinstalled",
                              hint=f"git -C {c.repo} pull --ff-only, then uv tool install --force --reinstall "
                                   f"'orch-core[dashboard] @ {c.package}'")
    manage._git("pull", "--ff-only", "--quiet", cwd=c.repo, timeout=120)
    r = subprocess.run([uv, "tool", "install", "--force", "--reinstall", f"orch-core[dashboard] @ {c.package}"],
                       capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL, check=False)
    if r.returncode:
        tail = (r.stderr.strip().splitlines() or [f"exit {r.returncode}"])[-1]
        raise ValidationError(f"the orch-core reinstall failed: {tail}")
    _set_state(core_commit=manage._git("rev-parse", "HEAD", cwd=c.repo, timeout=10).strip())
    return refresh_plugin()


PLUGIN = "orch-core@orch-core"


def refresh_plugin() -> str:
    """Bring the Claude plugin (hooks and skills) along with the tool. Its version string never changes, so Claude
    would otherwise keep serving the old cache. Best effort: the result is reported as it is, never raised."""
    claude = shutil.which("claude")
    if claude is None:
        return f"the Claude plugin was not refreshed (no claude CLI on PATH): claude plugin update {PLUGIN}"
    try:
        r = subprocess.run([claude, "plugin", "update", PLUGIN], capture_output=True, text=True, timeout=120,
                           stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"the Claude plugin was not refreshed ({e}): claude plugin update {PLUGIN}"
    said = (r.stdout.strip() or r.stderr.strip()).splitlines()
    tail = said[-1] if said else f"exit {r.returncode}"
    if r.returncode:
        return f"the Claude plugin was not refreshed: {tail}"
    return f"Claude plugin: {tail}. Restart Claude Code sessions to load it."


def needs_review(r: manage.TrustReview) -> bool:
    """A new version is trusted without a second look only when it was trusted before and asks for nothing new."""
    return r.old_version is None or any(r.added.values()) or r.api_change is not None or r.remote_humans_added


def apply_addon(name: str, actor=None) -> tuple[str, manage.Manifest, manage.TrustReview, bool]:
    """Apply an addon update and trust it again when it asks for nothing new (the contract suite runs; the enabled
    switch is untouched). Returns (old version, new manifest, review, trusted). When not trusted the caller shows
    the review and, if the human agrees, calls `manage.trust_addon(name, seen_digest=review.digest)`."""
    before = str(userfiles.registry_entries().get(name, {}).get("version", "?"))
    m = manage.update_apply(name, actor=actor)
    r = manage.review(name)
    if needs_review(r):
        return before, m, r, False
    manage.trust_addon(name, seen_digest=r.digest, actor=actor)
    return before, m, r, True


def addon_apply(name: str, review_text: Callable[[manage.TrustReview], str], confirm: Callable[[str], bool],
                out: Callable[[str], None]) -> None:
    before, m, r, trusted = apply_addon(name)
    if not trusted:
        out(review_text(r))
        if not confirm(name):
            out(f"{name} {before} → {m.version} is installed but stays off until you run: orch addon trust {name}")
            return
        manage.trust_addon(name, seen_digest=r.digest)
    out(f"updated {name} {before} → {m.version}")


def run(*, check_only: bool, ask: Callable[[str], str], review_text: Callable[[manage.TrustReview], str],
        confirm: Callable[[str], bool], out: Callable[[str], None], force: bool = False) -> None:
    """Check, ask once, apply. `force` (orch update) ignores the one-day snooze; serve passes False."""
    continuing = os.environ.pop(CONTINUE_ENV, None) == "1"
    if not (force or continuing or check_only) and time.time() < float(_state().get("next_check", 0)):
        return
    core = None if continuing else core_check()
    addons = [i for i in manage.update_check(None) if i.has_update]
    if not core and not addons:
        _set_state(next_check=time.time() + CHECK_EVERY)
        if check_only:
            out("orch-core and the custom addons are up to date")
        return
    names = ([f"orch-core ({core.behind} new commit{'s' if core.behind != 1 else ''})"] if core else []) + \
            [f"{i.name} ({i.message})" for i in addons]
    if check_only:
        out("updates available: " + ", ".join(names))
        return
    if not continuing and ask(f"Updates available: {', '.join(names)}.\nUpdate now? [Y/n] ").strip().lower() in ("n", "no"):
        _set_state(next_check=time.time() + CHECK_EVERY)
        out("not now; asking again tomorrow (orch update runs it any time)")
        return
    if core:
        out("updating orch-core …")
        out(core_apply(core))
        os.environ[CONTINUE_ENV] = "1"
        out("orch-core updated; restarting")
        os.execv(shutil.which("orch") or sys.argv[0], ["orch", *sys.argv[1:]])
    for i in addons:
        try:
            addon_apply(i.name, review_text, confirm, out)
        except OrchError as e:  # one broken addon must not stop the others, or serve
            out(f"{i.name}: update failed: {e.message}")
