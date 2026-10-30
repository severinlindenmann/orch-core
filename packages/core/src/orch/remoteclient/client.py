"""The remote client's API for the loopback server and the UI (R5, #1593; design sections 1, 3, 5).

One :class:`Client` per OS user: it keeps the hub it talks to, the hub token, the workspaces this computer is added
to and
the presence key, and offers the screens' verbs:

* :meth:`connect_hub` -- first run, on the office network: fetch the hub card (TOFU over the private network), verify
it,
  pin its TLS key and link address, sign in with ``client: "remote"`` for a 7 day token that grants nothing at a
  workspace;
* :meth:`workspaces` -- the person's rail from the hub (``name`` is "from the hub" until the workspace's own name is
known);
* :meth:`enroll` -- add this computer from a pasted code; ``on_words`` gets the six words to compare;
* :meth:`open` -- a :class:`~orch.remoteclient.session.Session` to a workspace this computer is added to.

All verbs are blocking and thread safe; the server runs them in worker threads. Every refusal is a
:class:`~orch.remoteclient.codes.ClientError` with a stable ``code`` and a plain ``message``."""

from __future__ import annotations

import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from orch.canon.jcs import JcsError, loads_strict
from orch.crypto.encoding import b64u
from orch.hubclient import wire as hubwire
from orch.hubclient.custodial_shape import text_ok
from orch.hubclient.errors import HubRefused
from orch.hubclient.tlsadapter import parse_addr

from . import enroll as E
from .channel import Channel, open_channel
from .codes import ClientError
from .hubconn import HubConn, Wire, dial
from .noise_init import x25519_public
from .presence import PresenceBackend, Prompt
from .session import REMOTE_API, Session, open_session
from .state import Store, refuse_key_file

__all__ = ["Client"]

MAX_CARD = 64 * 1024
Http = Callable[[str, str, bytes | None, dict[str, str]], tuple[int, bytes]]
Connector = Callable[[str, int, str], Wire]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a: Any, **k: Any) -> None:  # noqa: D401
        return None


def _http(method: str, url: str, body: bytes | None, headers: dict[str, str]) -> tuple[int, bytes]:
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, data=body, method=method, headers=headers)  # noqa: S310
    try:
        with opener.open(req, timeout=10) as r:  # noqa: S310
            return r.status, r.read(MAX_CARD + 1)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(MAX_CARD + 1)
    except (urllib.error.URLError, OSError, ValueError):
        raise ClientError("unavailable") from None


class Client:
    def __init__(
        self,
        store: Store,
        *,
        presence: PresenceBackend | None = None,
        http: Http = _http,
        connector: Connector | None = None,
        clock: Callable[[], float] = time.time,
        dev_mode: bool = False,
        api: tuple[int, int] = REMOTE_API,
        rand: Callable[[int], bytes] | None = None,
        batch_window: float = 1.0,
    ) -> None:
        self.store, self.presence, self._http = store, presence, http
        self._connector = connector or (lambda host, port, spki: dial(host, port, spki))
        self._clock, self._dev, self.api, self._rand = clock, dev_mode, api, rand
        self._batch_window = batch_window

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    # -- the hub --

    def connect_hub(self, origin: str, link: str, user: str, password: str, totp: str) -> dict[str, Any]:
        """``origin``: the hub's web address (``http://hub.office:8443``); ``link``: its link address ``host:port`` (the
        card carries none). Replaces a previous hub: connecting again re-pins (the answer to a changed key)."""
        u = urllib.parse.urlsplit(origin)
        if u.scheme not in ("http", "https") or not u.netloc or u.path not in ("", "/") or u.query or u.username:
            raise ClientError("remote_client.no_hub")
        origin = f"{u.scheme}://{u.netloc}"
        try:
            parse_addr(link)
        except HubRefused:
            raise ClientError("remote_client.no_hub") from None
        status, raw = self._http("GET", origin + "/api/v1/hub/card", None, {"Accept": "application/json"})
        if status != 200 or len(raw) > MAX_CARD:
            raise ClientError("remote_client.no_hub")
        try:
            card = loads_strict(raw).get("card")
            verified = hubwire.verify_card(card, now_ms=self._now_ms(), dev_mode=self._dev)
        except (JcsError, UnicodeDecodeError, ValueError, AttributeError, HubRefused):
            raise ClientError("remote_client.no_hub") from None
        c = verified["card"]
        body = json.dumps({"user_id": user, "password": password, "totp": totp, "client": "remote"}).encode()
        status, raw = self._http(
            "POST", origin + "/api/v1/session", body, {"Content-Type": "application/json", "Accept": "application/json"}
        )
        try:
            doc = loads_strict(raw) if status == 200 else {}
        except (JcsError, UnicodeDecodeError, ValueError):
            doc = {}
        if (
            status != 200
            or type(doc) is not dict
            or type(doc.get("token")) is not str
            or type(doc.get("expires_ms")) is not int
        ):
            raise ClientError("no_session")
        self.store.set_hub(origin=origin, link=link, hub_id=c["hub_id"], spki=c["tls_spki_sha256"])
        self.store.set_token(doc["token"], doc["expires_ms"])
        return {"hub_id": c["hub_id"], "expires_ms": doc["expires_ms"]}

    def signed_in(self) -> bool:
        return self.store.hub() is not None and self.store.token(self._now_ms()) is not None

    def _hub(self) -> tuple[dict[str, Any], str]:
        hub = self.store.hub()
        if hub is None:
            raise ClientError("remote_client.no_hub")
        token = self.store.token(self._now_ms())
        if token is None:
            raise ClientError("no_session")
        return hub, token

    def _wire(self, hub: dict[str, Any]) -> Wire:
        host, port = parse_addr(hub["link"])
        return self._connector(host, port, hub["spki"])

    # -- the rail --

    def workspaces(self) -> list[dict[str, Any]]:
        """The person's workspaces from the hub, with whether this computer is added. ``hub_name`` is the hub's word
        and is
        labelled so; ``name`` is the workspace's own name once known (end to end), else the hub's."""
        hub, token = self._hub()
        conn = HubConn(self._wire(hub), token=token, hub_id=hub["hub_id"])
        try:
            conn.hello()
            rows = conn.rail()
        except ClientError as exc:
            if exc.code == "no_session":
                self.store.drop_token()
            raise
        finally:
            conn.close()
        added = self.store.workspaces()
        out = []
        for r in rows:
            e = added.get(r["workspace_id"])
            own = e.get("name") if e else None
            out.append(
                {
                    "workspace_id": r["workspace_id"], "online": r["online"], "added": e is not None,
                    "name": own or r["name"], "name_from_hub": own is None, "hub_name": r["name"],
                }
            )  # fmt: skip
        return out

    # -- presence --

    def _backend(self) -> PresenceBackend:
        if self.presence is None:
            raise ClientError("remote_client.no_presence")
        return self.presence

    def _assert_for(self, entry: dict[str, Any]) -> Callable[[bytes, Prompt], dict[str, Any]]:
        backend, ref = self._backend(), entry["presence_ref"]
        refuse_key_file(ref)
        if backend.kind != entry["presence_kind"]:
            raise ClientError("remote_client.no_presence")
        return lambda msg, prompt: backend.assert_(ref, msg, prompt)

    # -- adding this computer --

    def enroll(self, code_text: str, name: str, on_words: Callable[[str], None]) -> dict[str, str]:
        code = E.parse_code(code_text)  # first: a bad or browser code touches nothing
        hub, token = self._hub()
        backend = self._backend()
        pres = self.store.presence()
        if pres is None or pres["kind"] != backend.kind:
            made = backend.create(name if text_ok(name, 64) else "This computer")
            refuse_key_file(made["ref"])
            self.store.set_presence(made["kind"], made["ref"], made["pub65"])
            pres = self.store.presence()
        assert pres is not None
        from orch.crypto.encoding import unb64u

        pub = unb64u(pres["pub"], 65)
        tkey = (self._rand or secrets.token_bytes)(32)
        ch = open_channel(
            self._wire(hub), token=token, hub_id=hub["hub_id"], workspace_id=code.workspace_id, ident=code.code_id,
            kind="enroll", transport_key=tkey, wrk_pub=code.wrk_pub, rand=self._rand,
        )  # fmt: skip
        device_id = E.run_enroll(
            ch, code, transport_key=tkey, presence_kind=pres["kind"], presence_pub=pub,
            assert_=lambda msg, prompt: backend.assert_(pres["ref"], msg, prompt), name=name, on_words=on_words,
        )  # fmt: skip
        self.store.put_transport_key(code.workspace_id, tkey)
        self.store.add_workspace(
            code.workspace_id,
            {
                "device_id": device_id, "wrk_pub": b64u(code.wrk_pub), "hub_id": hub["hub_id"],
                "presence_kind": pres["kind"], "presence_ref": pres["ref"], "transport_pub": b64u(x25519_public(tkey)),
                "added_ms": self._now_ms(),
            },
        )  # fmt: skip
        return {"workspace_id": code.workspace_id, "device_id": device_id}

    # -- a session --

    def open(self, workspace_id: str, notify: Callable[[Prompt | None], None] | None = None) -> Session:
        entry = self.store.workspace(workspace_id)
        hub, token = self._hub()
        if entry.get("hub_id") != hub["hub_id"]:
            raise ClientError("remote_client.hub_mismatch")
        tkey = self.store.transport_key(workspace_id)
        if tkey is None:
            raise ClientError("remote_client.not_added")
        from orch.crypto.encoding import EncodingError, unb64u

        try:
            wrk = unb64u(entry["wrk_pub"], 32)
        except (KeyError, EncodingError):
            raise ClientError("remote_client.state") from None
        assert_ = self._assert_for(entry)
        ch: Channel = open_channel(
            self._wire(hub), token=token, hub_id=hub["hub_id"], workspace_id=workspace_id, ident=entry["device_id"],
            kind="session", transport_key=tkey, wrk_pub=wrk, rand=self._rand,
        )  # fmt: skip
        session = open_session(
            ch, workspace_id=workspace_id, device_id=entry["device_id"], assert_=assert_, name=entry.get("name"),
            api=self.api, notify=notify, batch_window=self._batch_window,
        )  # fmt: skip
        self._learn_name(session)
        return session

    def _learn_name(self, session: Session) -> None:
        """The workspace's own name, read through the end-to-end session (``GET /api/workspaces``). Best effort:
        until it
        is known, prompts say "workspace <short id>"."""
        try:
            resp = session.request("GET", "/api/workspaces", "", [("accept", "application/json")])
            doc = loads_strict(resp.read(256 * 1024)) if resp.status == 200 else None
        except (ClientError, JcsError, UnicodeDecodeError, ValueError):
            return
        rows = doc.get("workspaces") if isinstance(doc, dict) else doc
        for r in rows if isinstance(rows, list) else []:
            if not isinstance(r, dict):
                continue
            ident = str(r.get("workspace_id") or r.get("id") or "").replace("-", "")
            nm = r.get("name")
            if ident == session.workspace_id and isinstance(nm, str) and text_ok(nm, 200):
                session.name = nm
                try:
                    self.store.set_workspace_name(session.workspace_id, nm)
                except ClientError:
                    pass
                return

    def remove(self, workspace_id: str) -> None:
        """Forget this computer's entry for a workspace (the owner removes the device at the workspace)."""
        self.store.remove_workspace(workspace_id)
