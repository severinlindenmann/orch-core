"""Create page from ticket: writes decisions/<KEY>.md in the local wiki folder (never over a file that exists). It
does not commit: orch never commits anything itself (#36); the page is yours to review and commit."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from orch.errors import ValidationError

from .local import resolve_folder

KEY = re.compile(r"[A-Z][A-Z0-9]*-\d+")
FOLDER = "decisions"
BODY = ("Summary", "Requirements")


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


def content(key: str, doc: dict, day: str) -> str:
    title = _one_line(doc.get("title")) or key
    head = yaml.safe_dump({"title": f"{key} {title}", "source": key}, allow_unicode=True,
                          default_flow_style=False, sort_keys=False).rstrip()
    out = [f"---\n{head}\n---\n", f"# {key} {title}\n", _provenance(key, doc, day)]
    sections = doc.get("sections") if isinstance(doc.get("sections"), dict) else {}
    for name in BODY:
        text = str(sections.get(name) or "").strip()
        if text:
            out.append(f"## {name}\n\n{text}\n")
    out.append("## Decisions\n\n")
    return "\n".join(out)


def create_page(ctx, key: str) -> str:
    """The workspace-relative path of the new page. Raises ValidationError for a bad key, a folder outside the
    workspace, a link on the way or a page that exists."""
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
    root = Path(ctx.root)
    dest = folder / FOLDER / f"{key}.md"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.parent.is_symlink() or not dest.parent.resolve().is_relative_to(folder.resolve()):
            raise ValidationError(f"{FOLDER} inside the wiki folder is a link; refusing to write there")
        with open(dest, "x", encoding="utf-8") as f:  # exclusive: an existing file, or a link there, is never written
            f.write(content(key, doc, ctx.now().date().isoformat()))
    except FileExistsError:
        raise ValidationError(f"{dest.relative_to(root).as_posix()} exists already; nothing was changed") from None
    except OSError as e:
        raise ValidationError(f"the page could not be written ({e.strerror or e})") from None
    return dest.relative_to(root).as_posix()
