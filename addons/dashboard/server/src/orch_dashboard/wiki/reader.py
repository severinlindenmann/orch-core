"""Flat, regular markdown blobs at HEAD. No working-tree or index reads."""

import re
from datetime import UTC, datetime

from orch import gitbrowse

from ..errors import fail
from ..explorer import describe, git, guard
from . import limits

SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


def validate(slug: str) -> str:
    if not SLUG.fullmatch(slug):
        raise fail("invalid.input", "Choose a valid page slug.", exit=5)
    return slug


def clean(value: str, maximum: int) -> str:
    return guard.label(describe.clean_line(value, maximum))[:maximum]


def headings(markdown: str):
    fence = False
    for line in markdown.split("\n"):
        if re.match(r"\s*```", line):
            fence = not fence
        if not fence:
            yield line


def title_of(line: str) -> str:
    return clean(line.removeprefix("# ").rstrip("\r").strip(), limits.TITLE_MAX)


def title(markdown: str, slug: str) -> str:
    """The first line that starts with "# " (the list takes it from one `git grep`, so the rule is the same line-based
    one here: no code-fence logic), else the slug."""
    return next((t for t in (title_of(x) for x in markdown.split("\n") if x.startswith("# ")) if t), slug)


def toc(markdown: str) -> list[dict]:
    out = []
    for line in headings(markdown):
        match = re.fullmatch(r"(#{2,3})\s+(.+?)\s*#*\s*", line)
        if match:
            out.append({"level": len(match[1]), "text": clean(re.sub(r"[`*_]", "", match[2]), limits.TITLE_MAX)})
    return out


def entries(repo) -> tuple[dict[str, int], bool, str]:
    """``({slug: size}, cut, state)`` of the regular files of ``wiki/`` at HEAD. ``state``: "ok", "no_commits" (HEAD is
    unborn) or "no_folder" (no ``wiki/`` folder: absent, or a link, a file or a submodule)."""
    # `HEAD:wiki` must be a TREE: git resolves a submodule entry to the submodule's commit and `ls-tree` would list it
    kind = git._run(repo, "blob_type", "HEAD", "wiki", max_bytes=64, timeout=limits.LIST_SECONDS)
    result = git._run(repo, "tree", "HEAD", "wiki", max_bytes=1_000_000, timeout=limits.LIST_SECONDS)
    found: dict[str, int] = {}
    if result.code != 0 or kind.code != 0 or kind.out.strip() != b"tree":
        head = git._run(repo, "dir_log", "1", "HEAD", "wiki", max_bytes=1024, timeout=limits.LIST_SECONDS)
        return found, False, "no_folder" if head.code == 0 else "no_commits"
    for mode, kind, _oid, size, name in gitbrowse.parse_tree(result.out):
        if (
            kind == "blob"
            and mode in ("100644", "100755")  # not 120000 (a link) and not 160000 (a submodule)
            and size is not None
            and name.endswith(".md")
            and SLUG.fullmatch(name[:-3])
            and guard.deny(("wiki", name)) is None
        ):
            found[name[:-3]] = size
    return found, result.cut, "ok"


def metadata(repo, slugs) -> dict:
    result = git._run(
        repo, "dir_log", str(limits.LOG_COMMITS), "HEAD", "wiki", max_bytes=1_000_000, timeout=limits.LIST_SECONDS
    )
    wanted = {"wiki/" + slug + ".md": slug for slug in slugs}
    out = {}
    if result.code == 0 or result.cut:
        for _sha, author, date, _subject, paths in gitbrowse.parse_dir_log(result.out):
            try:
                date = datetime.fromisoformat(clean(date, 100)).astimezone(UTC).isoformat().replace("+00:00", "Z")
            except ValueError:
                date = None
            for path in paths:
                if path in wanted:
                    out.setdefault(wanted[path], {"updated": date, "by": clean(author, 100)})
    return out


def content(repo, slug, size):
    if size > limits.PAGE_MAX_BYTES:
        return None, True
    result = git._run(
        repo,
        "blob",
        "HEAD",
        "wiki/" + validate(slug) + ".md",
        max_bytes=limits.PAGE_MAX_BYTES + 1,
        timeout=limits.LIST_SECONDS,
    )
    if b"\0" in result.out:
        raise fail("not_found", "That page does not exist.", exit=2)
    if result.cut or len(result.out) > limits.PAGE_MAX_BYTES:
        return None, True
    if result.code != 0:
        raise fail("not_found", "That page does not exist.", exit=2)
    return result.out.decode("utf-8", "replace").replace("\0", ""), False


def titles(repo, found) -> dict[str, str]:
    """Titles of the listed pages from ONE bounded search of the committed files (first line starting with "# ")."""
    result = git._run(repo, "tree_grep", "# ", "HEAD", "wiki", max_bytes=1_000_000, timeout=limits.LIST_SECONDS)
    out: dict[str, str] = {}
    if result.code in (0, 1):
        for path, _line, text in gitbrowse.parse_grep(result.out):
            name = path.partition(":")[2]
            slug = name.removeprefix("wiki/").removesuffix(".md")
            if name == f"wiki/{slug}.md" and slug in found and slug not in out and text.startswith("# "):
                if found[slug] <= limits.PAGE_MAX_BYTES and (t := title_of(text)):
                    out[slug] = t
    return out


def row(slug, name, large, meta):
    return {
        "slug": slug,
        "title": name,
        "updated": meta.get("updated"),
        "by": meta.get("by"),
        "tickets": [],
        "links": 0,
        "too_large": large,
    }


def list_pages(repo) -> dict:
    found, cut, state = entries(repo)
    slugs = sorted(found)[: limits.LIST_MAX]
    meta = metadata(repo, slugs) if slugs else {}
    names = titles(repo, found) if slugs else {}
    pages = [
        row(slug, names.get(slug, slug), found[slug] > limits.PAGE_MAX_BYTES, meta.get(slug, {})) for slug in slugs
    ]
    return {
        "state": state,
        "pages": pages,
        "total": len(found),
        "cut": cut or len(found) > limits.LIST_MAX,
        "sizes": {slug: found[slug] for slug in slugs},
    }


def get_page(repo, slug: str) -> dict:
    validate(slug)
    found, _cut, _state = entries(repo)
    if slug not in found:
        raise fail("not_found", "That page does not exist.", exit=2)
    markdown, large = content(repo, slug, found[slug])
    result = row(slug, title(markdown or "", slug), large, metadata(repo, [slug]).get(slug, {}))
    result.pop("links")
    return {**result, "markdown": markdown, "size": found[slug], "toc": toc(markdown or ""), "backlinks": []}
