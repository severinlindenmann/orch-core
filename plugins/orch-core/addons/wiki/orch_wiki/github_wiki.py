"""github-wiki: a pages provider that keeps a shallow clone of https://github.com/<owner>/<name>.wiki.git in the
addon's state folder (ruling R12), updated in the background with git through ctx.run, and parses its Markdown
pages. Read-only: it never pushes. Symlinks and anything outside the clone are never read."""
from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from orch.addons.api import Snapshot

from .gitcmd import GitError, run_git
from .markdown import parse_page
from .pages import page_item, write_index

REPO = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}")
DEPTH = "200"
MAX_PAGES = 500
MAX_PAGE_BYTES = 256 * 1024
MAX_REPOS = 5
LOG_FORMAT = "--format=%x1e%H%x1f%aI%x1f%an"
_ORIGIN = re.compile(r'\[remote "origin"\][^\[]*?\burl\s*=\s*(\S+)', re.S)
_GITHUB = re.compile(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
                     r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?")


def clone_dir(state_dir, owner: str, name: str) -> Path:
    return Path(state_dir) / "git" / owner / f"{name}.wiki"


def _read(path: Path, cap: int = 1_000_000) -> str | None:
    try:
        return path.read_text(encoding="utf-8")[:cap]
    except (OSError, UnicodeDecodeError):
        return None


def _config_dir(git: Path) -> Path | None:
    """The folder that holds the repo's config. .git is a folder, or, in a worktree or submodule, a file with
    `gitdir: <path>`. A worktree's gitdir has a commondir file that points to the main .git folder."""
    if git.is_dir():
        return git
    text = _read(git, 4096) if git.is_file() else None
    first = (text or "").splitlines()[0].strip() if (text or "").strip() else ""
    if not first.startswith("gitdir:") or not first.removeprefix("gitdir:").strip():
        return None
    gitdir = (git.parent / first.removeprefix("gitdir:").strip()).resolve()
    if not gitdir.is_dir():
        return None
    common = (_read(gitdir / "commondir", 4096) or "").strip()
    return (gitdir / common).resolve() if common else gitdir


def origin_repo(root) -> str | None:
    """owner/name of the harness repo's GitHub origin, from the git config (file reads only, no git call, so it
    is safe during a render). Works for a normal repo, a worktree and a submodule."""
    folder = _config_dir(Path(root) / ".git")
    text = _read(folder / "config") if folder else None
    if text is None:
        return None
    m = _ORIGIN.search(text)
    g = _GITHUB.fullmatch(m.group(1)) if m else None
    return f"{g.group(1)}/{g.group(2)}" if g else None


def wiki_repos(settings, root) -> list[str]:
    raw = [r.strip() for r in str((settings or {}).get("repos") or "").split(",") if r.strip()]
    if not raw:
        origin = origin_repo(root)
        raw = [origin] if origin else []
    out: list[str] = []
    for repo in raw:
        if REPO.fullmatch(repo) and ".." not in repo and repo not in out:
            out.append(repo)
    return out[:MAX_REPOS]


def parse_log(stdout: str) -> dict[str, tuple[str, str]]:
    """{path: (updated_at as UTC ISO, author)} from `git log --name-only` output, newest commit first."""
    out: dict[str, tuple[str, str]] = {}
    for record in stdout.split("\x1e"):
        lines = record.strip("\n").split("\n")
        if not lines or lines[0].count("\x1f") < 2:
            continue
        _sha, at, author = lines[0].split("\x1f")[:3]
        try:
            when = datetime.fromisoformat(at.strip()).astimezone(timezone.utc).isoformat()
        except ValueError:
            continue
        for name in lines[1:]:
            name = name.strip()
            if name and name not in out:
                out[name] = (when, author.strip())
    return out


def _not_found(scope: str) -> str:
    return f"no wiki at github.com/{scope}/wiki yet (create the first page on GitHub) or the repo name is wrong"


def _wiki_url(scope: str) -> str:
    """The only URL git is ever given: https, built from a checked owner/name, so it can never start with `-`."""
    url = f"https://github.com/{scope}.wiki.git"
    if not REPO.fullmatch(scope) or ".." in scope or not url.startswith("https://github.com/"):
        raise GitError("error", "not an owner/name GitHub repo")
    return url


class GitHubWikiPages:
    id = "github-wiki"
    kind = "pages"
    interval_s = 900

    def scopes(self, ctx) -> list[str]:
        if (ctx.settings.get("provider") or "github-wiki") != "github-wiki":
            return []
        return wiki_repos(ctx.settings, ctx.root)

    def fetch(self, ctx, scope, previous):
        now = ctx.now()
        if not REPO.fullmatch(scope or "") or ".." in scope:
            return Snapshot(self.id, scope, now, health="error", message="not an owner/name GitHub repo")
        owner, name = scope.split("/")
        dest = clone_dir(ctx.addon.state_dir, owner, name)
        try:
            self._sync(ctx, scope, dest)
            log = run_git(ctx, ["git", "-C", str(dest), "log", LOG_FORMAT, "--name-only", "-n", "500"]).stdout
        except GitError as e:
            message = _not_found(scope) if "not found" in e.message.lower() else e.message
            if e.health == "auth_required":
                message = f"git cannot read the wiki ({e.message}); run gh auth setup-git or sign in to GitHub in git"
            return Snapshot(self.id, scope, now, health=e.health, message=message)
        return self._snapshot(ctx, scope, dest, parse_log(log), now)

    def _sync(self, ctx, scope: str, dest: Path) -> None:
        url = _wiki_url(scope)
        if (dest / ".git").is_dir() and not dest.is_symlink():
            run_git(ctx, ["git", "-C", str(dest), "fetch", "--depth", DEPTH, "--no-tags", "--quiet", "origin"], timeout=120)
            run_git(ctx, ["git", "-C", str(dest), "reset", "--hard", "--quiet", "FETCH_HEAD"])
            return
        if dest.is_symlink():
            dest.unlink()
        elif dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        run_git(ctx, ["git", "clone", "--depth", DEPTH, "--no-tags", "--single-branch", "--quiet", "--",
                      url, str(dest)], timeout=120)

    def _pages(self, dest: Path) -> list[Path]:
        """Markdown files in the clone. A path is skipped when it, or any folder between it and the clone root,
        is a symlink, or when it resolves outside the clone (Review Focus 1)."""
        if not dest.is_dir() or dest.is_symlink():
            return []
        root = dest.resolve()
        out = []
        for path in sorted(dest.rglob("*.md")):
            rel = path.relative_to(dest)
            if ".git" in rel.parts:
                continue
            chain = [dest.joinpath(*rel.parts[: i + 1]) for i in range(len(rel.parts))]
            try:
                if any(p.is_symlink() for p in chain) or not path.is_file() or not path.resolve().is_relative_to(root):
                    continue
            except OSError:
                continue
            out.append(path)
        return out

    def _snapshot(self, ctx, scope: str, dest: Path, log: dict, now) -> Snapshot:
        config = ctx.addon.ws.config
        ident = config.get("id") or {}
        trackers = tuple(str(t["prefix"]) for t in config.get("external_trackers") or []
                         if isinstance(t, dict) and t.get("prefix"))
        oldest = min((when for when, _ in log.values()), default=now.isoformat())
        files = self._pages(dest)
        items, texts, titles, skipped = [], {}, {}, 0
        for path in files:
            if len(items) >= MAX_PAGES:
                break
            rel = path.relative_to(dest).as_posix()
            try:
                if path.stat().st_size > MAX_PAGE_BYTES:
                    skipped += 1
                    continue
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                skipped += 1
                continue
            parsed = parse_page(path.stem, raw, local_prefix=str(ident.get("prefix") or ""),
                                pad=int(ident.get("pad") or 4), tracker_prefixes=trackers)
            page_id = path.stem if path.stem not in texts else rel.removesuffix(".md")
            when, author = log.get(rel, (oldest, None))
            items.append(page_item(provider=self.id, space=scope, id=page_id, title=parsed.title,
                                   url=f"https://github.com/{scope}/wiki/{quote(path.stem)}", path=rel,
                                   updated_at=when, author=author, excerpt=parsed.excerpt, links=parsed.keys,
                                   documents=parsed.documents, file_links=parsed.file_links))
            texts[page_id] = parsed.text
            titles[page_id] = items[-1]["title"]
        write_index(ctx.addon.state_dir, self.id, scope, texts, titles)  # mentions are counted here, not in renders
        notes = []
        if len(files) > MAX_PAGES:
            notes.append(f"showing the first {MAX_PAGES} of {len(files)} pages")
        if skipped:
            notes.append(f"{skipped} page(s) over {MAX_PAGE_BYTES // 1024} KiB skipped")
        return Snapshot(self.id, scope, now, complete=not notes, message="; ".join(notes), items=tuple(items))
