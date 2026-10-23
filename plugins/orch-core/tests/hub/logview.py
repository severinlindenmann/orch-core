"""A test-side port of the hub's ``log_sync`` rules (hub contract section 10.7, ``apply_view_page``), written from the
text and replayed against the contract's ``log_sync`` vector cases. It shares no code with ``orch.hubclient.logsync``
(the sender under test): the key, AAD and signed bytes are rebuilt here from the contract. The fake hub uses it."""

from __future__ import annotations

import re
import struct
from typing import Any

from cryptography.exceptions import InvalidTag

from orch.canon.jcs import JcsError, loads_strict
from orch.crypto import suite as S
from orch.crypto.encoding import unb64u

from . import contract as H

ULID = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}")
HEAD = re.compile(r"sha256:[0-9a-f]{64}")
FIELDS = {"epoch", "log", "from_seq", "count", "head", "reset", "sealed"}
MAX_SEALED = 1 << 20


class View:
    """What the hub holds: ``log -> [(event, head)]``."""

    def __init__(self) -> None:
        self.logs: dict[str, list[tuple[dict[str, Any], str]]] = {}
        self.proj: dict[str, dict[str, Any]] = {}  # D15b: ticket log -> the newest P by (seq, ws_seq)

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {k: {"seq": len(v), "head": v[-1][1]} for k, v in sorted(self.logs.items()) if v}
        if self.proj:
            out["proj"] = dict(sorted(self.proj.items()))
        return out


def head_of(event: dict[str, Any]) -> str:
    return "sha256:" + S.sha256(b"orch/v2/event|" + H.cj(event)).hex()


def k_view(wk_e: bytes, workspace_id: str, epoch: int) -> bytes:
    return S.hkdf(wk_e, b"", b"orch/v2/hub-view|" + workspace_id.encode() + b"|" + str(epoch).encode())


def aad(workspace_id: str, b: dict[str, Any]) -> bytes:
    name = b["log"].encode()
    return (
        b"orch/v2/hub-view-aad|" + bytes([S.SUITE_ID]) + bytes.fromhex(workspace_id) + struct.pack(">I", b["epoch"])
        + struct.pack(">H", len(name)) + name + struct.pack(">Q", b["from_seq"]) + struct.pack(">I", b["count"])
        + bytes.fromhex(b["head"][7:]) + bytes([1 if b["reset"] else 0])
    )  # fmt: skip


def host_sig_bytes(workspace_id: str, log: str, event: dict[str, Any]) -> bytes:
    body = {k: v for k, v in event.items() if k != "host_sig"}
    return b"orch/v2/sig/host-event|" + H.cj(
        {"contract": 1, "suite": 2, "workspace_id": workspace_id, "log": log, "event": body}
    )


STATUSES = ("backlog", "open", "in_progress", "testing", "done", "closed")
GATES = ("requirements", "plan", "code", "verify")
SHA = re.compile(r"[0-9a-f]{40}")


def _gate_entry_ok(name: str, g: Any) -> bool:
    """One ``P.gates`` entry (contract 10.2b ``f1`` gate entry), written from the text."""
    src = name in ("code", "verify")
    if not (type(g) is dict and set(g) == {"gen", "hash", "policy_hash"} | ({"source_sha"} if src else set())):
        return False
    if not (_int(g["gen"], 0) and all(type(g[k]) is str and HEAD.fullmatch(g[k]) for k in ("hash", "policy_hash"))):
        return False
    if not src:
        return True
    s = g["source_sha"]
    return (
        type(s) is list and len(s) <= 32
        and all(type(e) is dict and set(e) == {"repo", "ref", "sha"} and type(e["repo"]) is str
                and type(e["ref"]) is str
                and type(e["sha"]) is str and SHA.fullmatch(e["sha"]) for e in s)
        and [e["repo"] for e in s] == sorted({e["repo"] for e in s})
    )  # fmt: skip


def _proj_shape_ok(p: Any) -> bool:
    return (
        type(p) is dict and set(p) == {"seq", "head", "ws_seq", "ws_head", "status", "roster_v", "gates"}
        and _int(p["seq"], 1) and _int(p["ws_seq"], 1) and _int(p["roster_v"], 0)
        and type(p["head"]) is str and HEAD.fullmatch(p["head"]) and type(p["ws_head"]) is str
        and HEAD.fullmatch(p["ws_head"]) and p["status"] in STATUSES and type(p["gates"]) is dict
        and all(k in GATES and _gate_entry_ok(k, g) for k, g in p["gates"].items())
    )  # fmt: skip


def _int(x: Any, lo: int, hi: int | None = None) -> bool:
    return type(x) is int and x >= lo and (hi is None or x <= hi)


def apply_page(view: View, body: Any, *, workspace_id: str, wsk_pub: bytes, wk: dict[int, bytes]) -> dict[str, Any]:
    """Apply one page to ``view`` (steps 1-6 of the contract, in order). ``wk``: epoch -> the ``WK_e`` the hub holds."""
    if not (
        type(body) is dict
        and set(body) == FIELDS
        and _int(body["epoch"], 1, 2**32 - 1)
        and type(body["log"]) is str
        and (body["log"] == "workspace" or ULID.fullmatch(body["log"]))
        and _int(body["from_seq"], 1)
        and _int(body["count"], 1, 256)
        and type(body["head"]) is str
        and HEAD.fullmatch(body["head"])
        and type(body["reset"]) is bool
        and type(body["sealed"]) is str
        and (not body["reset"] or body["from_seq"] == 1)
    ):
        raise H.Refused("malformed")
    log, from_seq = body["log"], body["from_seq"]
    if not wk or body["epoch"] != max(wk):
        raise H.Refused("bad_epoch")
    held = 0 if body["reset"] else len(view.logs.get(log, []))
    if from_seq > held + 1:
        raise H.Refused("view_gap")
    try:
        raw = unb64u(body["sealed"])
    except H.CanonError:
        raise H.Refused("malformed") from None
    if len(body["sealed"]) > MAX_SEALED:
        raise H.Refused("too_large")
    try:
        pt = S.salted_open(k_view(wk[body["epoch"]], workspace_id, body["epoch"]), b"orch/v2/hub-view-msg",
                           aad(workspace_id, body), raw)  # fmt: skip
        doc = loads_strict(pt)
    except (InvalidTag, JcsError):
        raise H.Refused("bad_wrap") from None
    if (
        type(doc) is not dict
        or set(doc) not in ({"lines"}, {"lines", "proj"})
        or type(doc["lines"]) is not list
        or len(doc["lines"]) != body["count"]
    ):
        raise H.Refused("malformed")
    base = [] if body["reset"] else list(view.logs.get(log, []))
    ws_held = len(view.logs.get("workspace", [])) if not (body["reset"] and log == "workspace") else 0
    out = list(base)
    head = base[from_seq - 2][1] if from_seq >= 2 else None
    for i, ev in enumerate(doc["lines"]):
        seq = from_seq + i
        if type(ev) is not dict or ev.get("seq") != seq or type(ev.get("host_sig")) is not str:
            raise H.Refused("malformed")
        try:
            sig_ok = S.verify(wsk_pub, unb64u(ev["host_sig"], 64), host_sig_bytes(workspace_id, log, ev))
        except (H.CanonError, S.CryptoError):
            sig_ok = False
        if not sig_ok:
            raise H.Refused("bad_signature")
        h = head_of(ev)
        if ev.get("prev") != head or (seq <= len(base) and base[seq - 1][1] != h):
            raise H.Refused("view_fork")
        if log != "workspace" and not (type(ev.get("ws_seq")) is int and ev["ws_seq"] <= ws_held):
            raise H.Refused("view_gap")
        if seq > len(out):
            out.append((ev, h))
        head = h
    if head != body["head"]:
        raise H.Refused("malformed")
    new_proj = None
    if "proj" in doc:  # step 7 (D15b): checked against the view after this page; a refusal refuses the whole page
        p = doc["proj"]
        last = doc["lines"][-1]
        if log == "workspace" or not _proj_shape_ok(p):
            raise H.Refused("malformed")
        if p["seq"] != last["seq"] or p["head"] != body["head"] or p["ws_seq"] < last["ws_seq"]:
            raise H.Refused("malformed")
        ws_rows = [] if (body["reset"] and log == "workspace") else view.logs.get("workspace", [])
        if p["ws_seq"] > len(ws_rows):
            raise H.Refused("view_gap")
        if ws_rows[p["ws_seq"] - 1][1] != p["ws_head"]:
            raise H.Refused("view_fork")
        new_proj = p
    if body["reset"] and log == "workspace":
        view.logs = {}  # a workspace reset drops every ticket view
        view.proj = {}
    if body["reset"]:
        view.proj.pop(log, None)
    if new_proj is not None:
        old = view.proj.get(log)
        if old is None or (new_proj["seq"], new_proj["ws_seq"]) >= (old["seq"], old["ws_seq"]):
            view.proj[log] = new_proj
    view.logs[log] = out
    return {"seq": len(out), "head": out[-1][1]}
