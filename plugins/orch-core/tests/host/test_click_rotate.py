"""#1198 C13: ``device.rotate`` (replace this computer's device key) by click.

The recovery code and the passphrase of the new key are typed once in the page and sealed to the signer's per-offer
key (purposes ``device.recovery_code`` and ``custody.passphrase``). The signer opens them bound to workspace, request
and review, makes the certificate with the person key (wiped at once), has the NEW key sign the one ``device.rotated``,
and installs the new key. Adversarial cases: another person's code, an envelope for another request / workspace /
review, a missing or extra seal, a replay, convenience custody, a host that builds another rotation, secrets in logs,
state and results."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from orch import crypto
from orch.click import sealed
from orch.custody import personkey, rotate
from orch.custody.passphrase import KdfParams, PassphraseBackend
from orch.host import convenience
from orch.identity import recovery as rec
from orch.ops import human
from tests.host import human_helpers
from tests.host.test_click import Click, refused

lab, served = human_helpers.lab, human_helpers.served

FAST = 2**10
CODE = rec.code_from_entropy(bytes(range(32)))
OTHER = rec.code_from_entropy(bytes(range(1, 33)))
OLD_PW = "orbit candle meadow zipper falcon"
NEW_PW = "violet harbour 7 kettles juggle quietly!"


def backend_over(directory):
    return PassphraseBackend(Path(directory), _passphrase_provider=lambda r: OLD_PW, _kdf=KdfParams(2**15, 8, 1))


@pytest.fixture(autouse=True)
def fast_world(monkeypatch):
    """The owner's person key is the one of ``CODE`` (derived at a low scrypt N)."""
    from tests.identity import helpers
    from tests.store import helpers as store_helpers

    monkeypatch.setattr(personkey, "_derive_scalar", lambda text: rec._scalar(text, FAST))
    original = helpers.Person
    made = []

    class CodePerson(original):
        def __init__(self):
            super().__init__()
            if not made:
                made.append(self)
                self.pk = rec._key_at(CODE, FAST)
                self.pk_pub = crypto.public_bytes(self.pk)

    monkeypatch.setattr(helpers, "Person", CodePerson)
    monkeypatch.setattr(store_helpers, "Person", CodePerson)
    monkeypatch.setattr(human, "open_backend", backend_over)


@pytest.fixture
def click(served):
    """A click signer with One click OFF and the owner's device key as a passphrase key file (where setup puts it)."""
    owner = served.lab.ws.owner
    directory = human.keys_dir(served.lab.state, served.wid)
    directory.parent.mkdir(parents=True, exist_ok=True)
    backend_over(directory).create("dk", secret=crypto.private_scalar(owner.dk_sig), role="device", passphrase=OLD_PW)
    c = Click(served, enable=False)
    c.signer.wall = lambda: float(served.lab.ws.clock[0])  # the wrong-code record wants float times
    c.keys = directory
    yield c
    c.close()


def begin(click, served):
    return click.signer.rotate({"workspace_id": served.wid, "begin": True})


def seal(offer, served, *, code=CODE, pw=NEW_PW, rid=None, wid=None, digest=None, swap=False):
    pk = offer["view"]["seal"]["pk"]

    def ctx(purpose):
        return sealed.Context(purpose, wid or served.wid, rid or offer["request_id"], digest or offer["view_digest"])

    a = "custody.passphrase" if swap else "device.recovery_code"
    b = "device.recovery_code" if swap else "custody.passphrase"
    return {
        "device.recovery_code": sealed.seal(pk, code, ctx(a)),
        "custody.passphrase": sealed.seal(pk, pw, ctx(b)),
    }


def go(click, served, offer, env):
    return click.signer.rotate(
        {
            "workspace_id": served.wid,
            "request_id": offer["request_id"],
            "view_digest": offer["view_digest"],
            "sealed": env,
        }
    )


def roster(served):
    served.service.store.refresh()
    return served.service.store.state.workspace.devices


def key_pub(click):
    return backend_over(click.keys).public_key("dk")


def test_the_review_names_the_old_and_the_new_key_and_asks_for_the_two_secrets(click, served):
    owner = served.lab.ws.owner
    offer = begin(click, served)
    v = offer["view"]
    assert v["op"] == "device.rotate" and v["old"] == owner.device and v["new"] != owner.device
    assert v["seal"]["purposes"] == ["device.recovery_code", "custody.passphrase"] and "pk" in v["seal"]
    assert f"old device: {owner.device}" in v["lines"] and f"new device: {v['new']}" in v["lines"]
    assert v["rotate_line"] in v["lines"] and v["confirm"] == {"kind": "click"}
    assert offer["view_digest"].startswith("sha256:") and set(offer) == {
        "request_id",
        "view_digest",
        "view",
        "expires_in",
    }
    assert begin(click, served)["view"]["new"] != v["new"]  # a newer offer replaces the older: one key per offer
    assert len(click.signer._rotations) == 1


def test_the_signer_advertises_the_feature():
    from orch.click.protocol import FEATURES, OPS

    assert "rotate" in OPS and "device_rotate" in FEATURES


def test_rotate_by_click_replaces_the_key_and_leaves_no_secret(click, served, tmp_path):
    owner = served.lab.ws.owner
    old_pub = key_pub(click)
    offer = begin(click, served)
    res = go(click, served, offer, seal(offer, served))
    assert res["done"] is True and res["old"] == owner.device and res["new"] == offer["view"]["new"]
    assert roster(served)[owner.device].removed and not roster(served)[res["new"]].removed
    assert not roster(served)[res["new"]].exposed
    assert key_pub(click) != old_pub and key_pub(click) == crypto.unb64u(
        crypto.b64u(key_pub(click)), crypto.PUB_LEN
    )  # THE key here is the new one
    assert backend_over(click.keys.parent / rotate.STAGE).__class__ is PassphraseBackend
    assert not (click.keys.parent / rotate.STAGE / human.KEY_FILE).exists()
    assert not click.signer._rotations  # the offer and its scalar are gone
    # nothing secret anywhere: the result, the signer's log and every file under the state directory
    blob = repr(res)
    for root, _, names in os.walk(served.lab.state):
        for n in names:
            try:
                blob += Path(root, n).read_text(errors="ignore")
            except OSError:
                pass
    for secret in (CODE, NEW_PW, *CODE.split()[:3]):
        assert secret not in blob


def test_the_new_key_unlocks_with_the_chosen_passphrase(click, served):
    offer = begin(click, served)
    go(click, served, offer, seal(offer, served))
    new = PassphraseBackend(click.keys, _passphrase_provider=lambda r: NEW_PW, _kdf=KdfParams(2**15, 8, 1))
    assert new.public_key("dk") is not None
    from orch.identity import device_ref

    assert device_ref(new.public_key("dk")) == offer["view"]["new"]


def test_another_persons_recovery_code_changes_nothing_and_burns_the_offer(click, served):
    owner = served.lab.ws.owner
    old_pub = key_pub(click)
    offer = begin(click, served)
    err = refused(
        lambda: go(click, served, offer, seal(offer, served, code=OTHER)), "human.click_refused", "check_failed"
    )
    assert "not yours" in err.message and OTHER not in str(err) and CODE not in str(err)
    assert not roster(served)[owner.device].removed and key_pub(click) == old_pub
    refused(lambda: go(click, served, offer, seal(offer, served)), "invalid.input")  # the offer is used up
    again = begin(click, served)
    assert go(click, served, again, seal(again, served))["done"] is True


def test_a_mistyped_recovery_code_is_refused_in_plain_words(click, served):
    offer = begin(click, served)
    words = CODE.split()
    bad = " ".join(words[:-1] + ["zoo" if words[-1] != "zoo" else "abandon"])
    err = refused(
        lambda: go(click, served, offer, seal(offer, served, code=bad)), "human.click_refused", "check_failed"
    )
    assert "not a recovery code" in err.message


@pytest.mark.parametrize("what", ["request", "workspace", "digest", "swapped"])
def test_an_envelope_made_for_another_request_workspace_review_or_purpose_is_refused(click, served, what):
    owner = served.lab.ws.owner
    offer = begin(click, served)
    kw = {
        "request": {"rid": "hr_" + "0" * 26},
        "workspace": {"wid": "0" * 32},
        "digest": {"digest": "sha256:" + "0" * 64},
        "swapped": {"swap": True},
    }[what]
    refused(lambda: go(click, served, offer, seal(offer, served, **kw)), "invalid.input")
    assert not roster(served)[owner.device].removed
    # nothing that failed to open was spent: the very same offer still works with right envelopes
    assert go(click, served, offer, seal(offer, served))["done"] is True


def test_an_envelope_sealed_to_another_offers_key_is_refused(click, served):
    offer = begin(click, served)
    other = begin(click, served)  # replaces the first offer
    refused(lambda: go(click, served, offer, seal(offer, served)), "invalid.input")  # the first one is gone
    env = seal(other, served, rid=other["request_id"])
    env["device.recovery_code"] = sealed.seal(
        offer["view"]["seal"]["pk"],
        CODE,
        sealed.Context("device.recovery_code", served.wid, other["request_id"], other["view_digest"]),
    )
    refused(lambda: go(click, served, other, env), "invalid.input")


def test_a_missing_extra_or_malformed_seal_is_refused_before_anything_is_opened(click, served):
    offer = begin(click, served)
    env = seal(offer, served)
    rid, vd = offer["request_id"], offer["view_digest"]
    for bad in (
        None,
        {},
        {"device.recovery_code": env["device.recovery_code"]},
        {"custody.passphrase": env["custody.passphrase"]},
        {**env, "hub.backup_key": env["device.recovery_code"]},
        {"device.recovery_code": "text", "custody.passphrase": env["custody.passphrase"]},
    ):
        refused(
            lambda bad=bad: click.signer.rotate(
                {"workspace_id": served.wid, "request_id": rid, "view_digest": vd, "sealed": bad}
            ),
            "invalid.input",
        )
    refused(  # a clear value is never taken
        lambda: click.signer.rotate(
            {"workspace_id": served.wid, "request_id": rid, "view_digest": vd, "sealed": env, "passphrase": NEW_PW}
        ),
        "invalid.input",
    )
    assert go(click, served, offer, env)["done"] is True  # the envelopes were never opened


def test_a_replayed_request_signs_nothing(click, served):
    offer = begin(click, served)
    env = seal(offer, served)
    go(click, served, offer, env)
    seq = served.service.store.log_seq("workspace")
    refused(lambda: go(click, served, offer, env), "invalid.input")
    assert served.service.store.log_seq("workspace") == seq


def test_the_review_digest_must_be_the_offered_one(click, served):
    offer = begin(click, served)
    refused(
        lambda: click.signer.rotate(
            {
                "workspace_id": served.wid,
                "request_id": offer["request_id"],
                "view_digest": "sha256:" + "1" * 64,
                "sealed": seal(offer, served),
            }
        ),
        "invalid.input",
    )


def test_an_expired_offer_is_refused(click, served):
    offer = begin(click, served)
    click.mono.t += 10_000
    refused(lambda: go(click, served, offer, seal(offer, served)), "invalid.input")
    assert not click.signer._rotations


def test_a_weak_passphrase_changes_nothing(click, served):
    owner = served.lab.ws.owner
    offer = begin(click, served)
    refused(lambda: go(click, served, offer, seal(offer, served, pw="password")), "invalid.input")
    assert not roster(served)[owner.device].removed


def test_one_click_must_be_off(click, served):
    convenience.set_enabled(served.lab.state, served.wid, True, os.getuid())
    err = refused(lambda: begin(click, served), "invalid.input")
    assert "One click off" in err.message


def test_a_device_rotate_is_never_a_prepare(click, served):
    refused(lambda: click.prepare("device.rotate", {}), "human.click_refused", "terminal_only")


def test_a_host_that_builds_another_rotation_is_not_signed(click, served):
    owner = served.lab.ws.owner

    def tamper(op, args, result):
        if op == "human.prepare":
            result["data"]["events"][0]["old"] = "d_" + "0" * 32
        return result

    click.tamper = tamper
    offer = begin(click, served)
    with pytest.raises(Exception):  # noqa: B017 - a click refusal or a custody refusal, whichever check catches it first
        go(click, served, offer, seal(offer, served))
    assert not roster(served)[owner.device].removed and key_pub(click) == owner.sig_pub


def test_wrong_recovery_codes_are_rate_limited(click, served):
    from orch.click.signer import CUSTODY_TRIES

    for _ in range(CUSTODY_TRIES):
        offer = begin(click, served)
        refused(
            lambda offer=offer: go(click, served, offer, seal(offer, served, code=OTHER)),
            "human.click_refused",
            "check_failed",
        )
    offer = begin(click, served)
    refused(lambda: go(click, served, offer, seal(offer, served)), "rate.limited")


def test_a_run_that_landed_the_event_and_stopped_is_finished_by_the_next_begin(click, served, monkeypatch):
    owner = served.lab.ws.owner

    class Crash(Exception):
        pass

    def die(*a, **k):
        raise Crash

    monkeypatch.setattr(rotate, "_install", die)
    offer = begin(click, served)
    with pytest.raises(Crash):
        go(click, served, offer, seal(offer, served))
    assert roster(served)[owner.device].removed and key_pub(click) == owner.sig_pub  # event in, key not installed
    monkeypatch.undo()
    monkeypatch.setattr(human, "open_backend", backend_over)
    out = begin(click, served)
    assert out["done"] is True and out["old"] == owner.device and out["new"] == offer["view"]["new"]
    assert key_pub(click) != owner.sig_pub
