"""A stateful stand-in for orch-relay's checkpoint routes (R3 tests, layer 1): ``relay-info``, staging pages and the
manifest, the final ``PUT checkpoint`` (plain and restore) and the verified ``GET checkpoint``, with the relay's check
order from ``src/orch_relay/checkpoints.py`` (``put_staging``, ``publish``, ``restore``) at the pinned commit.

Its checks are an **independent** copy of the relay's (page digest, manifest order and contiguity, the running total
against the maximum publication size, the manifest's 74-bytes-per-head lower bound, receipts, the lower-``n`` rules,
``source_hash``, ``checkpoint_time``, ``staging_incomplete``, the folds, ``used == pages``), so the publisher is tested
against the relay's construction, not against itself. Restores check the source, the retained/abandoned folds and the
line's fold, not the owner policy or the evidence (T2 runs those against the real relay).

``fault(seen) -> Reply | None`` answers before anything else (hostile modes); ``after(seen, reply) -> Reply`` can
rewrite an answer after the state changed (a lost or lying acknowledgement)."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

from orch import canon, crypto
from orch.store.checkpoints import verify_object

from .mock import Reply, Seen, json_reply
from .sim import SimRelay, _err

ZERO = "sha256:" + "0" * 64
EMPTY = {"seq": 0, "head": None, "acc": ZERO}
PAGE_CAP = 4 << 20
HOUR_MS = 3_600_000


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _entries(cp: dict[str, Any] | None) -> dict[str, Any]:
    return {} if cp is None else {"workspace": cp["o"]["workspace_log"], **cp["o"]["tickets"]}


def _h(cp: dict[str, Any] | None) -> str | None:
    return None if cp is None else canon.checkpoint_hash(cp["o"])


class Refused(Exception):  # noqa: N818
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status, self.code = status, code


def need(ok: object, code: str = "malformed", status: int = 400) -> None:
    if not ok:
        raise Refused(status, code)


class CheckpointSim(SimRelay):
    def __init__(
        self, origin: str, *, owner_pk: bytes, wsk_pub: bytes, ws: str, max_pub: int = 16 << 20,
        info: dict[str, Any] | None | bool = True, now_ms: int = 1_790_000_000_000,
    ) -> None:  # fmt: skip
        super().__init__(origin, owner_pk=owner_pk, now_ms=now_ms)
        self.ws, self.wsk_pub, self.max_pub = ws, wsk_pub, max_pub
        self.cards[ws] = {"o": {"wsk_pub": crypto.b64u(wsk_pub)}}  # the card, for login only
        self.info = info
        self.held: dict[str, Any] | None = None
        self.checkpoints: dict[str, dict[str, Any]] = {}
        self.nodes: dict[str, dict[str, tuple[int, str | None, str | None]]] = {}
        self.stages: dict[tuple[str, str], dict[str, Any]] = {}
        self.receipts: dict[str, dict[str, Any]] = {}
        self.restore_ids: set[str] = set()
        self.fault: Callable[[Seen], Reply | None] | None = None
        self.after: Callable[[Seen, Reply], Reply] | None = None
        self.log: list[tuple[str, str]] = []  # (kind, detail) of every staging and final request
        self.accepted: list[tuple[int, str]] = []

    # -- routing ------------------------------------------------------------------------------------------------

    def _route(self, s: Seen) -> Reply:
        if self.fault is not None:
            r = self.fault(s)
            if r is not None:
                return r
        r = self._checkpoint_route(s)
        if r is None:
            return super()._route(s)
        return self.after(s, r) if self.after is not None else r

    def _checkpoint_route(self, s: Seen) -> Reply | None:
        if s.method == "GET" and s.path == "/v2/relay-info":
            if self.info is False or self.info is None:
                return _err(404, "not_found")
            default = {"v": 1, "reader": 1, "max_publication_bytes": self.max_pub}
            body = self.info if isinstance(self.info, dict) else default
            return json_reply(200, body)
        p = s.path.split("/")
        if len(p) < 5 or p[1:3] != ["v2", "workspaces"] or p[3] != self.ws:
            return None
        rest = p[4:]
        try:
            if rest == ["checkpoint"] and s.method == "GET":
                return self._auth(s, self.ws) or self._get(s)
            if rest == ["checkpoint"] and s.method == "PUT":
                return self._auth(s, self.ws) or self._fence(s, self.ws) or self._publish(s)
            if rest[:1] == ["staging"] and s.method == "PUT":
                return self._auth(s, self.ws) or self._fence(s, self.ws) or self._staging(s, rest[1:])
        except Refused as e:
            self.log.append(("refused", e.code))
            return _err(e.status, e.code)
        return None

    # -- reads --------------------------------------------------------------------------------------------------

    def _get(self, s: Seen) -> Reply:
        h = s.query.get("hash")
        cp = self.checkpoints.get(h) if h else self.held
        if cp is None:
            return _err(404, "not_found")
        g = canon.loads_strict(self.genesis[self.ws])
        return json_reply(200, {"genesis": g, "checkpoint": cp, "transitions_head": 0})

    # -- staging ------------------------------------------------------------------------------------------------

    def _staging(self, s: Seen, rest: list[str]) -> Reply:
        need(len(rest) in (3, 4), "not_found", 404)
        source, target = rest[0], rest[1]
        key = (source, target)
        for k in [k for k, st in self.stages.items() if st["exp"] <= self.now]:
            del self.stages[k]
        st = self.stages.get(key)
        if st is None:
            need(len(self.stages) < 2, "quota_exceeded", 429)
            st = self.stages[key] = {"exp": self.now + HOUR_MS, "pages": {}, "manifest": None, "slots": None,
                                     "bytes": 0}  # fmt: skip
        if rest[2] == "pages" and len(rest) == 4:
            self.log.append(("page", rest[3]))
            return self._page(st, int(rest[3]), s.body)
        need(rest[2:] == ["manifest"], "not_found", 404)
        self.log.append(("manifest", target))
        return self._manifest(st, s.body)

    def _page(self, st: dict[str, Any], i: int, wire: bytes) -> Reply:
        need(len(wire) <= PAGE_CAP, "too_large", 413)
        body = canon.loads_strict(wire)
        need(isinstance(body, dict) and set(body) == {"kind", "log", "after_seq", "from_acc", "heads", "digest"})
        need(body["kind"] in ("appended", "abandoned", "retained") and body["heads"])
        need(body["digest"] == _digest(canon.dumps({k: v for k, v in body.items() if k != "digest"})))
        if i in st["pages"]:
            need(st["pages"][i] == wire, "staging_conflict", 409)
            return json_reply(200, {})
        if st["manifest"] is not None:
            slot = st["slots"].get(i)
            need(slot is not None and slot == self._descriptor(i, body), "staging_conflict", 409)
        need(st["bytes"] + len(wire) <= self.max_pub, "too_large", 413)
        st["bytes"] += len(wire)
        st["pages"][i] = wire
        return json_reply(200, {})

    @staticmethod
    def _descriptor(i: int, page: dict[str, Any]) -> dict[str, Any]:
        return {"i": i, "kind": page["kind"], "log": page["log"], "after_seq": page["after_seq"],
                "count": len(page["heads"]), "digest": page["digest"]}  # fmt: skip

    def _complete(self, st: dict[str, Any]) -> dict[str, Any]:
        missing = [i for i in st["order"] if i not in st["pages"]]
        return {"complete": not missing, "missing": missing}

    def _manifest(self, st: dict[str, Any], wire: bytes) -> Reply:
        body = canon.loads_strict(wire)
        need(isinstance(body, dict) and set(body) == {"pages"} and isinstance(body["pages"], list))
        need(len(wire) <= PAGE_CAP, "too_large", 413)
        if st["manifest"] is not None:
            need(st["manifest"] == wire, "staging_conflict", 409)
            return json_reply(200, self._complete(st))
        seen, prev, order = set(), {}, []
        for d in body["pages"]:
            need(isinstance(d, dict) and set(d) == {"i", "kind", "log", "after_seq", "count", "digest"})
            need(d["i"] not in seen and d["count"] >= 1)
            seen.add(d["i"])
            g = (d["kind"], d["log"])
            need(g not in prev or prev[g] == d["after_seq"])
            prev[g] = d["after_seq"] + d["count"]
            order.append((*g, d["after_seq"]))
            if d["i"] in st["pages"]:
                need(self._descriptor(d["i"], canon.loads_strict(st["pages"][d["i"]])) == d, "staging_conflict", 409)
        need(order == sorted(order))
        need(set(st["pages"]) <= seen, "staging_conflict", 409)
        missing = sum(d["count"] for d in body["pages"] if d["i"] not in st["pages"])
        need(st["bytes"] + len(wire) + 74 * missing <= self.max_pub, "too_large", 413)
        st["bytes"] += len(wire)
        st["manifest"] = wire
        st["slots"] = {d["i"]: d for d in body["pages"]}
        st["order"] = [d["i"] for d in body["pages"]]
        st["groups"] = {}
        for d in body["pages"]:
            st["groups"].setdefault((d["kind"], d["log"]), []).append(d)
        return json_reply(200, self._complete(st))

    # -- the final publication ----------------------------------------------------------------------------------

    def _publish(self, s: Seen) -> Reply:
        body = canon.loads_strict(s.body)
        need(isinstance(body, dict) and set(body) in ({"checkpoint", "source_hash"},
                                                     {"checkpoint", "source_hash", "restore"}))  # fmt: skip
        receipt = _digest(canon.dumps(body))
        self.log.append(("final", str(body["checkpoint"]["o"]["n"])))
        if receipt in self.receipts:
            return json_reply(200, self.receipts[receipt])
        cp = body["checkpoint"]
        need(verify_object(self.wsk_pub, cp), "bad_signature", 401)
        need(self.ws in self.genesis, "no_genesis", 409)
        pin = canon.loads_strict(self.genesis[self.ws])["pin"]
        o = cp["o"]
        need(o["workspace_id"] == self.ws and o["genesis"] == pin["o"]["genesis"], "genesis_mismatch", 409)
        h = canon.checkpoint_hash(o)
        held = self.held
        result = {"result": "accepted", "n": o["n"], "hash": h}
        if held and o["n"] <= held["o"]["n"]:
            if o == held["o"] and "restore" not in body:
                return json_reply(200, result)
            if o["n"] == held["o"]["n"] or "restore" in body:
                raise Refused(409, "restore_source_mismatch" if "restore" in body else "chain_diverged")
            old = _entries(held)
            need(all(k in old and (e["seq"] < old[k]["seq"] or e == old[k]) for k, e in _entries(cp).items()),
                 "chain_diverged", 409)  # fmt: skip
            return json_reply(200, {**result, "result": "ignored"})
        need(body["source_hash"] == _h(held), "restore_source_mismatch", 409)
        at = _ms(o["at"])
        need(at <= self.now + 300_000 and (not held or at >= _ms(held["o"]["at"])), "checkpoint_time", 409)
        key = ("null" if body["source_hash"] is None else body["source_hash"], h)
        st = self.stages.get(key)
        need(st is not None and st["exp"] > self.now and st["manifest"] is not None
             and self._complete(st)["complete"], "staging_incomplete", 409)  # fmt: skip
        need(st["bytes"] <= self.max_pub, "too_large", 400)
        old, offered = _entries(held), _entries(cp)
        need(not old.keys() - offered.keys(), "chain_diverged", 409)
        used: set[int] = set()
        nodes: list[tuple[str, str, tuple[int, str, str]]] = []
        restored = None
        if "restore" in body:
            restored = self._restore(body, cp, held, st, used, nodes)
        for lg, entry in offered.items():
            if lg == restored:
                continue
            got = self._fold(st, "appended", lg, old.get(lg, EMPTY), used, nodes)
            need(got == entry, "chain_diverged", 409)
        need(used == set(st["order"]), "chain_diverged", 409)
        for lg, acc, node in nodes:
            self.nodes.setdefault(lg, {})[acc] = node
        self.held = cp
        self.checkpoints[h] = cp
        self.receipts[receipt] = result
        del self.stages[key]
        self.accepted.append((o["n"], h))
        return json_reply(200, result)

    def _fold(self, st, kind, lg, start, used, nodes) -> dict[str, Any]:
        cur = {k: start[k] for k in ("seq", "head", "acc")}
        for d in st["groups"].get((kind, lg), ()):
            p = canon.loads_strict(st["pages"][d["i"]])
            need(p["after_seq"] == cur["seq"] and p["from_acc"] == cur["acc"], "chain_diverged", 409)
            for head in p["heads"]:
                acc = canon.log_acc(cur["acc"], head)
                nodes.append((lg, acc, (cur["seq"] + 1, head, cur["acc"])))
                cur = {"seq": cur["seq"] + 1, "head": head, "acc": acc}
            used.add(d["i"])
        return cur

    def _at_seq(self, lg: str, entry: dict[str, Any], seq: int) -> dict[str, Any]:
        cur = dict(entry)
        index = self.nodes.get(lg, {})
        while cur["seq"] > seq:
            node = index.get(cur["acc"])
            need(node is not None, "restore_invalid", 409)
            prev = node[2]
            if cur["seq"] - 1 == 0:
                cur = dict(EMPTY)
                break
            pnode = index.get(prev)
            need(pnode is not None, "restore_invalid", 409)
            cur = {"seq": pnode[0], "head": pnode[1], "acc": prev}
        return cur

    def _restore(self, body, cp, held, st, used, nodes) -> str:
        bundle = body["restore"]
        need(isinstance(bundle, dict) and set(bundle) == {"line", "cert", "authz", "log"}, "restore_invalid", 409)
        line, lg = bundle["line"], bundle["log"]
        ref = None if held is None else {"n": held["o"]["n"], "hash": _h(held)}
        need(line["source"] == ref, "restore_source_mismatch", 409)
        need(line["id"] not in self.restore_ids, "restore_reused", 409)
        need(line["seq"] == line["from_seq"] + 1 and line["prev"] == line["head"], "restore_invalid", 409)
        old = _entries(held).get(lg, EMPTY)
        fs = line["from_seq"]
        if fs < old["seq"]:
            need(not st["groups"].get(("retained", lg)), "restore_invalid", 409)  # the sim holds the path
            retained = self._at_seq(lg, old, fs)
            abandoned = self._fold(st, "abandoned", lg, retained, used, nodes)
            need(abandoned == old, "restore_invalid", 409)
            ab = line["abandoned"]
            need(ab is not None and ab["seq"] >= old["seq"], "restore_invalid", 409)
        else:
            retained = self._fold(st, "retained", lg, old, used, nodes)
        need(retained["seq"] == fs and retained["head"] == line["head"], "restore_invalid", 409)
        head = canon.event_head(line)
        acc = canon.log_acc(retained["acc"], head)
        nodes.append((lg, acc, (fs + 1, head, retained["acc"])))
        need(_entries(cp).get(lg) == {"seq": fs + 1, "head": head, "acc": acc}, "restore_invalid", 409)
        self.restore_ids.add(line["id"])
        return lg

    # -- test helpers -------------------------------------------------------------------------------------------

    def forget(self) -> None:
        """A relay restored from an empty backup: nothing held, nothing staged."""
        with self.lock:
            self.held, self.checkpoints, self.stages, self.receipts, self.nodes = None, {}, {}, {}, {}

    def roll_back_to(self, h: str) -> None:
        """A relay restored from an older backup: it holds the checkpoint ``h`` again (``h`` was accepted)."""
        with self.lock:
            self.held = self.checkpoints[h]
            self.stages, self.receipts = {}, {}


def _ms(at: str) -> int:
    import calendar
    import time

    return calendar.timegm(time.strptime(at, "%Y-%m-%dT%H:%M:%SZ")) * 1000
