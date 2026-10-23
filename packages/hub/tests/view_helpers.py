"""D15 test kit: a fake HOST that builds host-signed, chained log lines and seals ``log_sync`` pages exactly as the
contract says (``P4.seal_view_page``), plus the hub-side parts (store, service, builder, reader) on real files."""

from __future__ import annotations

from typing import Any

from custodial_helpers import LOG, WK, WS

from orch_hub.contract import phase4 as P4
from orch_hub.contract import suite as S
from orch_hub.contract import vectorgen as G
from orch_hub.contract.canon import b64u
from orch_hub.view import build_view_services
from orch_hub.view import project as PR

WSK = G.K.priv["wsk_a"]
WSK_PUB = G.K.pub["wsk_a"]
OWNER = "p_" + "0f" * 16
TK2 = "01J9ZP0000000000000000TKT2"


class Host:
    """Appends host-signed lines to per-log chains and seals pages from them."""

    def __init__(self, ws: str = WS, wk: bytes = WK, epoch: int = 1, key=WSK) -> None:
        self.ws, self.wk, self.epoch, self.key = ws, wk, epoch, key
        self.logs: dict[str, list[dict[str, Any]]] = {}
        self.n = 0

    def append(self, log: str, typ: str, **fields: Any) -> dict[str, Any]:
        lines = self.logs.setdefault(log, [])
        self.n += 1
        e: dict[str, Any] = {
            "v": 2,
            "id": f"01J9ZP{self.n:020d}",
            "seq": len(lines) + 1,
            "at": "2026-10-22T20:00:00Z",
            "type": typ,
            "actor": {"kind": "host"},
            "based_on": P4.event_head(lines[-1]) if lines else None,
            "prev": P4.event_head(lines[-1]) if lines else None,
            "hash_v": 1,
            **fields,
        }
        if log != "workspace":
            e["ws_seq"] = len(self.logs.get("workspace", []))
        e["host_sig"] = b64u(S.sign(self.key, P4.host_sig_bytes(self.ws, log, e), deterministic=True))
        lines.append(e)
        return e

    def page(self, log: str, from_seq: int = 1, count: int | None = None, *, reset: bool = False, **kw: Any):
        lines = self.logs[log][from_seq - 1 :]
        lines = lines[:count] if count else lines
        salt = S.fake_seed(f"d15/salt/{log}/{from_seq}/{len(lines)}/{self.epoch}/{self.n}")[:16]
        return P4.seal_view_page(
            kw.pop("wk", self.wk),
            self.ws,
            kw.pop("epoch", self.epoch),
            log,
            from_seq,
            lines,
            salt,
            reset=reset,
            proj=kw.pop("proj", None),
        )

    def proj(self, log: str = LOG, status: str = "in_progress", gates: dict | None = None, roster_v: int | None = None):
        """The host's projection P of ``log``'s last line at the workspace head (D15b, §10.7)."""
        ws = self.logs["workspace"]
        return {
            "seq": len(self.logs[log]),
            "head": P4.event_head(self.logs[log][-1]),
            "ws_seq": len(ws),
            "ws_head": P4.event_head(ws[-1]),
            "status": status,
            "roster_v": PR.project_workspace(ws).roster_v if roster_v is None else roster_v,
            "gates": gates or {},
        }

    def gate(self, gen: int = 1, hash_: str | None = None, sources: list | None = None) -> dict:
        """One §10.2b gate entry; ``sources`` (a list of ``{repo, ref, sha}``) only on code/verify."""
        g: dict = {"gen": gen, "hash": hash_ or "sha256:" + "ab" * 32, "policy_hash": "sha256:" + "cd" * 32}
        if sources is not None:
            g["source_sha"] = sources
        return g

    # --- a small realistic workspace ---------------------------------------------------------------------------------

    def genesis(self) -> None:
        self.append(
            "workspace",
            "workspace.created",
            prefix="DEMO",
            workspace_id=self.ws,
            owner={"person": OWNER, "name": "Ann", "pk_pub": "x"},
            roster_v=0,
        )

    def ticket(self, uid: str = LOG, key: str = "DEMO-0001", title: str = "Load tariffs") -> None:
        self.append(uid, "ticket.created", key=key, title=title, ticket_type="feature", owner=OWNER, roster_v=1)

    def ask(self, uid: str = LOG, to: str = OWNER, text: str = "CSV or API?", qn: str = "Q1", options=None) -> str:
        q: dict[str, Any] = {"id": qn, "to": to, "text": text, "blocking": True}
        if options:
            q["options"] = options
        qid = PR.question_id(self.ws, uid, qn)
        h = PR.question_hash(qid, uid, text, options)
        self.append(uid, "question.asked", question=q, qid=qid, hash=h, roster_v=1)
        return h

    def push_all(self, svc, log: str | None = None) -> None:
        for lg in [log] if log else sorted(self.logs, key=lambda x: x != "workspace"):
            svc.service.accept(self.ws, WSK_PUB, self.page(lg))


def services(tmp_path, seals, registry=None, access=None, mode="full"):
    d = tmp_path / "view"
    d.mkdir(mode=0o700, exist_ok=True)

    class _Reg:
        def mode(self, ws):
            return mode

        def owner_alias(self, ws):
            return None

    class _Acc:
        def role_of(self, user, ws):
            return "edit"

    return build_view_services(d, wk_seals=seals, registry=registry or _Reg(), access=access or _Acc())
