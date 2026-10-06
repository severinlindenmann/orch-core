"""Keep orch-core and its custom addons current without the human running five commands.

`orch serve` (and `orch update`) check, ask once, then apply: core first (pull the clone it was installed from, reinstall
the uv tool, re-exec), then each custom addon (apply, and trust it again when it asks for nothing new). Everything here
runs for the human at a terminal; the addon steps keep the human-only checks in `orch.addons.manage`."""
from __future__ import annotations

import json
import os
import re
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
    repo: Path | None     # the git clone the installed tool was built from (None for a remote install)
    package: Path | None  # the orch-core folder inside it
    behind: int           # commits on the upstream branch the installed build does not have
    tag: str | None = None  # remote install: the newer release tag to install
    remote: GitRemote | None = None

    @property
    def summary(self) -> str:
        if self.remote:
            return f"{self.remote.built} → {self.tag}"
        return f"{self.behind} new commit{'s' if self.behind != 1 else ''}"


@dataclass(frozen=True)
class GitRemote:
    """orch-core installed straight from a git URL at a release tag (`uv tool install "… @ git+<url>@<tag>"`)."""
    url: str
    subdirectory: str | None
    built: str  # the tag the tool was installed from, or v<version> when the install was not pinned to a tag


def _state_path() -> Path:
    return userfiles._config_dir() / "update.json"


def _state() -> dict:
    return userfiles.read_json_object(_state_path())


def _set_state(**fields) -> None:
    userfiles.update_json(_state_path(), lambda d: d.update(fields))


@dataclass(frozen=True)
class CoreStatus:
    update: CoreUpdate | None  # set when the installed build is behind its upstream branch
    line: str                  # what was found, after "orch-core: ", for the human to read
    checked: bool              # False when the check could not run; `line` then says why


def marketplace_clone(folder: Path) -> Path | None:
    """The orch-core folder in the Claude marketplace clone that a plugin-cache folder
    (.../plugins/cache/<marketplace>/orch-core/<version>) was copied from, when that clone is a git repository."""
    parts = folder.parts
    for i in range(len(parts) - 3):
        if parts[i:i + 2] == ("plugins", "cache") and parts[i + 3] == "orch-core":
            package = Path(*parts[:i + 1]) / "marketplaces" / parts[i + 2] / "plugins" / "orch-core"
            try:
                manage._git("rev-parse", "--show-toplevel", cwd=package, timeout=10)
            except OrchError:
                return None
            return package if (package / "pyproject.toml").is_file() else None
    return None


def _reinstall_hint(package: Path | None) -> str:
    clone = marketplace_clone(package) if package else None
    return f'uv tool install --force "{clone or "<clone of orch-core>/plugins/orch-core"}[dashboard]"'


def _version(tag: str) -> tuple[int, ...] | None:
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)", tag)
    return tuple(int(n) for n in m.group(1).split(".")) if m else None


def core_source() -> tuple[Path, Path] | GitRemote | str:
    """(clone, package folder) or the git remote the installed tool was built from, or why there is none to update from."""
    from importlib import metadata
    try:
        raw = metadata.distribution("orch-core").read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return "orch-core is not installed as a package"
    try:
        url = json.loads(raw or "{}")
    except ValueError:
        url = {}
    if not isinstance(url, dict) or not url.get("url"):
        return f"orch-core was not installed from a folder; reinstall it from a clone to get updates: {_reinstall_hint(None)}"
    vcs = url.get("vcs_info") or {}
    if vcs.get("vcs") == "git":
        rev = vcs.get("requested_revision") or ""
        return GitRemote(url["url"], url.get("subdirectory"),
                         rev if _version(rev) else f"v{metadata.version('orch-core')}")
    package = Path(unquote(urlparse(url["url"]).path))
    if (url.get("dir_info") or {}).get("editable"):
        return f"orch-core is an editable install from {package}; update that checkout with git pull"
    try:
        repo = Path(manage._git("rev-parse", "--show-toplevel", cwd=package, timeout=10).strip())
    except OrchError:
        return (f"orch-core was installed from {package}, which is not a git clone; reinstall it from a clone to get "
                f"updates: {_reinstall_hint(package)}")
    return repo, package


def _public_url(url: str) -> str:
    """A remote URL without any user or token in it."""
    p = urlparse(url)
    return p._replace(netloc=p.hostname + (f":{p.port}" if p.port else "")).geturl() if p.hostname else url


def _remote_status(r: GitRemote) -> CoreStatus:
    try:
        out = manage._git("ls-remote", "--tags", "--refs", r.url, "v*", timeout=30)
    except OrchError as e:
        return CoreStatus(None, f"not checked: could not list the tags of {_public_url(r.url)} (offline?): {e.message}", False)
    tags = [t for t in (line.rpartition("refs/tags/")[2] for line in out.splitlines()) if _version(t)]
    if not tags:
        return CoreStatus(None, f"not checked: {_public_url(r.url)} has no release tags", False)
    newest = max(tags, key=_version)
    if _version(newest) <= (_version(r.built) or ()):
        return CoreStatus(None, f"up to date ({r.built})", True)
    c = CoreUpdate(None, None, 0, newest, r)
    return CoreStatus(c, c.summary, True)


def core_where(repo: Path | GitRemote) -> str:
    if isinstance(repo, GitRemote):
        return f"{_public_url(repo.url)} (release tags)"
    """'<origin> (<branch>)' of the clone, for the line that says what is being checked."""
    try:
        origin = _public_url(manage._git("remote", "get-url", "origin", cwd=repo, timeout=10).strip())
    except OrchError:
        origin = str(repo)
    try:
        branch = manage._git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo, timeout=10).strip()
    except OrchError:
        return origin
    return f"{origin} ({branch})"


def core_status(src: tuple[Path, Path] | str | None = None) -> CoreStatus:
    src = core_source() if src is None else src
    if isinstance(src, str):
        return CoreStatus(None, f"not checked: {src}", False)
    if isinstance(src, GitRemote):
        return _remote_status(src)
    repo, package = src
    try:
        manage._git("fetch", "--quiet", cwd=repo, timeout=20)
    except OrchError as e:
        return CoreStatus(None, f"not checked: could not fetch {repo} (offline?): {e.message}", False)
    try:
        upstream = manage._git("rev-parse", "@{u}", cwd=repo, timeout=10).strip()
    except OrchError:
        return CoreStatus(None, f"not checked: the clone at {repo} has no upstream branch", False)
    try:
        head = manage._git("rev-parse", "HEAD", cwd=repo, timeout=10).strip()
        # The build is the commit recorded when we last installed it; before the first update, assume the clone's HEAD.
        built = _state().get("core_commit") or head
        try:
            behind = int(manage._git("rev-list", "--count", f"{built}..@{{u}}", cwd=repo, timeout=10).strip())
        except ValidationError:  # the recorded commit is gone: fall back to the clone's own position
            built = head
            behind = int(manage._git("rev-list", "--count", "HEAD..@{u}", cwd=repo, timeout=10).strip())
    except (OrchError, ValueError) as e:
        return CoreStatus(None, f"not checked: {getattr(e, 'message', e)}", False)
    if not behind:
        return CoreStatus(None, f"up to date (commit {built[:7]})", True)
    return CoreStatus(CoreUpdate(repo, package, behind),
                      f"{behind} new commit{'s' if behind != 1 else ''} ({built[:7]} → {upstream[:7]})", True)


def core_check() -> CoreUpdate | None:
    return core_status().update


def core_apply(c: CoreUpdate) -> str:
    """Pull and reinstall; returns what happened to the Claude plugin, for the human to read."""
    uv = shutil.which("uv")
    if c.remote:
        if uv is None:
            raise ValidationError("uv is not on PATH, so orch-core cannot be reinstalled")
        sub_ = f"#subdirectory={c.remote.subdirectory}" if c.remote.subdirectory else ""
        spec = f"orch-core[dashboard] @ git+{c.remote.url}@{c.tag}{sub_}"
        r = subprocess.run([uv, "tool", "install", "--force", "--reinstall", spec],
                           capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL, check=False)
        if r.returncode:
            tail = (r.stderr.strip().splitlines() or [f"exit {r.returncode}"])[-1]
            raise ValidationError(f"the orch-core reinstall failed: {tail}")
        return refresh_plugin()
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
    """Check, ask once, apply, saying what was checked and what was found. `force` (orch update) ignores the one-day
    snooze; serve passes False."""
    continuing = os.environ.pop(CONTINUE_ENV, None) == "1"
    if not (force or continuing or check_only):
        due = float(_state().get("next_check", 0))
        if time.time() < due:
            out(f"update check: next one {time.strftime('%a %H:%M', time.localtime(due))} (orch update checks now)")
            return
    core = None
    if continuing:  # orch-core was just updated and this is the restarted process: only the addons are left
        out("checking the custom addons for updates …")
    else:
        src = core_source()
        out(f"checking for updates: orch-core{' at ' + core_where(src if isinstance(src, GitRemote) else src[0]) if not isinstance(src, str) else ''} "
            "and the custom addons …")
        status = core_status(src)
        out(f"orch-core: {status.line}")
        core = status.update
    infos = manage.update_check(None)
    addons = [i for i in infos if i.has_update]
    if not infos:
        out("addons: no custom addons installed")
    else:
        out(f"addons: {len(infos)} checked, " + (f"{len(addons)} with updates" if addons else "up to date"))
    for i in infos:
        if not i.has_update and i.message != "up to date":
            out(f"  {i.name}: {i.message}")
    if not core and not addons:
        _set_state(next_check=time.time() + CHECK_EVERY)
        out("nothing to update" if force or check_only else "nothing to update; next check in a day (orch update checks now)")
        return
    names = ([f"orch-core ({core.summary})"] if core else []) + \
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
