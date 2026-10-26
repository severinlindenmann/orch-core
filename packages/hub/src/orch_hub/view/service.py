"""``log_sync`` on the hub (protocol.md §10.7): receive a sealed page, check it with the contract's
``apply_view_page`` (epoch, position, AEAD, ``host_sig`` under the pinned WSK, chain, ``ws_seq``, ``reset``), keep it
sealed, and read lines back for a build (opened with ``WK_e`` for that call only).

The contract package does every check; nothing is re-implemented here. This module only supplies the inputs
(held heads, the newest ``WK_e``, the pinned WSK) and stores the outcome in one step.
"""

from __future__ import annotations

from typing import Any

from ..contract import phase4 as P4
from ..contract.canon import CanonError, unb64u
from ..contract.hub import Refused
from ..link.wkseal import WkSeals
from .store import ViewStore


class Keyring:
    """``WK_e`` per epoch, opened on demand for ONE use; ``close`` zeroises every copy it opened. A key the caller
    hands in (``known``) is only borrowed, never copied: the caller owns it and wipes it."""

    def __init__(self, seals: WkSeals, ws: str, known: dict[int, bytes | bytearray] | None = None) -> None:
        self._seals, self._ws = seals, ws
        self._known = dict(known or {})
        self._own: list[bytearray] = []

    def get(self, epoch: int) -> bytes | bytearray:
        k = self._known.get(epoch)
        if k is None:
            b = self._seals.open_for_view(self._ws, epoch)
            self._own.append(b)
            self._known[epoch] = k = b
        return k

    def close(self) -> None:
        for b in self._own:
            for i in range(len(b)):
                b[i] = 0
        self._own.clear()
        self._known.clear()


class ViewService:
    def __init__(self, store: ViewStore, seals: WkSeals) -> None:
        self.store, self.seals = store, seals

    # --- the op ------------------------------------------------------------------------------------------------------

    def accept(self, ws: str, wsk_pub: bytes, body: Any) -> dict[str, Any]:
        """One ``log_sync`` body. The caller (the op handler) holds the registry lock and has checked: workspace pinned,
        bound, FULL mode. Returns ``{"seq", "head"}`` of the hub's view of that log after the page."""
        log = body.get("log") if type(body) is dict else None
        if type(log) is not str:
            raise Refused("malformed")
        with self.store.lock:
            try:
                epoch, wk = self.seals.open_current(ws)
            except Refused:
                raise Refused("bad_epoch") from None  # no live wk_seal wrap yet (the host sends wk_seal first)
            try:
                # the contract reads only the log named in the page and the workspace log (for `ws_seq`)
                views = {"workspace": self.store.heads(ws, "workspace")}
                held = views["workspace"] if log == "workspace" else self.store.heads(ws, log)
                views[log] = held
                stub = self.store.proj_key(ws, log)
                projs = {} if stub is None else {log: {"seq": stub[1], "ws_seq": stub[2]}}
                # D15b-4: the lines and the projection pass together or the whole page is refused (nothing is stored)
                new, new_projs = P4.apply_view_page_proj(
                    views, projs, body, workspace_id=ws, wsk_pub=wsk_pub, wk_epochs={epoch: wk}
                )
            finally:
                for i in range(len(wk)):
                    wk[i] = 0
            out = new[log]
            reset = body["reset"]
            np = new_projs.get(log)
            kept = projs.get(log)
            newer = np is not None and np is not kept  # this page's projection replaced the held one (or none held)
            if reset or newer or len(out) > len(held):
                self.store.commit(
                    ws, log, out, len(held), body, reset=reset, proj=(np["seq"], np["ws_seq"]) if newer else None
                )
            return {"seq": len(out), "head": out[-1]}

    # --- reading for a build / the needs-you read ----------------------------------------------------------------------

    def lines(self, ws: str, log: str, keys: Keyring) -> list[dict[str, Any]]:
        """The lines of one log, opened from the stored sealed pages, as a gap-free list (index ``i`` = seq ``i + 1``)
        whose heads equal the stored heads. Anything off (a page that does not open, a hole, a head that differs) is
        ``unavailable``: the hub builds from the view it verified or from nothing."""
        heads = self.store.heads(ws, log)
        by_seq: dict[int, dict[str, Any]] = {}
        try:
            for body in self.store.pages(ws, log):
                for i, e in enumerate(P4.open_view_page(keys.get(body["epoch"]), ws, body)):
                    by_seq[body["from_seq"] + i] = e
            out = [by_seq[i + 1] for i in range(len(heads))]
            if any(P4.event_head(e) != h for e, h in zip(out, heads, strict=True)):
                raise Refused("unavailable")
        except Refused, KeyError, TypeError, ValueError, CanonError:
            raise Refused("unavailable") from None
        return out

    def proj(self, ws: str, log: str, keys: Keyring) -> dict[str, Any] | None:
        """The held ticket projection of ``log`` (D15b), opened from the sealed page that carried it; None when the hub
        holds none. A page that does not open reads as none (fail closed: the build says ``unavailable``)."""
        body = self.store.proj_page(ws, log)
        if body is None:
            return None
        try:
            p = P4.open_view_doc(keys.get(body["epoch"]), ws, body)[1]
        except Refused, KeyError, TypeError, ValueError, CanonError:
            return None
        return p if type(p) is dict else None

    def logs(self, ws: str) -> list[str]:
        return self.store.logs(ws)

    def head(self, ws: str, log: str) -> str | None:
        h = self.store.heads(ws, log)
        return h[-1] if h else None


def wsk_of(pin: dict[str, str]) -> bytes:
    try:
        return unb64u(pin["wsk_pub"], 65)
    except CanonError, KeyError:
        raise Refused("unknown_workspace") from None


__all__ = ["Keyring", "ViewService", "wsk_of"]
