"""Manual acceptance is person-only evidence bound to the current criterion wording."""

import pytest

from orch.model import Code
from orch.model.accept import criterion_hash, label, parse_label
from orch.model.tasks import acceptance_state
from tests.model.world import World, refused
from tests.schema.examples import digest


def setup():
    w = World().bootstrap()
    uid = w.ticket()
    w.edit(uid, "sev", {"ticket.acceptance": [{"id": "AC1", "text": "works"}]})
    return w, uid


def artifact(**kw):
    return {
        "name": "accept-AC1-1.md",
        "kind": "feedback",
        "sha256": digest("statement"),
        "bytes": 1,
        "ac": "AC1",
        "label": label("AC1", "works"),
        **kw,
    }


def test_accept_edit_and_withdraw():
    w, uid = setup()
    gen = w.view(uid).gates["verify"].gen
    w.tev(uid, "artifact.added", "sev", **artifact())
    assert w.view(uid).gates["verify"].gen == gen + 1
    assert w.view(uid).acceptance[0].evidence == ("artifact:accept-AC1-1.md",)
    assert acceptance_state(w.state()._core.tickets[uid], "AC1") == "accepted"
    w.edit(uid, "sev", {"ticket.acceptance": [{"id": "AC1", "text": "works differently"}]})
    assert not w.view(uid).acceptance[0].evidence
    assert acceptance_state(w.state()._core.tickets[uid], "AC1") == "stale"
    gen = w.view(uid).gates["verify"].gen
    withdrawn = artifact(label="withdrawn AC1", sha256=digest("withdrawn"), replaces=digest("statement"))
    del withdrawn["ac"]
    w.tev(uid, "artifact.replaced", "sev", **withdrawn)
    assert w.view(uid).gates["verify"].gen == gen + 1
    assert w.state()._core.tickets[uid].artifacts[withdrawn["name"]].ac is None
    assert not w.view(uid).acceptance[0].evidence
    assert acceptance_state(w.state()._core.tickets[uid], "AC1") is None


@pytest.mark.parametrize(
    "bad",
    ["accepted AC1 bad", "accepted AC2 " + "0" * 16, "accepted AC1 " + "A" * 16, "accepted AC1 " + "0" * 16 + " "],
)
def test_malformed_or_mismatched_labels_never_count(bad):
    w, uid = setup()
    w.tev(uid, "artifact.added", "sev", **artifact(label=bad))
    assert not w.view(uid).acceptance[0].evidence


def test_agent_cannot_make_or_replace_feedback_but_ordinary_file_is_evidence():
    w, uid = setup()
    a = w.claim(uid)
    assert refused(w, uid, "artifact.added", a, **artifact()) == Code.ARTIFACT_KIND
    w.tev(uid, "artifact.added", a, **artifact(name="accept-AC1.md", kind="other"))
    assert w.view(uid).acceptance[0].evidence == ("artifact:accept-AC1.md",)
    assert acceptance_state(w.state()._core.tickets[uid], "AC1") is None
    assert (
        refused(w, uid, "artifact.replaced", a, **artifact(name="accept-AC1.md", replaces=digest("statement")))
        == Code.ARTIFACT_KIND
    )


def test_label_is_strict_and_hash_is_domain_separated():
    import hashlib

    assert criterion_hash("AC2", "text") == hashlib.sha256(b"orch.accept.v1\nAC2\ntext").hexdigest()[:16]
    value = label("AC2", "text")
    assert parse_label(value) == ("AC2", criterion_hash("AC2", "text"))
    for bad in (value + "\n", " " + value, value.upper(), value.replace("AC2", "AC02")):
        assert parse_label(bad) is None


def test_person_completion_ends_live_lease_without_evidence():
    w, uid = setup()
    w.edit(uid, "sev", {"ticket.tasks": [{"id": "T1", "text": "do it", "verify": None, "proves": ["AC1"]}]})
    actor = w.claim(uid)
    w.tev(uid, "task.started", actor, task="T1")
    assert w.view(uid).tasks[0].leased_by == actor["session"]
    w.tev(uid, "task.done", "sev", task="T1")
    assert w.view(uid).tasks[0].leased_by is None and w.view(uid).tasks[0].state == "done"
    assert not w.view(uid).acceptance[0].evidence
