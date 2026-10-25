"""One frozen policy object per listener (#1198 R1-3; host spec 0.8.6 item 10).

The listener decides the rules, never a header: which Host and Origin spellings it answers to, its CSP, the WebSocket
Origin rule, which routes answer before login, and which session kinds it accepts. `security.py` asks the policy;
it keeps no listener constants of its own. Today only the loopback listener is wired (`loopback_policy`, behaviour
unchanged and pinned by a golden test). `remote_policy` is the stricter definition for the HTTPS front; R1-4 wires it
to a listener."""

import re
from dataclasses import dataclass

#: The two listeners, by name: a session, a terminal ticket and a request's `scope["orch_listener"]` all use these.
LOOPBACK = "loopback"
REMOTE = "remote"

# §0.8.6 item 7: the required directives (default-src, script-src, object-src, base-uri, frame-ancestors, form-action)
# plus the narrow extras this app needs (its own styles, connections and inline-free images).
# `connect-src` adds the terminal WebSocket on this server's own origin only (section 12.6.6: no `ws:` wildcard, no
# `localhost`); `script-src` and `style-src` are not loosened for xterm. `frame-src` names only the locked Widgets
# route (a path source, never `'self'`); on or off is the route's own check, not this header.
_CSP_LOOPBACK = (
    "default-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
    "form-action 'self'; style-src 'self'; connect-src 'self' ws://127.0.0.1:{port}; img-src 'self' data:; "
    "frame-src http://127.0.0.1:{port}/widget-frame/"
)
# Remote: the same directives, the terminal socket is `wss` on the configured name, no frames at all (the sandboxed
# widget frame is a loopback feature), and nothing is ever fetched over plain HTTP.
_CSP_REMOTE = (
    "default-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
    "form-action 'self'; style-src 'self'; connect-src 'self' wss://{name}; img-src 'self' data:; "
    "frame-src 'none'; upgrade-insecure-requests"
)
HSTS = (b"strict-transport-security", b"max-age=31536000")  # §3 item 1: no preload, no includeSubDomains
TERM_PATH = "/term"

_SEGMENT = re.compile(r"[A-Za-z0-9_-]{1,64}")
_ASSET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_NAME = re.compile(r"(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?")


def clean_path(path: str) -> list[str] | None:
    """The segments of a path that is plain: absolute, ASCII graphic only, no percent escape, backslash, semicolon,
    empty segment (a double slash), dot segment or trailing slash. Anything else is None (fail closed); the root is
    an empty list."""
    if not path.startswith("/") or not path.isascii() or any(c in path for c in "%\\;?#") or not path.isprintable():
        return None
    if path == "/":
        return []
    segments = path[1:].split("/")
    if any(s in ("", ".", "..") for s in segments):
        return None
    return segments


@dataclass(frozen=True)
class ListenerPolicy:
    name: str
    scheme: str  # "http" or "https": the scheme of the Origin the browser sends
    host: str  # the one Host this listener answers to (with its port when it is not the default)
    csp_template: str
    port: int
    terminals: bool  # the `/term` WebSocket (a shell on the server): off by default on the remote listener
    widget_frames: bool  # the locked `/widget-frame/` route and its own CSP
    pre_login_only: bool  # True: before login only `pre_login` answers
    launch_codes: bool  # the local launch-code exchange (`/auth/session`) is a loopback credential
    route_gate: bool  # every route needs an ALLOWED/PRE_LOGIN rule in `routegate.TABLE` (default refuse)
    session_kinds: frozenset | None  # None: every kind of session
    extra_headers: tuple = ()
    # Hosts that also answer on the setup page and the public assets (the first-run page may be opened by name).
    asset_hosts: tuple = ()

    # -- Host and Origin --
    def hosts_for(self, setup_host: bool) -> tuple[str, ...]:
        return (self.host, *self.asset_hosts) if setup_host else (self.host,)

    def origin_for(self, host: str) -> str:
        return f"{self.scheme}://{host}"

    # -- CSP --
    def csp(self) -> str:
        return self.csp_template.format(port=self.port, name=self.host)

    # -- WebSocket --
    def websocket_ok(self, path: str, hosts: list[str], origins: list[str]) -> bool:
        """Only `/term`; Host and Origin exactly this listener's (a missing or `null` Origin refused)."""
        return self.terminals and path == TERM_PATH and hosts == [self.host] and origins == [self.origin_for(self.host)]

    # -- Pre-login routes --
    def pre_login(self, path: str, raw_path: bytes | None = None, method: str = "GET") -> bool:
        """May this path answer without a session? Loopback: every route checks its own credential, so True. Remote:
        only the app shell, its assets, `/auth/passkey/*` and `/auth/enroll/*`, matched exactly and case-sensitively
        on the decoded path AND on the raw bytes (which must be the same string: no escape of any kind)."""
        if not self.pre_login_only:
            return True
        if raw_path is not None:
            try:
                raw = raw_path.decode("ascii")
            except UnicodeDecodeError:
                return False
            if raw != path:
                return False
        segments = clean_path(path)
        if segments is None:
            return False
        if segments == []:
            return True
        from .app import ROOT_FILES, SPA_PATH  # late: app imports security imports this module

        if method in ("GET", "HEAD") and (segments[0] != "api" and SPA_PATH.fullmatch(path[1:])):
            return True  # a reload or a pasted URL: the browser sends no bearer, the shell is public (it holds no data)
        head, rest = segments[0], segments[1:]
        if head == "assets":
            return len(rest) == 1 and bool(_ASSET.fullmatch(rest[0])) and ".." not in rest[0]
        if len(segments) == 1 and segments[0] in ROOT_FILES:
            return True
        if head == "auth" and len(rest) >= 2 and rest[0] in ("passkey", "enroll"):
            tail = rest[1:]
            return len(tail) <= 2 and all(_SEGMENT.fullmatch(s) for s in tail)
        return False

    # -- Sessions --
    def session_kind_ok(self, kind: str) -> bool:
        return self.session_kinds is None or kind in self.session_kinds


def loopback_policy(port: int) -> ListenerPolicy:
    """The 127.0.0.1 listener, exactly as it behaved before the policy object."""
    return ListenerPolicy(
        name=LOOPBACK,
        scheme="http",
        host=f"127.0.0.1:{port}",
        csp_template=_CSP_LOOPBACK,
        port=port,
        terminals=True,
        widget_frames=True,
        pre_login_only=False,
        launch_codes=True,
        route_gate=False,
        session_kinds=None,  # any: the loopback behaviour is unchanged (click routes apply their own kind rule)
        asset_hosts=(f"localhost:{port}",),
    )


def remote_policy(name: str) -> ListenerPolicy:
    """The HTTPS front (port 443 implied): the exact configured name only, `https://<name>` as Origin, a pre-login
    surface of the shell and the passkey/enrollment routes, no launch codes, no `localhost` spelling anywhere."""
    if not _NAME.fullmatch(name):
        raise ValueError("remote name must be a lowercase DNS name (no port, no IP address, no trailing dot)")
    if name.split(".")[-1].isdigit() or name.endswith(".localhost") or name.endswith(".local"):
        raise ValueError("remote name must be a public DNS name")
    return ListenerPolicy(
        name=REMOTE,
        scheme="https",
        host=name,
        csp_template=_CSP_REMOTE,
        port=443,  # the front's port; nothing here binds it
        terminals=False,
        widget_frames=False,
        pre_login_only=True,
        launch_codes=False,
        route_gate=True,
        session_kinds=frozenset({"remote"}),  # the session kind, not the listener
        extra_headers=(HSTS,),
    )
