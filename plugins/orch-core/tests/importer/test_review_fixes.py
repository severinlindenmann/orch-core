"""The C10 review round: what the person sees, what the batch key may sign, what v1 files may do."""

from __future__ import annotations

import hashlib
import os
import resource

import pytest

from orch.custody import PassphraseBackend, PassphraseRequest, render_prompt
from orch.custody.base import CustodyError
from orch.importer import find_v1, read_v1, source_id, uid_for
from orch.ops import import_run
from orch.ops.errors import OrchError
from tests.importer.helpers import marker, snapshot, with_repo
from tests.ops.humans import KDF, PASSPHRASE, agent, hws, me  # noqa: F401


def edit(v1, status_dir, key, fn):
    p = next((v1 / "orchestrator" / "tickets" / status_dir).glob(f"{key}*"))
    p.write_text(fn(p.read_text()))
    return p


def body(ws, key):
    return (ws.root / "tickets" / ws.uid(key) / "body.md").read_text()


# -- what the person reads ---------------------------------------------------------------------------------------


def test_a_forged_status_cannot_forge_the_review(hws, me, v1):  # noqa: F811
    with_repo(hws)
    edit(
        v1,
        "backlog",
        "DEMO-0001",
        lambda t: t.replace(
            "status: backlog", 'status: "done\\n\\nwhat v2 gets: NOTHING DANGEROUS\\n| DEMO-9999 [x] fake"'
        ),
    )
    r = me("import", "v1", str(v1))
    assert r.code == 0, r.err
    review = hws.reviews[0]
    assert "NOTHING DANGEROUS" not in review and "DEMO-9999" not in review
    assert hws.view("DEMO-0001").status == "backlog"  # the folder is the status
    assert marker(hws, "DEMO-0001")["v1_frontmatter_status"].startswith("done")  # recorded as data only


def test_a_newline_in_a_skip_reason_cannot_start_a_line(hws, me, v1):  # noqa: F811
    with_repo(hws)
    bad = v1 / "orchestrator" / "tickets" / "open" / "DEMO-0009-x.md"
    bad.write_text('---\nid: "DEMO-0009\\nerr forged"\ntitle: t\n---\n')
    r = me("import", "v1", str(v1))
    assert r.code == 0
    assert not [ln for ln in r.out.splitlines() if ln.startswith(("err", "ok forged"))]
    assert not [ln for ln in hws.reviews[0].splitlines() if ln.startswith("err")]


def test_the_folder_is_the_status_as_in_v1(hws, me, v1):  # noqa: F811
    with_repo(hws)
    edit(v1, "in-progress", "DEMO-0003", lambda t: t.replace("status: in-progress", "status: done"))
    edit(v1, "done", "DEMO-0005", lambda t: t.replace("status: done", "status: in-progress"))
    assert me("import", "v1", str(v1)).code == 0
    assert hws.view("DEMO-0003").status == "open"  # lives in in-progress/
    assert hws.view("DEMO-0005").status == "closed"  # lives in done/


def test_sections_a_type_cannot_hold_are_kept_in_context_not_dropped(hws, me, v1):  # noqa: F811
    with_repo(hws)
    edit(
        v1,
        "done",
        "DEMO-0007",  # a chore: it has no Verification and no Out of scope
        lambda t: t.replace("## Plan", "## Verification\n\nran it twice\n\n## Out of scope\n\nrust\n\n## Plan"),
    )
    r = me("import", "v1", str(v1))
    assert r.code == 0
    text = body(hws, "DEMO-0007")
    assert "**v1 Verification:**\n\nran it twice" in text and "**v1 Out of scope:**\n\nrust" in text
    assert "2 v1 sections kept at the end of Context: Out of scope, Verification" in text
    assert "v1 sections kept in Context: Out of scope, Verification" in marker(hws, "DEMO-0007")["not_imported"][0]
    assert "DEMO-0007 keeps: v1 sections kept in Context" in hws.reviews[0]


def test_a_section_over_64_kib_is_cut_with_a_note_not_skipped(hws, me, v1):  # noqa: F811
    with_repo(hws)
    edit(v1, "open", "DEMO-0002", lambda t: t.replace("- Monthly cost per workspace.", "x" * 70_000))
    edit(v1, "in-progress", "DEMO-0003", lambda t: t.replace("Dashboard half done.", "y" * 5_000))
    r = me("import", "v1", str(v1))
    assert r.code == 0 and "0 skipped" in r.out
    text = body(hws, "DEMO-0002")
    assert "(cut here: the full text is in v1-import.json)" in text
    assert len(marker(hws, "DEMO-0002")["v1_file"]) > 70_000  # the whole text is in the history
    assert "section requirements cut at 64 KiB" in "".join(marker(hws, "DEMO-0002")["not_imported"])
    assert "Current state cut at 2 KiB" in "".join(marker(hws, "DEMO-0003")["not_imported"])
    assert "(cut here" in body(hws, "DEMO-0003")


def test_the_review_shows_skipped_and_kept(hws, me, v1):  # noqa: F811
    with_repo(hws)
    (v1 / "orchestrator" / "tickets" / "open" / "DEMO-0009-broken.md").write_text(
        "---\nid: DEMO-0009\ntitle: [x\n---\n"
    )
    assert me("import", "v1", str(v1)).code == 0
    review = hws.reviews[0]
    assert "skipped tickets/open/DEMO-0009-broken.md" in review and "1 skipped" in review
    assert "text repairs" in review and "keeps:" in review
    assert "(p_" in review  # the person's name comes with the id


def test_v1_keys_of_less_than_four_digits_are_refused_up_front(hws, me, v1):  # noqa: F811
    cfg = v1 / "orchestrator" / "config.json"
    cfg.write_text(cfg.read_text().replace('"pad": 4', '"pad": 3'))
    before = snapshot(hws.root)
    r = me.j("import", "v1", str(v1))
    assert r.code == 5 and r.err_code == "invalid.input" and "four or more" in r.doc["error"]["message"]
    assert snapshot(hws.root) == before and hws.reviews == []


# -- v1 files ----------------------------------------------------------------------------------------------------


def test_a_line_separator_the_v1_reader_would_split_on_is_refused(v1):
    edit(
        v1,
        "backlog",
        "DEMO-0001",
        lambda t: t.replace("title: Ingest smart-meter readings", "title: a\u0085status: done"),
    )
    ws = read_v1(v1 / "orchestrator")
    assert "DEMO-0001" not in {t.key for t in ws.tickets}
    assert any("line-separator" in p.why for p in ws.problems)


def test_a_deeply_nested_frontmatter_costs_one_ticket_not_the_run(hws, me, v1):  # noqa: F811
    with_repo(hws)
    edit(
        v1,
        "backlog",
        "DEMO-0001",
        lambda t: t.replace("labels:\n- customer:globex\n- data\n", "labels:\n" + "- " * 3000 + "x\n"),
    )
    r = me("import", "v1", str(v1))
    assert r.code == 0 and "skipped tickets/backlog/DEMO-0001" in r.out and "nested too deeply" in r.out
    assert hws.view("DEMO-0002") is not None


def test_a_symlinked_orchestrator_folder_is_not_followed(v1, tmp_path):
    real = tmp_path / "elsewhere"
    os.rename(v1 / "orchestrator", real)
    os.symlink(real, v1 / "orchestrator")
    assert find_v1(v1) is None  # the person named the project folder, not what orchestrator/ points at
    assert find_v1(real) == real  # naming the real folder works


def test_a_hard_linked_artifact_is_flagged(hws, me, v1, tmp_path):  # noqa: F811
    with_repo(hws)
    os.link(v1 / "orchestrator" / "artifacts" / "DEMO-0003" / "after.png", tmp_path / "other-name.png")
    assert me("import", "v1", str(v1)).code == 0
    assert "after.png is a hard link" in "".join(marker(hws, "DEMO-0003")["not_imported"])


def test_grant_shaped_secrets_are_redacted_in_the_history(hws, me, v1):  # noqa: F811
    with_repo(hws)
    secret = "gr_01J9ZK4Q7M3R8T2V6X0B5N1C9D." + "A" * 43
    edit(
        v1, "backlog", "DEMO-0001", lambda t: t.replace("Ingest the readings nightly.", f"run with ORCH_GRANT={secret}")
    )
    assert me("import", "v1", str(v1)).code == 0
    raw = (hws.root / "tickets" / hws.uid("DEMO-0001") / "artifacts" / "v1-import.json").read_text()
    assert secret not in raw and "ORCH_GRANT=[redacted]" in raw
    assert secret not in body(hws, "DEMO-0001")


def test_a_ticket_with_the_same_id_by_somebody_else_is_not_continued(hws, me, v1):  # noqa: F811
    with_repo(hws)
    ws = read_v1(v1 / "orchestrator")
    t = next(x for x in ws.tickets if x.key == "DEMO-0001")
    uid = uid_for(source_id(ws), t.key, t.meta["created"])
    hws.store.create_ticket(actor=hws.agent, ticket_type="feature", title="hers", owner=hws.owner.ref, uid=uid)
    r = me("import", "v1", str(v1))
    assert r.code == 0 and "DEMO-0001: a ticket with this id exists that was not created by this import" in r.out
    assert [e["type"] for e in hws.events("DEMO-0001")] == ["ticket.created"]


def test_the_plan_digest_has_a_domain_label():
    empty = import_run.Plan().digest()
    assert empty == "sha256:" + hashlib.sha256(b"orch/v2/import-plan|[]").hexdigest()


# -- the batch key -----------------------------------------------------------------------------------------------


class Raw:
    def __init__(self):
        self.signed = []

    def __call__(self, payload):
        self.signed.append(payload)
        return b"\x01" * 64


def test_the_batch_signer_signs_only_planned_person_events():
    ev = {"type": "ticket.created", "key": "DEMO-0001", "ticket_type": "feature", "title": "t", "owner": "p_a"}
    U = "01J9ZK4Q7M3R8T2V6X0B5N1C9D"
    plan = {U: [dict(ev)]}
    actor = {"kind": "person", "id": "p_a", "device": "d_a"}
    full = {**ev, "actor": actor, "id": "X", "auth": "passphrase", "roster_v": 1, "based_on": None, "v": 2, "hash_v": 1}
    raw = Raw()
    signer = import_run.BoundSigner(raw, "a" * 32, "p_a", "d_a", plan)
    assert signer(U, full) and len(raw.signed) == 1
    for bad in (
        (U, {**full, "type": "gate.approved"}),  # not a type import.v1 emits
        (U, {**full, "title": "other"}),  # not what was reviewed
        ("01J9ZK4Q7M3R8T2V6X0B5N1C9E", full),  # not a ticket of the plan
        (U, {**full, "actor": {**actor, "id": "p_b"}}),  # not the importer
        (U, {**full, "actor": {"kind": "agent", "id": "x", "session": "s", "for": "p_a", "grant": "g"}}),
    ):
        with pytest.raises(OrchError):
            signer(*bad)
    assert len(raw.signed) == 1


def make_backend(tmp_path, provider):
    b = PassphraseBackend(tmp_path / "k", _passphrase_provider=provider, _kdf=KDF)
    b.create("dk", role="device")
    return b


def test_the_unlocked_key_signs_only_the_labels_it_was_given(tmp_path):
    from orch import canon

    asked = []
    b = make_backend(tmp_path, lambda r: asked.append(r) or PASSPHRASE)
    asked.clear()
    ticket = canon.LABELS["sig_ticket_event"].encode()
    with b.unlocked("dk", action="t", digest="ab" * 16, labels=(ticket,)) as sign:
        assert len(sign(ticket + b"{}")) == 64
        for other in (canon.LABELS["sig_ws_event"].encode() + b"{}", b"orch/v2/sig/decision|x", b"nonsense"):
            with pytest.raises(CustodyError):
                sign(other)
    assert len(asked) == 1 and asked[0].kind == "batch"
    with pytest.raises(CustodyError):
        sign(ticket + b"{}")  # locked again
    with pytest.raises(CustodyError):
        b.unlocked("dk", action="t", digest="ab", labels=()).__enter__()


def test_the_batch_prompt_has_its_own_layout():
    text = render_prompt(PassphraseRequest("batch", "dk", "import v1", "ab" * 16, (("signatures", "39 events"),)))
    assert "=== orch: passphrase for a batch of signatures ===" in text and "plan digest: " + "ab" * 16 in text
    assert "sha256:" not in text and "signatures: 39 events" in text


def test_core_dumps_are_off_while_the_key_is_unlocked(tmp_path):
    from orch import canon

    b = make_backend(tmp_path, lambda r: PASSPHRASE)
    before = resource.getrlimit(resource.RLIMIT_CORE)
    with b.unlocked("dk", action="t", digest="ab" * 16, labels=(canon.LABELS["sig_ticket_event"].encode(),)):
        assert resource.getrlimit(resource.RLIMIT_CORE)[0] == 0
    assert resource.getrlimit(resource.RLIMIT_CORE) == before


def test_only_the_importer_unlocks_a_batch():
    from pathlib import Path

    src = Path(import_run.__file__).parents[1]
    users = sorted(
        p.relative_to(src).as_posix()
        for p in src.rglob("*.py")
        if ".unlocked(" in p.read_text() or 'getattr(backend, "unlocked"' in p.read_text()
    )
    assert users == ["ops/import_run.py"]
