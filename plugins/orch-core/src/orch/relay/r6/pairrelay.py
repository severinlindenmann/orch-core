"""R6c: the relay routes of pairing over R1's :class:`orch.relay.session.Session` (the production :class:`PairRelay`).

Routes (the relay's HTTP fixtures ``pair_host_post``, ``cert_requests_post``, ``cert_request_get``, ``device_put``,
``baseline_post``):

=========================================================  =========  ==================================================
Route                                                      Auth       Outcome mapping (:class:`PairRelayError` kinds)
=========================================================  =========  ==================================================
``POST /v2/workspaces/{ws}/pair/{offer}/{rid}`` (raw ORB2)  W(g)       202; 429/503 ``retry``; other ``refused``
``POST /v2/persons/{person}/cert-requests``                W(g)       ``{request_id}`` must be ours; 409
                                                                      ``request_open``/``replayed``; 410 ``expired``;
                                                                      429 ``retry``; other ``refused``
``GET /v2/cert-requests/{id}``                             W          exactly ``{state, challenge, cert, rejection}``;
                                                                      404 ``not_found``; other ``retry``
``PUT /v2/devices/{device}`` (``{cert}``)                  none       ``{device_id}`` must be the device; 409
                                                                      ``cert_conflict``; 429 ``retry``; other
                                                                      ``refused``
``POST /v2/workspaces/{ws}/baselines`` (``{hash, offer_id}``) W(g)    ``{until_ms}``; 404/410 ``gone``; 429 ``quota``
                                                                      (``quota_exceeded``) or ``retry``; other
                                                                      ``refused``
=========================================================  =========  ==================================================

A transport failure is ``retry`` (the engine resends the same bytes); a malformed 2xx is ``retry`` too, alarmed by the
session's caller. Every call has the 5 s total deadline of the pair thread (11A.15.2 "Threads"). A ``fenced`` answer
or a lost session raises the session's own error: the worker stops (the bridge's poll meets it too).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from orch.net.http import Transport, TransportError

from ..retry import RelayRefused
from ..session import LoginHeldOff, RelayMisbehaving, Session
from .pair import PairRelayError

__all__ = ["DEADLINE_S", "SessionPairRelay"]

DEADLINE_S = 5.0
SMALL = 64 * 1024
_HEX32 = re.compile(r"[0-9a-f]{32}")
_SHA = re.compile(r"sha256:[0-9a-f]{64}")


def _id(value: str) -> str:
    if type(value) is not str or not _HEX32.fullmatch(value):
        raise PairRelayError("refused")
    return value


class SessionPairRelay:
    """The pair thread's relay routes. ``conn`` makes the pair thread's own connection (11A.7: one per thread)."""

    def __init__(self, session: Session, workspace_id: str, *, conn: Callable[[], Transport] | None = None) -> None:
        self._s, self._ws = session, _id(workspace_id)
        self._conn = conn
        self._t: Transport | None = None

    def _transport(self) -> Transport | None:
        if self._conn is None:
            return None
        if self._t is None:
            self._t = self._conn()
        return self._t

    def _call(self, method: str, path: str, route: str, *, refusals: dict[tuple[int, str | None], str], **kw: Any):
        try:
            return self._s.call(method, path, route, deadline_s=DEADLINE_S, transport=self._transport(), **kw)
        except TransportError:
            raise PairRelayError("retry") from None
        except (RelayMisbehaving, LoginHeldOff):  # a login hold-off is transient (as publish.py)
            raise PairRelayError("retry") from None
        except RelayRefused as e:
            kind = refusals.get((e.status, e.code)) or refusals.get((e.status, None))
            if kind is None:
                kind = "retry" if e.status in (429, 500, 502, 503, 504) else "refused"
            raise PairRelayError(kind) from None

    def post_answer(self, offer_id: str, rid: str, envelope: bytes) -> None:
        self._call(
            "POST", f"/v2/workspaces/{self._ws}/pair/{_id(offer_id)}/{_id(rid)}",
            "POST /v2/workspaces/{ws}/pair/{offer}/{rid}", raw=envelope, holder=True, request_cap=SMALL,
            keys=set(), refusals={},
        )  # fmt: skip

    def post_cert_request(self, person_id: str, signed: dict[str, Any]) -> None:
        obj = self._call(
            "POST", f"/v2/persons/{_id(person_id)}/cert-requests", "POST /v2/persons/{person}/cert-requests",
            body={"request": signed}, holder=True, request_cap=SMALL, keys={"request_id"},
            refusals={(409, "request_open"): "request_open", (409, "replayed"): "replayed", (410, None): "expired"},
        )  # fmt: skip
        if obj["request_id"] != signed["o"]["request_id"]:
            raise PairRelayError("retry")  # an answer for another request is not an acknowledgement of ours

    def get_cert_request(self, request_id: str) -> dict[str, Any]:
        return self._call(
            "GET", f"/v2/cert-requests/{_id(request_id)}", "GET /v2/cert-requests/{id}", cap=SMALL,
            keys={"state", "challenge", "cert", "rejection"}, refusals={(404, None): "not_found"},
        )  # fmt: skip

    def put_device(self, device_id: str, cert: dict[str, Any]) -> None:
        obj = self._call(
            "PUT", f"/v2/devices/{_id(device_id)}", "PUT /v2/devices/{device}", body={"cert": cert}, auth=False,
            request_cap=SMALL, keys={"device_id"}, refusals={(409, "cert_conflict"): "cert_conflict"},
        )  # fmt: skip
        if obj["device_id"] != device_id:
            raise PairRelayError("retry")

    def post_baseline(self, hash_: str, offer_id: str) -> None:
        if type(hash_) is not str or not _SHA.fullmatch(hash_):
            raise PairRelayError("refused")
        self._call(
            "POST", f"/v2/workspaces/{self._ws}/baselines", "POST /v2/workspaces/{ws}/baselines",
            body={"hash": hash_, "offer_id": _id(offer_id)}, holder=True, keys={"until_ms"},
            refusals={(404, None): "gone", (410, None): "gone", (429, "quota_exceeded"): "quota"},
        )  # fmt: skip
