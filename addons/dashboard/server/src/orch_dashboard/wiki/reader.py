"""Flat, regular markdown blobs at HEAD. No working-tree or index reads."""

import os
import re
from datetime import UTC, datetime

from orch import gitbrowse, gitsafe

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


TOC_LINE_MAX = 200  # a longer line is not a heading worth a table-of-contents row (and never reaches clean())
TOC_MAX = 200


def toc(markdown: str) -> list[dict]:
    """``##`` and ``###`` headings outside code fences. No regular expression runs over page text (a hostile page must
    not be able to stall the server); lines and rows are capped."""
    out: list[dict] = []
    for line in headings(markdown):
        if len(out) >= TOC_MAX:
            break
        if len(line) > TOC_LINE_MAX or not line.startswith("##"):
            continue
        level = 3 if line.startswith("###") else 2
        if line[level : level + 1] == "#" or line[level : level + 1] not in (" ", "\t"):
            continue  # "####", or no space after the hashes
        text = line[level:].strip().rstrip("#").strip()
        text = text.replace("`", "").replace("*", "").replace("_", "")
        if text:
            out.append({"level": level, "text": clean(text, limits.TITLE_MAX)})
    return out


def entries(repo) -> tuple[dict[str, int], bool, str]:
    """``({slug: size}, cut, state)`` of the regular files of ``wiki/`` at HEAD. ``state``: "ok", "no_commits" (HEAD is
    unborn) or "no_folder" (no ``wiki/`` folder: absent, or a link, a file or a submodule)."""
    # `HEAD:wiki` must be a TREE: git resolves a submodule entry to the submodule's commit and `ls-tree` would list it
    kind = git._run(repo, "blob_type", "HEAD", "wiki", max_bytes=64, timeout=limits.LIST_SECONDS)
    result = git._run(repo, "tree", "HEAD", "wiki", max_bytes=1_000_000, timeout=limits.LIST_SECONDS)
    found: dict[str, int] = {}
    gone = kind.code != 0 or kind.out.strip() != b"tree"
    partial = result.cut or result.timed_out  # git was stopped by a cap: what it printed is the start of the list
    if gone or (result.code != 0 and not partial):
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
    return found, partial, "ok"


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
            except (ValueError, OverflowError):
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


def scan_set(found, slugs) -> tuple[tuple[str, ...], bool]:
    """The files a content search may read, as ``wiki/<slug>.md``: the listed pages that are small enough to be served,
    in slug order, while their sizes add up to at most SEARCH_SCAN_BYTES. ``cut`` is True when some were left out.
    Every content read (titles, search) greps EXACTLY this list, so the cap is the cap of what git reads."""
    names, total, cut = [], 0, False
    for slug in slugs:
        size = found[slug]
        if size > limits.PAGE_MAX_BYTES:
            continue
        if total + size > limits.SEARCH_SCAN_BYTES:
            cut = True
            continue
        total += size
        names.append(f"wiki/{slug}.md")
    return tuple(names), cut


def grep(repo, pattern: str, files: tuple[str, ...], timeout: float):
    """A committed-text search of exactly ``files`` (literal pathspecs); ``(rows, cut)`` with rows ``(slug, text)``
    in file order. Nothing outside ``files`` is read."""
    if not files:
        return [], False
    try:
        result = gitbrowse.run(
            repo.folder, "tree_grep", os.environ, operand=(pattern, "HEAD"), files=files, max_bytes=1_000_000,
            timeout=timeout,
        )  # fmt: skip
    except gitsafe.Unsafe as exc:
        if exc.reason == "invalid search text":
            raise fail("invalid.input", "Use plain search text.", exit=5) from None
        raise fail("observe.unavailable", exc.reason, exit=4) from None
    cut = result.cut or result.timed_out or result.code not in (0, 1)
    allowed = set(files)
    rows = []
    for path, _line, text in gitbrowse.parse_grep(result.out):
        name = path.partition(":")[2]
        if name in allowed:
            rows.append((name.removeprefix("wiki/").removesuffix(".md"), text))
    return rows, cut


def titles(repo, found, slugs) -> dict[str, str]:
    """Titles of the listed pages from ONE bounded search of the committed files (first line starting with "# ")."""
    files, _cut = scan_set(found, slugs)
    rows, _cut = grep(repo, "# ", files, limits.LIST_SECONDS)
    out: dict[str, str] = {}
    for slug, text in rows:
        if slug not in out and text.startswith("# ") and (t := title_of(text)):
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
    names = titles(repo, found, slugs) if slugs else {}
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
