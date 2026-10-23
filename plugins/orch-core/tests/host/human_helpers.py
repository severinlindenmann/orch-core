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

    def check(self, prepared: dict[str, Any], **kw: Any) -> None:
        signer.check(
            prepared,
            workspace_id=self.served.wid,
            person=kw.pop("person", self.person.ref),
            device=kw.pop("device", self.person.device),
            auth=kw.pop("auth", "passphrase"),
        )

    def sign(self, prepared: dict[str, Any]) -> signer.Signed:
        self.check(prepared)
        return signer.sign(prepared, backend=self.backend, key_id="dk", workspace_id=self.served.wid)

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
        yield s
    finally:
        s.close()


@pytest.fixture
def human(served):
    return Human(served)
