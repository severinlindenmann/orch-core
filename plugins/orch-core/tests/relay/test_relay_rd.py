"""H12d-3 (orch-v2-host §0.8.6 item 9.5, 9.11): column R is served against the device's current reader declaration.

While ``required_reader`` is at least 1, every column-R request but ``reader.declare`` must carry ``rd`` naming the
device's current reader record at the required level (``Call.authorize`` -> ``readerops.check_rd``), else
``sign.convenience_readers`` with ``reason`` ``no_declaration`` / ``declaration_unknown`` / ``reader_too_old``
(``required``, ``declared``), sealed with those closed fields. ``reader.declare`` is served in column R for the
requesting device only (``via:"declare"``, the ``view`` surface), exempt from the check."""

from __future__ import annotations

from typing import Any

import pytest

from orch.host.relayentry import RelayEntry, register
from orch.identity.reader import decl_digest
from orch.relay import framing
from orch.relay.bridge import _refusal_fields
from orch.relay.framing import Identity, Refusal
from orch.store.convmark import ensure_convenience_mark
from tests.host.human_helpers import lab, served  # noqa: F401 - fixtures
from tests.identity.helpers import Person
from tests.identity.reader_helpers import decl

UID = __import__("os").getuid()


def _ident(served, person, rd: str | None = None) -> Identity:  # noqa: F811
    return Identity(served.wid, person.device, person.ref, "sha256:" + "1" * 64, 2, "sha256:" + "4" * 64, rd)


def _entry(served) -> RelayEntry:  # noqa: F811
    from orch.host.dispatch import RegistryBackend

    register(served.service)
    return RelayEntry(served.service, RegistryBackend())


def _read(served, entry, ident, op="whoami", args=None) -> Any:  # noqa: F811
    return served.service.submit(lambda: entry.read(ident, op, args or {}, lambda: True)).result(60)


def _refused(served, entry, ident, op="whoami", args=None) -> Refusal:  # noqa: F811
    with pytest.raises(Refusal) as exc:
        _read(served, entry, ident, op, args)
    return exc.value


def _now(served) -> int:  # noqa: F811
    return int(served.service.clock() * 1000)


def _phone_decl(served, p: Person, level: int = 1, *, created_ms: int | None = None, surfaces=("view",)):  # noqa: F811
    return decl(p, served.wid, level=level, via="declare", created_ms=created_ms or _now(served), surfaces=surfaces)


def _mark(served) -> None:  # noqa: F811
    ensure_convenience_mark(served.lab.state, served.wid, UID)


def test_without_convenience_column_r_is_served_without_rd(served):  # noqa: F811
    entry, owner = _entry(served), served.lab.ws.owner
    assert _read(served, entry, _ident(served, owner))["data"]["person"] == owner.ref


def test_with_convenience_a_request_without_rd_is_no_declaration(served):  # noqa: F811
    entry, owner = _entry(served), served.lab.ws.owner
    _mark(served)
    r = _refused(served, entry, _ident(served, owner))
    assert (r.code, r.fields) == ("sign.convenience_readers", {"reason": "no_declaration"})
    assert framing.refusal_meta(**_refusal_fields(r)) == {
        "refusal": "sign.convenience_readers", "retryable": False, "reason": "no_declaration",
    }  # fmt: skip


def test_reader_declare_is_served_without_rd_and_its_digest_then_serves(served):  # noqa: F811
    entry, owner = _entry(served), served.lab.ws.owner
    _mark(served)
    d = _phone_decl(served, owner)
    got = _read(served, entry, _ident(served, owner), "reader.declare", {"reader_decl": d})["data"]
    assert got == {"device": owner.device, "reader": 1, "level": 1, "digest": decl_digest(d)}
    # the same declaration again: the same answer (idempotent on the digest, no idem)
    again = _read(served, entry, _ident(served, owner), "reader.declare", {"reader_decl": d})["data"]
    assert again == got
    assert _read(served, entry, _ident(served, owner, got["digest"]))["data"]["person"] == owner.ref
    r = _refused(served, entry, _ident(served, owner, "sha256:" + "9" * 64))
    assert r.fields == {"reason": "declaration_unknown"}


def test_a_lower_newer_declaration_is_reader_too_old_and_the_old_rd_unknown(served):  # noqa: F811
    entry, owner = _entry(served), served.lab.ws.owner
    _mark(served)
    one = _phone_decl(served, owner, created_ms=_now(served) - 1000)
    zero = _phone_decl(served, owner, 0)
    _read(served, entry, _ident(served, owner), "reader.declare", {"reader_decl": one})
    got = _read(served, entry, _ident(served, owner), "reader.declare", {"reader_decl": zero})["data"]
    assert (got["level"], got["digest"]) == (0, decl_digest(zero))
    r = _refused(served, entry, _ident(served, owner, decl_digest(zero)))
    assert (r.code, r.fields) == (
        "sign.convenience_readers", {"reason": "reader_too_old", "required": 1, "declared": 0}
    )  # fmt: skip
    meta = framing.refusal_meta(**_refusal_fields(r))
    assert (meta["reason"], meta["required"], meta["declared"]) == ("reader_too_old", 1, 0)
    assert _refused(served, entry, _ident(served, owner, decl_digest(one))).fields == {"reason": "declaration_unknown"}


def test_the_flag_alone_requires_rd(served):  # noqa: F811
    from orch.host import convenience

    entry, owner = _entry(served), served.lab.ws.owner
    convenience.set_enabled(served.lab.state, served.wid, True, UID)
    assert _refused(served, entry, _ident(served, owner)).fields == {"reason": "no_declaration"}


def test_reader_list_and_other_reads_are_gated_too(served):  # noqa: F811
    entry, owner = _entry(served), served.lab.ws.owner
    _mark(served)
    for op in ("reader.list", "member.list", "workspace.show"):
        assert _refused(served, entry, _ident(served, owner), op).code == "sign.convenience_readers", op


def test_a_device_declares_only_for_itself(served):  # noqa: F811
    entry, owner = _entry(served), served.lab.ws.owner
    other = Person()
    r = _refused(served, entry, _ident(served, owner), "reader.declare", {"reader_decl": _phone_decl(served, other)})
    assert r.code == "op.not_allowed"


@pytest.mark.parametrize(
    ("change", "reason"),
    [({"surfaces": ("log",)}, "kat_missing"), ({"created_ms": 1}, "time")],
)
def test_a_bad_phone_declaration_is_reader_decl_invalid_with_its_reason(served, change, reason):  # noqa: F811
    entry, owner = _entry(served), served.lab.ws.owner
    d = _phone_decl(served, owner, **change)
    r = _refused(served, entry, _ident(served, owner), "reader.declare", {"reader_decl": d})
    assert (r.code, r.fields) == ("reader.decl_invalid", {"reason": reason})
    assert framing.refusal_meta(**_refusal_fields(r))["reason"] == reason


def test_reader_declare_in_column_r_needs_args(served):  # noqa: F811
    entry, owner = _entry(served), served.lab.ws.owner
    assert _refused(served, entry, _ident(served, owner), "reader.declare", {}).code == "invalid.input"


def test_the_refusal_fields_are_closed():
    with pytest.raises(ValueError):
        framing.refusal_meta("sign.convenience_readers", reason="relay_too_old")
    with pytest.raises(ValueError):
        framing.refusal_meta("sign.convenience_readers", reason="reader_too_old")  # needs required and declared
    with pytest.raises(ValueError):
        framing.refusal_meta("sign.convenience_readers", reason="no_declaration", required=1, declared=0)
    with pytest.raises(ValueError):
        framing.refusal_meta("internal", reason="no_declaration")
    with pytest.raises(ValueError):
        framing.refusal_meta("stale_epoch", epoch=1, reason="no_declaration")
    with pytest.raises(ValueError):
        framing.refusal_meta("sign.convenience_readers", reason="reader_too_old", required=True, declared=0)
    assert _refusal_fields(Refusal("sign.convenience_readers", reason="bogus")) == {
        "code": "sign.convenience_readers", "effects_possible": False, "before_bound": False,
    }  # fmt: skip
    assert "reason" not in _refusal_fields(Refusal("sign.convenience_readers", reason="reader_too_old"))
    assert "reason" not in _refusal_fields(Refusal("internal", reason="no_declaration"))
