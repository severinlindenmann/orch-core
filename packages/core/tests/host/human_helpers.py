"""H5a test helpers: a person-class connection on a served workspace, an in-memory signing backend for a test person,
and the prepare → sign → submit round trip."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from orch.custody.describe import describe_payload
from orch.host import signer
from tests.host.dispatch_helpers import SESSION, Served, err, ok
from tests.host.lifecycle_helpers import Lab

__all__ = ["KeyBackend", "Human", "SESSION", "err", "ok"]


class KeyBackend:
    """A person-capable custody backend over a test person's in-memory device key. It records each payload it signs
    and the prompt fields the real passphrase backend would show (``describe_payload``: fail closed)."""

    name = "test"
    person_capable = True

    def __init__(self, person: Any, auth: str = "passphrase") -> None:
        self.person, self.auth = person, auth
        self.payloads: list[bytes] = []
        self.prompts: list[list[tuple[str, str]]] = []

    def sign(self, key_id: str, payload: bytes, *, action: str) -> bytes:
        self.prompts.append(describe_payload(payload))
        self.payloads.append(payload)
        return self.person.dk_sign(payload)


class Human:
    """The owner of a served Lab workspace, at a person-class connection (column P)."""

    def __init__(self, served: Served, person: Any = None) -> None:
        self.served = served
        self.person = person or served.lab.ws.owner
        self.c = served.client()
        self.backend = KeyBackend(self.person)

    @property
    def signer(self) -> dict[str, str]:
        return {"device": self.person.device, "auth": "passphrase"}

    def prepare_res(self, op: str, args: dict[str, Any], *, handoff: bool = False, **kw: Any) -> dict[str, Any]:
        if op == "hub.join" and "hub_id" not in args and isinstance(args.get("hub_card"), dict):
            args = {
                **args,
                "hub_id": (args["hub_card"].get("o") or {}).get("hub_id"),
            }  # D12c: tests that don't test it name the card's own
        body: dict[str, Any] = {"op": op, "args": args}
        if handoff:
            body["handoff"] = True
        else:
            body["signer"] = kw.pop("signer", self.signer)
        return self.c.req("human.prepare", body, **kw)

    def prepare(self, op: str, args: dict[str, Any], **kw: Any) -> dict[str, Any]:
        return ok(self.prepare_res(op, args, **kw))["data"]

    def prepare_unbound(self, op: str, args: dict[str, Any], **kw: Any) -> dict[str, Any]:
        """A request in state ``prepared`` (no device yet), for tests of the host's binding at ``human.get``.

        ``human.prepare`` stores an unbound request only for a hub hand-off (``addon.grant``); every other op is bound
        at prepare. So this prepares the request bound to this person's device through the real ``human.prepare``
        (every prepare-time check runs) and takes the binding off the stored record on the workspace thread: the
        events' ``actor.device`` and ``auth`` back to ``null``, ``state: prepared``, ``signer: null``, exactly what the
        binding then fills in. The fingerprint and review digest never cover those fields. Returns ``human.get``'s
        view of the unbound request. Test-only: production has no way to unbind a request."""
        rid = self.prepare(op, args, **kw)["request"]["id"]
        service = self.served.service

        def unbind() -> None:
            record = service.requests.read(rid)
            for ev in record["prepared"]["events"]:
                ev["actor"]["device"], ev["auth"] = None, None
            service.requests.write({**record, "state": "prepared", "signer": None})

        service.submit(unbind).result()
        return ok(self.get(rid))["data"]

    def check(self, prepared: dict[str, Any], **kw: Any) -> None:
        signer.check(
            prepared,
            workspace_id=self.served.wid,
            person=kw.pop("person", self.person.ref),
            device=kw.pop("device", self.person.device),
            auth=kw.pop("auth", "passphrase"),
        )

    def owner_proof(self, prepared: dict[str, Any], **over: Any) -> dict[str, Any]:
        """#1198 C9: the owner proof the terminal would make with the owner's backup key (here: the test person's
        ``PK``), over the prepared hub event, bound to this request, at the lab's clock. ``over`` overrides a field."""
        from orch.host import hubproof
        from orch.hubclient import wire

        owner = getattr(self.person, "owner", self.person)  # a new device of the owner: the owner's PK signs
        now = int(self.served.lab.ws.clock[0] * 1000)
        kw: dict[str, Any] = {
            "workspace_id": self.served.wid, "pk_pub": owner.pk_pub, "op": prepared["op"],
            "payload": hubproof.payload_of(prepared["events"][0]), "request_id": prepared["request"]["id"],
            "issued_ms": now, "expires_ms": now + 600_000, "dev": prepared["owner_proof"]["dev"],
            "sign": owner.pk_sign,
        }  # fmt: skip
        return wire.build_owner_proof(**{**kw, **over})

    def sign(self, prepared: dict[str, Any], *, proof: Any = "auto") -> signer.Signed:
        self.check(prepared)
        if proof == "auto":
            proof = self.owner_proof(prepared) if prepared["op"] in ("hub.join", "hub.mode", "hub.leave") else None
        return signer.sign(prepared, backend=self.backend, key_id="dk", workspace_id=self.served.wid, owner_proof=proof)

    def submit_res(self, request_id: str, events: list[dict[str, Any]], idem: str | bool = True) -> dict[str, Any]:
        return self.c.req("human.submit", {"request_id": request_id, "events": events}, idem=idem)

    def run(self, op: str, args: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], signer.Signed]:
        prepared = self.prepare(op, args)
        signed = self.sign(prepared)
        return prepared, ok(self.submit_res(prepared["request"]["id"], signed.events)), signed

    def get(self, request_id: str, **kw: Any) -> dict[str, Any]:
        body: dict[str, Any] = {"request_id": request_id}
        if "signer" in kw:
            body["signer"] = kw.pop("signer")
        return self.c.req("human.get", body, **kw)

    def ticket(self, title: str = "A ticket") -> tuple[str, str]:
        return self.served.ticket(title)

    def agent(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        return ok(self.c.req(op, args, grant=self.served.grant, session=SESSION, idem=True))


def unsigned(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in e.items() if k != "sig"} for e in copy.deepcopy(events)]


def sync_dev_store(served: Any) -> None:
    """#1198 C9: one source for the dev state. When ``hubops.workspace_dev`` says the served workspace is a dev
    workspace (a test patches it), its store replays as dev too (the model compares a hub proof's ``dev`` marker with
    the store's own ``hub_dev``), and so do the test's own stores (``lab.ws.other()``). Per instance, no class patch."""
    from orch.host import hubops

    service = served.service
    if not hubops.workspace_dev(service):
        return

    def make_dev(store: Any) -> None:
        store.hub_dev = True
        store._needs_reload = True  # noqa: SLF001
        store.refresh()

    served.lab.ws.hub_dev = True
    service.submit(make_dev, service.store).result()


@pytest.fixture
def lab(tmp_path):
    import errno

    value = Lab(tmp_path)
    try:
        yield value
    except OSError as exc:  # pragma: no cover
        if exc.errno == errno.EPERM:
            pytest.skip("Sandbox forbids AF_UNIX bind (EPERM)")
        raise
    finally:
        value.close()


@pytest.fixture
def served(lab):
    s = Served(lab)
    try:
        sync_dev_store(s)
        yield s
    finally:
        s.close()


@pytest.fixture
def human(served):
    return Human(served)
