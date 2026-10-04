"""Create page from ticket: writes decisions/<KEY>.md in the local wiki folder (never over a file that exists), with
the widgets of the ticket's Verification and Findings and the files they pin, and then commits exactly those files
when the wiki folder is inside a git repository. That commit is the one thing orch does on its own in git: the owner
asked for it for this action only (docs/widgets.md, "Widgets on a wiki page"; the addon README)."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from orch.errors import ValidationError

from .gitcmd import GitError, run_git
from .local import resolve_folder

KEY = re.compile(r"[A-Z][A-Z0-9]*-\d+")
FOLDER = "decisions"
FILES = "_files"  # the wiki folder's place for the files a page's widgets pin (core reads only this folder)
BODY = ("Summary", "Requirements")
WIDGET_SECTIONS = ("Verification", "Findings")  # Summary and Requirements are gated: widgets stand in neither


def page_id(key: str) -> str:
    return f"{FOLDER}/{key}"


def _one_line(text) -> str:
    return " ".join(str(text or "").split())


def _provenance(key: str, doc: dict, day: str) -> str:
    """Says where the text came from and that it is a copy: the ticket's Summary and Requirements may be agent-written
    and unapproved, and nothing on a wiki page is ever an approval. The requirements hash pins which text was
    copied; whether that text is approved is read from the ticket (its gates and the ledger), never from here."""
    gate = (doc.get("gates") or {}).get("requirements") or {}
    pinned = str(gate.get("hash") or "")
    pin = f" (requirements text {pinned[:19]})" if pinned.startswith("sha256:") else ""
    return (f"> Copied from ticket {key} on {day}{pin}. A copy, not a record of approval: whether this text is "
            f"approved is shown only on the ticket.\n")


def content(key: str, doc: dict, day: str, widgets: dict | None = None) -> str:
    title = _one_line(doc.get("title")) or key
    head = yaml.safe_dump({"title": f"{key} {title}", "source": key}, allow_unicode=True,
                          default_flow_style=False, sort_keys=False).rstrip()
    out = [f"---\n{head}\n---\n", f"# {key} {title}\n", _provenance(key, doc, day)]
    sections = doc.get("sections") if isinstance(doc.get("sections"), dict) else {}
    for name in BODY:
        text = str(sections.get(name) or "").strip()
        if text:
            out.append(f"## {name}\n\n{text}\n")
    shown = widgets or {}
    for name in WIDGET_SECTIONS:
        blocks = [text for section, text in shown.get("blocks", ()) if section == name]
        if blocks:
            out.append(f"## {name} widgets\n\n" + "\n\n".join(blocks) + "\n")
    if shown.get("skipped"):
        out.append("> Not copied (a file is missing or changed since the widget was written): "
                   + "; ".join(shown["skipped"]) + "\n")
    out.append("## Decisions\n\n")
    return "\n".join(out)


def _message(key: str, rel: str) -> str:
    return (f"{key} Add wiki page {rel} from ticket\n\n"
            f"What: New page {rel}, copied from ticket {key}, with the files its widgets pin.\n"
            "Why:  Created with Create page from ticket in Mission Control, which commits the new files.\n"
            "Risk: Adds files only; nothing else in the working tree or the index is committed.\n")


def _commit(ctx, root: Path, folder: Path, paths: list[Path], key: str) -> str:
    """Commit exactly `paths`, whatever else is staged. Says what happened; never raises."""
    rel = paths[0].relative_to(root).as_posix()
    try:
        inside = run_git(ctx, ["git", "-C", str(folder), "rev-parse", "--is-inside-work-tree"], ok=(0, 128))
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return f"Created {rel}. The wiki folder is not in a git repository, so nothing was committed."
    except GitError as e:
        return f"Created {rel}, not committed (git is not available: {e.message})."
    names = [p.relative_to(folder).as_posix() for p in paths]  # relative to the -C folder: no symlinked-path surprises
    try:
        run_git(ctx, ["git", "-C", str(folder), "add", "--", *names])
        run_git(ctx, ["git", "-C", str(folder), "commit", "--quiet", "--only", "-m", _message(key, rel), "--", *names])
        sha = run_git(ctx, ["git", "-C", str(folder), "rev-parse", "--short", "HEAD"]).stdout.strip()
        return f"Created {rel} and committed it ({sha}). Nothing else was committed."
    except GitError as e:
        try:
            run_git(ctx, ["git", "-C", str(folder), "reset", "--quiet", "--", *names])  # leave the index as it was
        except GitError:
            pass
        return (f"Created {rel}, but the commit failed ({e.message}). The page is written and not committed; "
                "commit it yourself.")


def create_page(ctx, key: str) -> str:
    """Writes the page and commits it; returns what happened, for the person. Raises ValidationError for a bad key, a
    folder outside the workspace, a link on the way or a page that exists (nothing is changed then)."""
    if not isinstance(key, str) or not KEY.fullmatch(key):
        raise ValidationError("that is not a ticket key; reload the page")
    if ctx.settings.get("provider") != "local":
        raise ValidationError("set the wiki provider to local in Workspace & addons first")
    folder, why = resolve_folder(ctx.root, ctx.settings)
    if folder is None:
        raise ValidationError(why)
    doc = ctx.document(key)  # raises for a ticket that does not exist
    if str(doc.get("id") or "").upper() != key.upper():
        raise ValidationError("that ticket does not exist; reload the page")
    widgets = ctx.ticket_widget_copy(key, WIDGET_SECTIONS)
    root = Path(ctx.root)
    dest = folder / FOLDER / f"{key}.md"
    made: list[Path] = []
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.parent.is_symlink() or not dest.parent.resolve().is_relative_to(folder.resolve()):
            raise ValidationError(f"{FOLDER} inside the wiki folder is a link; refusing to write there")
        files = folder / FILES
        if widgets["files"]:
            files.mkdir(exist_ok=True)
            if files.is_symlink() or not files.resolve().is_relative_to(folder.resolve()):
                raise ValidationError(f"{FILES} inside the wiki folder is a link; refusing to write there")
        for name, blob in widgets["files"].items():
            target = files / name
            try:
                with open(target, "xb") as f:  # exclusive: a link or a file there is never written through
                    f.write(blob)
                made.append(target)
            except FileExistsError:
                if target.is_symlink() or target.read_bytes() != blob:
                    raise ValidationError(f"{target.relative_to(root).as_posix()} exists with other content; "
                                          "nothing was changed") from None
        with open(dest, "x", encoding="utf-8") as f:  # exclusive: an existing file, or a link there, is never written
            f.write(content(key, doc, ctx.now().date().isoformat(), widgets))
        made.insert(0, dest)
    except FileExistsError:
        _undo(made)
        raise ValidationError(f"{dest.relative_to(root).as_posix()} exists already; nothing was changed") from None
    except ValidationError:
        _undo(made)
        raise
    except OSError as e:
        _undo(made)
        raise ValidationError(f"the page could not be written ({e.strerror or e})") from None
    return _commit(ctx, root, folder, made, key)


def _undo(made: list[Path]) -> None:
    for path in made:
        path.unlink(missing_ok=True)
