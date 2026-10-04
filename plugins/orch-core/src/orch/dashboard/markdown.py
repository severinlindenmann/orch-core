from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import escape

from pathlib import Path

from markdown_it.renderer import RendererHTML

from orch.core.artifacts import _SHA, _parser, resolve_src
from orch.textsafe import badge_html, has_hidden

# One parser for rendering and for what a gate or verdict hash binds (orch.core.artifacts.binding): only render rules
# are added here, and an artifact is shown only when the binding reads it from the same tokens.
_md = _parser()
# `md-task`, not `task`: tasks.css styles `.task` as the Tasks card's two-column grid.
_TASKS = (
    ("<li>[ ] ", '<li class="md-task">☐ '), ("<li>[x] ", '<li class="md-task done">☑ '), ("<li>[X] ", '<li class="md-task done">☑ '),
    ("<li><p>[ ] ", '<li class="md-task"><p>☐ '), ("<li><p>[x] ", '<li class="md-task done"><p>☑ '),
    ("<li><p>[X] ", '<li class="md-task done"><p>☑ '),
)


@dataclass(frozen=True)
class ArtifactScope:
    """What `artifact:<name>` resolves to while one ticket's text is rendered: its id and its linked files."""
    ticket: str
    files: dict = field(default_factory=dict)  # name -> artifact entry
    base: Path | None = None  # artifacts/<ticket>/ on disk: an inline image's size is read there, never from the file


def artifact_scope(ticket, ws=None) -> ArtifactScope | None:
    if ticket is None or not getattr(ticket, "meta", None):
        return None
    from orch.core.artifacts import entries
    return ArtifactScope(str(ticket.id), {e["name"]: e for e in entries(ticket) if e.get("name")},
                         ws.artifacts_dir / str(ticket.id) if ws is not None else None)


def inline_size_ok(scope: ArtifactScope, name: str) -> bool:
    """The file is small enough to show inline, by its size on disk (the frontmatter size is agent-written)."""
    from orch.core.artifacts import INLINE_MAX_BYTES
    if scope.base is None:
        return False
    try:
        return (scope.base / name).stat().st_size <= INLINE_MAX_BYTES
    except OSError:
        return False


def artifact_url(scope: ArtifactScope, name: str, entry: dict) -> str:
    """`/a/<ticket>/<name>?v=<first 16 hex of its sha256>`: the artifact route refuses the file when its bytes no
    longer match, so an inline image is always the one the ticket links (and an approval bound)."""
    from urllib.parse import quote
    return f"/a/{quote(scope.ticket)}/{quote(name)}?v={str(entry['sha256'])[:16]}"


def _servable(entry) -> bool:
    """A linked file is shown or linked only with a well-formed sha256: every artifact URL carries ?v=<sha>."""
    return entry is not None and isinstance(entry.get("sha256"), str) and bool(_SHA.match(entry["sha256"]))


_TICKET_ID = re.compile(r"^[A-Z][A-Z0-9]*-[0-9]+$")


def canonical_link(src: str, scope) -> tuple[str, str]:
    """R24: what a non-bound href or src in ticket Markdown may be, decided once on its canonical form (decoded once,
    dot segments resolved, scheme and host split off): ("other", "/a/<ID>/<name>") for another ticket's artifact,
    ("live", src) for an http(s) or mailto URL or a clean root-relative path whose path is not under /a/, ("fragment",
    src) for an in-page #link, else ("inert", ""). Relative paths, other schemes, backslashes, dot segments in a
    root-relative path and any /a/ path that is not another ticket's canonical artifact are inert."""
    from urllib.parse import unquote, urlsplit
    s = str(src or "").strip()
    if s.startswith("#") and "\\" not in s:
        return ("fragment", s)
    if not s or "\\" in s or any(c.isspace() for c in s):
        return ("inert", "")
    # Browsers skip every run of "/" or "\" after the scheme: only "//host" (exactly two) is scheme-relative and goes
    # through the absolute rule; a root-relative path must start with exactly one "/".
    if s.startswith("/") and s[1:2] in ("/", "\\") and not (s.startswith("//") and s[2:3] not in ("/", "\\")):
        return ("inert", "")
    try:
        parts = urlsplit(s)
    except ValueError:
        return ("inert", "")
    scheme = parts.scheme.lower()
    if scheme == "mailto":
        return ("live", s)
    if scheme not in ("", "http", "https"):
        return ("inert", "")
    absolute = bool(scheme or parts.netloc)
    path = unquote(parts.path)
    if "\\" in path:
        return ("inert", "")
    raw = path.split("/")
    dots = any(seg in (".", "..") for seg in raw)
    stack: list[str] = []
    for seg in raw:
        if seg == "..":
            if stack:
                stack.pop()
        elif seg not in ("", "."):
            stack.append(seg)
    if not absolute and not path.startswith("/"):
        # relative: only a legacy artifact reference to another ticket, `(./|../)*artifacts/<ID>/<name>`
        lead = list(raw)
        while lead and lead[0] in (".", ".."):
            lead.pop(0)
        if len(lead) == 3 and lead[0] == "artifacts" and not parts.query and not parts.fragment:
            return _other(lead[1], lead[2], scope)
        return ("inert", "")
    under_a = bool(stack) and stack[0].lower() == "a"
    if absolute:  # same-origin and scheme-relative hosts included: any path under /a/ is inert (the host is unknown)
        return ("inert", "") if under_a or not parts.netloc else ("live", s)
    if dots:
        return ("inert", "")
    if under_a:
        if len(raw) == 4 and raw[0] == "" and raw[1] == "a" and not parts.query and not parts.fragment:
            return _other(raw[2], raw[3], scope)
        return ("inert", "")
    return ("live", s)


def _other(ticket: str, name: str, scope) -> tuple[str, str]:
    from urllib.parse import quote
    from orch.core.artifacts import safe_name
    if (not _TICKET_ID.match(ticket) or (scope is not None and ticket.upper() == scope.ticket.upper())
            or "/" in name or not safe_name(name)):
        return ("inert", "")
    return ("other", f"/a/{quote(ticket)}/{quote(name)}")


@dataclass(frozen=True)
class PageScope:
    """What a relative link in an addon page (a wiki page) may point at: the addon's own page route and the page ids
    its provider listed (already inside its folder, no symlink on the way), and the id of the page being shown."""
    addon: str
    here: str
    pages: frozenset = frozenset()


_PAGE_ID = re.compile(r"^[^/\\\x00-\x1f]+(?:/[^/\\\x00-\x1f]+)*$")


def page_link(src: str, scope: PageScope | None) -> str | None:
    """The live URL of another page for a relative href in page text, or None (then canonical_link decides). Decided
    on R24's canonical form: decoded once, dot segments resolved against the shown page's folder; a scheme, host, query,
    backslash, any leading `/` (so `//`, `/\\` and `///` too), a dot segment that would leave the folder, or a target
    that is not a listed page gives None. `other.md`, `sub/other`, `./other.md#part` and `../other.md` within the
    folder become `/addons/<addon>/?page=<id>` (plus the #fragment)."""
    from urllib.parse import quote, unquote, urlsplit
    if scope is None or not scope.pages:
        return None
    s = str(src or "").strip()
    if not s or s.startswith(("/", "#")) or "\\" in s or any(c.isspace() for c in s):
        return None
    try:
        parts = urlsplit(s)
    except ValueError:
        return None
    if parts.scheme or parts.netloc or parts.query:
        return None
    path = unquote(parts.path)  # once, as canonical_link does
    if not path or path.startswith("/") or "\\" in path or "\x00" in path:
        return None
    stack = scope.here.split("/")[:-1]  # the shown page's folder inside the wiki folder
    for seg in path.split("/"):
        if seg == "..":
            if not stack:
                return None  # would leave the wiki folder
            stack.pop()
        elif seg not in ("", "."):
            stack.append(seg)
    if not stack:
        return None
    pid = "/".join(stack)
    pid = pid[:-3] if pid.lower().endswith(".md") else pid
    if pid not in scope.pages or not _PAGE_ID.match(pid):
        return None
    url = f"/addons/{quote(scope.addon, safe='')}/?page={quote(pid, safe='')}"
    if parts.fragment:
        url += "#" + quote(unquote(parts.fragment), safe="-_.~")
    return url


def _resolve(env, src: str):
    """(scope, name, entry) for a src or href that points at an artifact of the scope's ticket and that the binding
    read from these tokens (env["bound"]); entry None when the ticket does not link it. A bare `artifact:` without a
    ticket gives (None, name, None). None for anything else."""
    from orch.core.artifacts import safe_name
    scope = (env or {}).get("artifacts")
    if scope is None:
        return (None, src[len("artifact:"):], None) if src.startswith("artifact:") else None
    name = resolve_src(src, scope.ticket)
    if name is None or name not in (env or {}).get("bound", ()):
        return None
    return scope, name, (scope.files.get(name) if safe_name(name) else None)


def _image(self, tokens, idx, options, env):
    """Images: only a ticket artifact the ticket links becomes an <img> (lazy, inside a link to the full file); a web
    image is a link (never loaded), anything else its alt text."""
    from orch.core.artifacts import is_image
    token = tokens[idx]
    src = token.attrGet("src") or ""
    alt = self.renderInlineAsText(token.children or [], options, env)
    hit = _resolve(env, src)
    if hit is not None:
        scope, name, entry = hit
        if not _servable(entry):
            return f'<span class="md-artifact-missing">{escape(alt or name)} <span class="muted">(artifact {escape(name)} is not linked)</span></span>'
        url = escape(artifact_url(scope, name, entry))
        if is_image(name) and inline_size_ok(scope, name):
            return (f'<a class="md-artifact" href="{url}" target="_blank" rel="noopener">'
                    f'<img src="{url}" alt="{escape(alt or name)}" loading="lazy" decoding="async"></a>')
        return f'<a class="md-artifact" href="{url}" target="_blank" rel="noopener">{escape(alt or name)}</a>'
    kind, url = canonical_link(src, (env or {}).get("artifacts"))
    if kind == "other" and (env or {}).get("page"):  # addon page text: no ticket's file, bound by no gate
        return escape(alt)
    if kind == "other":  # another ticket's file: a link, never an image
        return f'<a href="{escape(url)}" target="_blank" rel="noopener">{escape(alt or url)}</a>'
    if kind == "live" and src.lower().startswith(("http://", "https://")):  # a web image is never loaded
        return f'<a href="{escape(src)}" target="_blank" rel="noopener noreferrer">{escape(alt or src)} ↗</a>'
    return escape(alt)


def _link_open(self, tokens, idx, options, env):
    token = tokens[idx]
    href = token.attrGet("href") or ""
    hit = _resolve(env, href)
    scope = (env or {}).get("artifacts")
    if hit is not None:
        _, name, entry = hit
        if hit[0] is None or not _servable(entry):
            return _inert(self, tokens, idx, options, env, name)
        token.attrSet("href", artifact_url(scope, name, entry))
        token.attrSet("target", "_blank")
        token.attrSet("rel", "noopener")
        return self.renderToken(tokens, idx, options, env)
    target = page_link(href, (env or {}).get("pages")) if (env or {}).get("page") else None
    if target is not None:  # another listed page of this addon's folder
        token.attrSet("href", target)
        return self.renderToken(tokens, idx, options, env)
    kind, url = canonical_link(href, scope)
    if kind == "inert" or (kind == "other" and (env or {}).get("page")):
        return _inert(self, tokens, idx, options, env, "")
    if kind == "other":
        token.attrSet("href", url)
    return self.renderToken(tokens, idx, options, env)


def _inert(self, tokens, idx, options, env, name: str) -> str:
    """A link to an artifact that is not linked (or not bound): its text, never a live href."""
    token = tokens[idx]
    token.attrs.pop("href", None)
    token.attrSet("class", "md-artifact-missing")
    token.attrSet("title", f"artifact {name} is not linked" if name else
                  "not a page of this wiki or a web link" if (env or {}).get("page") else "artifact is not linked")
    return self.renderToken(tokens, idx, options, env).replace("<a ", "<span ", 1)


def _link_close(self, tokens, idx, options, env):
    opener = next((t for t in reversed(tokens[:idx]) if t.type == "link_open"), None)
    if opener is not None and opener.attrGet("class") == "md-artifact-missing" and not opener.attrGet("href"):
        return "</span>"
    return self.renderToken(tokens, idx, options, env)


def _env(tokens, scope) -> dict:
    """The render env: the scope and the artifact names the binding reads from these very tokens."""
    import orch.core.artifacts as art  # looked up at call time: the binding function is the one source of truth
    bound = {name for _, name, _ in art.refs_in_tokens(tokens, scope.ticket)} if scope is not None else set()
    return {"artifacts": scope, "bound": bound}


def _fence(self, tokens, idx, options, env):
    """A top-level ```orch fence becomes its widget when the caller passed the ticket's widgets in `env`
    (`render_section`); everywhere else, and for every other fence, the usual code block. A render rule only: the
    tokens (and so what orch.core.artifacts.binding reads) are the same; html=False still holds, since the widget
    HTML is orch's own, built from validated data."""
    tok = tokens[idx]
    widgets = env.get("widgets") if isinstance(env, dict) else None
    if widgets is not None and tok.level == 0 and tok.info.strip() == "orch":
        return widgets.html(env.get("section", ""), tok.content.removesuffix("\n")) + "\n"
    return RendererHTML.fence(self, tokens, idx, options, env)


_md.add_render_rule("fence", _fence)
_md.add_render_rule("image", _image)
_md.add_render_rule("link_open", _link_open)
_md.add_render_rule("link_close", _link_close)


def render_markdown(text: str | None, scope: ArtifactScope | None = None, *, widgets=None, section: str = "") -> str:
    """Markdown as HTML. With `scope` (artifact_scope(ticket, ws)), an image of a linked artifact shows inline and a
    link to one opens its file; which ones is decided by orch.core.artifacts.refs_in_tokens on the same tokens. With
    `widgets` (a SectionWidgets), each top-level ```orch fence of `section` is drawn as its widget."""
    tokens = _md.parse(text or "", {})
    env = _env(tokens, scope)
    if widgets is not None:
        env.update(widgets=widgets, section=section)
    html = _md.renderer.render(tokens, _md.options, env)
    if has_hidden(html):  # checked on the output: Markdown decodes &#x202E;, &shy; and %-encoded autolinks
        html = badge_html(html)  # hidden characters (bidi, zero-width, controls) shown as badges, never raw
    for plain, task in _TASKS:
        html = html.replace(plain, task)
    return html


def render_page_markdown(text: str | None, pages: PageScope | None = None) -> str:
    """The addon Markdown widget (a wiki page): the same parser, sanitiser and R24 link rules as ticket text, but
    never with an artifact scope and with no live link to any ticket's artifact (`/a/...`, `artifacts/...`,
    `artifact:`): no gate hash covers page text, so a page can never point at a file as if a ticket had bound it.
    With `pages`, a relative link to another listed page becomes a link to it (page_link); anything else is R24's."""
    tokens = _md.parse(text or "", {})
    html = _md.renderer.render(tokens, _md.options, {"artifacts": None, "bound": set(), "page": True,
                                                      "pages": pages})
    if has_hidden(html):
        html = badge_html(html)
    for plain, task in _TASKS:
        html = html.replace(plain, task)
    return html


def pinned_images(text: str | None, scope: ArtifactScope | None, limit: int = 3) -> list[dict]:
    """M: the inline images `render_markdown` draws for `text`, as [{href, alt, name}] in document order (at most
    `limit`), for thumbnails beside a gated text or a criterion. The same parser, tokens and binding decide it, so a
    thumbnail is always a bound, version-pinned `/a/…?v=` image the full text shows; never a URL built by hand."""
    import orch.core.artifacts as art
    if scope is None or not text:
        return []
    tokens = _md.parse(text, {})
    bound = _env(tokens, scope)["bound"]
    out: list[dict] = []
    for kind, name, alt in art.refs_in_tokens(tokens, scope.ticket):
        if kind != "image" or name not in bound or any(o["name"] == name for o in out):
            continue
        entry = scope.files.get(name) if art.safe_name(name) else None
        if not _servable(entry) or not art.is_image(name) or not inline_size_ok(scope, name):
            continue
        out.append({"href": artifact_url(scope, name, entry), "alt": alt or name, "name": name})
        if len(out) >= limit:
            break
    return out


def render_inline(text: str | None, scope: ArtifactScope | None = None) -> str:
    """One line of Markdown (code, emphasis, links) without a wrapping paragraph."""
    tokens = _md.parseInline(text or "", {})
    html = _md.renderer.render(tokens, _md.options, _env(tokens, scope))
    return badge_html(html) if has_hidden(html) else html


class SectionWidgets:
    """The ticket's widgets for one page render: `render_section` asks it for a fence's HTML by section and raw
    text, which finds the block parsed from the file (so its line number is the file's)."""

    def __init__(self, ctx, blocks, *, assets: bool = False):
        """assets=True: the page does not load the widget CSS and host script in its head (Today, the decision
        cards, the board's flow); they are linked before the first widget drawn, so a page without widgets pays
        nothing."""
        from orch.widgets.blocks import duplicate_ids
        self.ctx, self.blocks, self.drawn, self.assets = ctx, blocks, 0, assets
        self.duplicates = duplicate_ids(blocks)

    def _assets(self) -> str:
        from orch.dashboard.assets import static_url
        from orch.widgets.render import css_names
        self.assets = False
        return ("".join(f'<link rel="stylesheet" href="{escape(static_url(css))}">' for css in css_names())
                + f'<script src="{escape(static_url("widgets/orch-frames.js"))}" defer></script>')

    def html(self, section: str, raw: str) -> str:
        """The block drawn, or after MAX_BLOCKS of them on this page, shown as code with a note (no more frames)."""
        from orch.widgets import render_html
        from orch.widgets.blocks import MAX_BLOCKS, make_block
        from orch.widgets.render import _as_code
        block = next((b for b in self.blocks if b.section == section and b.raw == raw), None) or make_block(section, 0, raw)
        self.drawn += 1
        if self.drawn > MAX_BLOCKS:
            return _as_code(block, "warn", "Shown as code", f"only the first {MAX_BLOCKS} widgets of a ticket are drawn")
        wid = (block.data or {}).get("id") if isinstance(block.data, dict) else None
        if wid in self.duplicates:
            return _as_code(block, "err", "Widget not shown", f"id {wid!r} is used twice in this ticket")
        return (self._assets() if self.assets else "") + str(render_html(block, self.ctx))


def section_widgets(ws, ticket, raw: str | None = None, *, assets: bool = False) -> SectionWidgets:
    """The widgets of one ticket for `section_md`: the ticket page, the decision cards and the epic verdict all draw
    them through this, with the same checks (placement, schema, pins) and the same frames."""
    from orch.widgets import Ctx, ticket_blocks
    return SectionWidgets(Ctx.of(ws, ticket), ticket_blocks(ticket, raw), assets=assets)


def render_section(text: str | None, section: str, widgets: SectionWidgets | None = None,
                   scope: ArtifactScope | None = None) -> str:
    """Markdown of one ticket section, with its ```orch blocks drawn as widgets (or as code with a warning where
    widgets may not stand). Without `widgets`, the same as `render_markdown`."""
    use = isinstance(widgets, SectionWidgets)  # a template rendered without them passes Undefined
    return render_markdown(text, scope, widgets=widgets if use else None, section=section)


def md_page_filter(text, addon="", here="", pages=()) -> str:
    """The `md_page` template filter for the addon Markdown widget: relative links reach the addon's listed pages."""
    ids = frozenset(p for p in pages or () if isinstance(p, str))
    return render_page_markdown(text, PageScope(str(addon), str(here or ""), ids) if addon and ids else None)
