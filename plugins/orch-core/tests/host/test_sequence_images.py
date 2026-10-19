# ruff: noqa: F811
"""R3 part B: a pasted screenshot in a ticket section. One review, the image added in the SAME signature run before the
section that shows it; the signer holds the bytes and the host never decodes (header check only)."""

from __future__ import annotations

import base64
import hashlib

import pytest

from orch import canon
from orch.click.protocol import ClickError
from orch.host import sequence
from orch.host.sequence import Aborted, SequenceInvalid
from tests.host import human_helpers
from tests.host.human_helpers import ok
from tests.host.test_click import Click, click, refused  # noqa: F401
from tests.host.test_imagecheck import chunk, png
from tests.host.test_sequence import Rig, intent_of
from tests.host.test_sequence_click import seq_args

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


def image(extra: bytes = b"", pad: int = 0):
    data = png(chunk(b"IDAT", b"\x00" * pad)) if pad else png()
    data += extra
    digest = canon.artifact_digest(data)
    name = f"paste-20261019-101500-{digest[7:15]}.png"
    return data, {"name": name, "sha256": digest, "bytes": len(data), "alt": "the broken page"}


def with_image(data_meta=None, **over):
    data, meta = data_meta or image()
    sections = {
        "summary": "One line.",
        "context": f"It looks like this:\n\n![the broken page](artifact:{meta['name']})",
    }
    return data, intent_of(sections=sections, images=[meta], **over)


# --- the intent ----------------------------------------------------------------------------------------------------


def test_an_image_step_comes_before_the_sections_and_before_nothing_it_needs():
    _, intent = with_image()
    plan = sequence.steps(sequence.validate_intent(intent))
    ops = [s["op"] for s in plan]
    assert ops == ["new", "set", "artifact.add", "section.set", "section.set"]
    assert plan[2]["args"]["kind"] == "screenshot" and plan[2]["kind"] == "image"


def test_an_image_no_section_shows_is_refused():
    _, meta = image()
    with pytest.raises(SequenceInvalid, match="shown by a written section"):
        sequence.validate_intent(intent_of(images=[meta]))


@pytest.mark.parametrize(
    "bad",
    [
        {"name": "../x.png"},
        {"name": "paste-20261019-101500-00000000.png"},  # does not carry the digest
        {"bytes": 5 * 1024 * 1024 + 1},
        {"bytes": 0},
        {"alt": ""},
        {"alt": "two\nlines"},
        {"sha256": "sha256:" + "0" * 63},
    ],
)
def test_a_malformed_image_entry_is_refused(bad):
    data, meta = image()
    meta = {**meta, **bad}
    with pytest.raises(SequenceInvalid):
        sequence.validate_intent(
            intent_of(sections={"summary": f"![x](artifact:{meta['name']})", "context": "c"}, images=[meta])
        )


def test_at_most_six_images():
    metas = [image(pad=i + 1)[1] for i in range(7)]
    text = "\n".join(f"![x](artifact:{m['name']})" for m in metas)
    with pytest.raises(SequenceInvalid, match="1 to 6"):
        sequence.validate_intent(intent_of(sections={"summary": text}, images=sorted(metas, key=lambda m: m["name"])))


def test_the_review_lists_the_image_with_its_full_digest():
    data, intent = with_image()
    lines = sequence.intent_lines(sequence.validate_intent(intent), lambda x: str(x))
    assert any(line.startswith("screenshot: paste-") and intent["images"][0]["sha256"] in line for line in lines)


# --- the one-click path --------------------------------------------------------------------------------------------


def stash(click_, data, meta=None):
    digest = canon.artifact_digest(data)
    step = 300_000
    for off in range(0, len(data), step):
        part = data[off : off + step]
        out = click_.signer.stash(
            {
                "workspace_id": click_.served.wid,
                "sha256": digest,
                "size": len(data),
                "offset": off,
                "b64": base64.b64encode(part).decode(),
                "last": off + len(part) == len(data),
            }
        )
    return out


def test_one_click_signs_the_image_then_the_sections_and_the_ticket_shows_it(click, served, human):
    data, intent = with_image()
    assert stash(click, data)["state"] == "complete"
    intent, op, args, meta = seq_args(click, intent)
    p = click.prepare(op, args, sequence=meta)
    v = click.view(p["request_id"])
    content = v["view"]["content"]
    assert content["images"] == [
        {
            "name": intent["images"][0]["name"],
            "sha256": canon.artifact_digest(data),
            "bytes": len(data),
            "alt": "the broken page",
        }
    ]
    assert any("Adds 1 screenshot" in n for n in content["notes"])
    click.mono.t += 2
    out = click.sign(p["request_id"], v["view_digest"])
    assert click.signatures == 5  # new, set, the file, two sections: one click
    key = out["result"]["key"]
    got = ok(served.client().req("ticket.get", {"ref": key}))["data"]
    assert intent["images"][0]["name"] in got["doc"]["sections"]["context"]
    arts = got["doc"].get("artifacts") or got.get("artifacts")
    assert arts and arts[0]["name"] == intent["images"][0]["name"] and arts[0]["kind"] == "screenshot"
    stored = ok(served.client().req("artifact.get", {"ref": key, "name": arts[0]["name"], "offset": 0}))["data"]
    assert base64.b64decode(stored["b64"]) == data


def test_a_big_image_goes_to_the_host_as_a_blob(click, served, human):
    data, intent = with_image(image(pad=700_000))
    assert len(data) > 512 * 1024
    stash(click, data)
    _, op, args, meta = seq_args(click, intent)
    p = click.prepare(op, args, sequence=meta)
    v = click.view(p["request_id"])
    click.mono.t += 2
    click.sign(p["request_id"], v["view_digest"])
    assert click.signatures == 5


def test_prepare_without_the_uploaded_file_is_refused(click, served, human):
    data, intent = with_image()
    _, op, args, meta = seq_args(click, intent)
    with pytest.raises(ClickError, match="not uploaded"):
        click.prepare(op, args, sequence=meta)


def test_the_signer_refuses_bytes_that_do_not_match_the_digest(click, served, human):
    data, _ = image()
    out = None
    with pytest.raises(ClickError, match="digest"):
        out = click.signer.stash(
            {
                "workspace_id": served.wid,
                "sha256": "sha256:" + hashlib.sha256(b"other").hexdigest(),
                "size": len(data),
                "offset": 0,
                "b64": base64.b64encode(data).decode(),
                "last": True,
            }
        )
    assert out is None


@pytest.mark.parametrize(
    "bad,why",
    [
        (b'<svg xmlns="http://www.w3.org/2000/svg"/>', "SVG"),
        (png(chunk(b"tEXt", b"Comment\x00GPS")), "metadata"),
        (png(tail=b"MZ-polyglot"), "after its end"),
    ],
)
def test_the_signer_refuses_a_file_the_header_check_refuses(click, served, human, bad, why):
    digest = canon.artifact_digest(bad)
    with pytest.raises(ClickError, match=why):
        click.signer.stash(
            {
                "workspace_id": served.wid,
                "sha256": digest,
                "size": len(bad),
                "offset": 0,
                "b64": base64.b64encode(bad).decode(),
                "last": True,
            }
        )


def test_stash_size_and_order_caps(click, served, human):
    data, _ = image()
    base = {"workspace_id": served.wid, "sha256": canon.artifact_digest(data), "size": len(data)}
    with pytest.raises(ClickError, match="5 MiB"):
        click.signer.stash({**base, "size": 5 * 1024 * 1024 + 1, "offset": 0, "b64": "AAAA", "last": False})
    with pytest.raises(ClickError, match="offset 0"):
        click.signer.stash({**base, "offset": 10, "b64": "AAAA", "last": False})
    click.signer.stash({**base, "offset": 0, "b64": base64.b64encode(data[:10]).decode(), "last": False})
    with pytest.raises(ClickError, match="in order"):
        click.signer.stash({**base, "offset": 20, "b64": "AAAA", "last": False})
    with pytest.raises(ClickError, match="differs"):
        click.signer.stash({**base, "offset": 0, "b64": base64.b64encode(b"x" * 10).decode(), "last": False})


def test_the_host_refuses_a_poisoned_file_in_a_sequence_even_from_a_client_that_did_not_check(human, tmp_path):
    """The host is the second line: a client that skipped the signer's check cannot add a PNG with a text chunk."""
    bad = png(chunk(b"tEXt", b"GPS"))
    digest = canon.artifact_digest(bad)
    meta = {"name": f"paste-20261019-101500-{digest[7:15]}.png", "sha256": digest, "bytes": len(bad), "alt": "x"}
    rig = Rig(human, tmp_path, intent_of(sections={"summary": f"![x](artifact:{meta['name']})"}, images=[meta]))
    first = rig.first()
    human.check(first)
    # the driver would stop at its own check; ask the host directly for the file step of a later position
    key = first["material"]["ticket"]["key"]
    args = {"ref": key, "path": meta["name"], "name": meta["name"], "kind": "screenshot", "label": "x"}
    res = human.c.req(
        "human.prepare",
        {
            "op": "artifact.add",
            "args": args,
            "signer": human.signer,
            "sequence": {"step": 1, "of": 5, "digest": rig.meta0()["digest"], "prev": "0" * 26, "ticket": key},
        },
        files={"path": {"b64": base64.b64encode(bad).decode()}},
    )
    assert "error" in res and "metadata" in str(res["error"])


def test_the_driver_aborts_before_any_signature_when_the_bytes_are_not_the_reviewed_ones(human, tmp_path):
    data, intent = with_image()
    rig = Rig(human, tmp_path, intent)
    first = rig.first()
    human.check(first)
    swapped = bytearray(data)
    swapped[-20] ^= 1
    drv = sequence.Driver(
        rig.port, rig.sessions, workspace_id=rig.wid, person=rig.person, intent=rig.intent,
        images={rig.intent["images"][0]["name"]: bytes(swapped)},
    )  # fmt: skip
    with pytest.raises(Aborted, match="not the file you reviewed"):
        drv.run(first)
    assert rig.submitted == []
    with pytest.raises(Aborted, match="not here"):  # no bytes at all (the terminal): nothing is signed
        sequence.Driver(rig.port, rig.sessions, workspace_id=rig.wid, person=rig.person, intent=rig.intent).run(first)
    assert rig.submitted == []


def test_the_terminal_refuses_an_intent_with_images(human, tmp_path):
    from orch.host import sequence_terminal

    _, intent = with_image()
    rig = Rig(human, tmp_path, intent)
    first = rig.first()
    with pytest.raises(Exception, match="click in the dashboard"):
        sequence_terminal.sign_sequence(
            call=lambda *a, **k: {"data": {}},
            backend=human.backend if hasattr(human, "backend") else None,
            key_id="k",
            workspace_id=rig.wid,
            person=rig.person,
            device=human.signer["device"],
            prepared=first,
            request_id=first["request"]["id"],
            sessions=rig.sessions,
            workspace=lambda w: None,
            prefix="D",
            confirm=lambda *a: True,
            new_idem=lambda: "a",
        )


def test_an_edit_may_start_with_the_image(click, served, human):
    made = click.prepare(*seq_args(click)[1:3], sequence=seq_args(click)[3])
    v = click.view(made["request_id"])
    click.mono.t += 2
    key = click.sign(made["request_id"], v["view_digest"])["result"]["key"]
    data0 = ok(served.client().req("ticket.get", {"ref": key}))["data"]
    data, meta = image()
    intent = {
        "v": 1,
        "kind": "edit_ticket",
        "ticket": {"key": key, "uid": data0["doc"]["uid"]},
        "ticket_type": "feature",
        "head": data0["doc"]["head"]["head"],
        "sections": {
            "summary": {"was": canon.section_hash("One line."), "text": f"Now:\n\n![x](artifact:{meta['name']})"}
        },
        "images": [meta],
    }
    stash(click, data)
    _, op, args, m = seq_args(click, intent)
    assert op == "artifact.add"
    p = click.prepare(op, args, sequence=m)
    v = click.view(p["request_id"])
    click.mono.t += 2
    before = click.signatures
    click.sign(p["request_id"], v["view_digest"])
    assert click.signatures == before + 2
    got = ok(served.client().req("ticket.get", {"ref": key}))["data"]
    assert meta["name"] in got["doc"]["sections"]["summary"]


def test_a_review_open_longer_than_the_stash_lifetime_still_signs_and_bytes_count_once(click, served, human):
    data, intent = with_image()
    stash(click, data)
    _, op, args, meta = seq_args(click, intent)
    p = click.prepare(op, args, sequence=meta)
    v = click.view(p["request_id"])
    held = next(iter(click.signer.stash_.values()))
    approval = next(iter(click.signer.approvals.values()))
    assert next(iter(approval.images.values())) is held["data"]  # shared, not copied
    click.signer.STASH_LIFETIME = 300.0
    click.mono.t += 250  # two waits of 250 are past the (shortened) lifetime: only the pin keeps it
    click.signer.stash(
        {
            "workspace_id": served.wid,
            "sha256": "sha256:" + "1" * 64,
            "size": 1,
            "offset": 0,
            "b64": "AA==",
            "last": False,
        }
    )
    click.mono.t += 250
    click.signer.stash(
        {
            "workspace_id": served.wid,
            "sha256": "sha256:" + "2" * 64,
            "size": 1,
            "offset": 0,
            "b64": "AA==",
            "last": False,
        }
    )
    assert (served.wid, canon.artifact_digest(data)) in click.signer.stash_  # pinned by the open review
    click.mono.t += 2
    click.sign(p["request_id"], v["view_digest"])
    assert click.signatures == 5


def test_a_first_step_that_names_no_held_screenshot_is_a_clear_refusal(click, served, human):
    data, intent = with_image()
    stash(click, data)
    _, op, args, meta = seq_args(click, intent)
    made = click.prepare(*seq_args(click)[1:3], sequence=seq_args(click)[3])
    v = click.view(made["request_id"])
    click.mono.t += 2
    key = click.sign(made["request_id"], v["view_digest"])["result"]["key"]
    d0 = ok(served.client().req("ticket.get", {"ref": key}))["data"]
    img, meta2 = image()
    stash(click, img)
    text = f"![x](artifact:{meta2['name']})"
    edit = {"v": 1, "kind": "edit_ticket", "ticket": {"key": key, "uid": d0["doc"]["uid"]}, "ticket_type": "feature",
            "head": d0["doc"]["head"]["head"],
            "sections": {"summary": {"was": canon.section_hash("One line."), "text": text}},
            "images": [meta2]}  # fmt: skip
    _, op, args, m = seq_args(click, edit)
    with pytest.raises(ClickError, match="does not carry"):
        click.prepare(op, {**args, "name": "paste-20261019-101500-00000000.png"}, sequence=m)


def test_upload_prepare_expire_cycles_stay_within_the_stash_cap(click, served, human):
    data, intent = with_image()
    stash(click, data)
    _, op, args, meta = seq_args(click, intent)
    click.prepare(op, args, sequence=meta)
    click.mono.t += 700  # the review has expired but nobody swept it: a new upload sweeps first
    click.signer.STASH_LIFETIME = 100.0
    click.signer.stash(
        {
            "workspace_id": served.wid,
            "sha256": "sha256:" + "3" * 64,
            "size": 1,
            "offset": 0,
            "b64": "AA==",
            "last": False,
        }
    )
    assert click.signer.approvals == {}  # swept
    assert (served.wid, canon.artifact_digest(data)) not in click.signer.stash_  # and its bytes are gone once
    held = sum(v["size"] for v in click.signer.stash_.values())
    assert held <= click.signer.STASH_BYTES and len(click.signer.stash_) <= click.signer.STASH_FILES
