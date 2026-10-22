"""C3: ``accept_event_deliver`` resolves the actor of a verified custodial event through ``identity.hub_actor``."""

import pytest

from orch.hubclient import HubRefused
from orch.hubclient import custodial as C
from tests.hub.test_custodial import CASE, LOG, NOW, _state
from tests.model.test_hub_actor import join_other
from tests.model.test_identity_aliases import alias, core, join
from tests.model.test_identity_aliases_l2p4 import _hub_b
from tests.model.world import World

C_USER = CASE["event"]["actor"]["id"][2:]


@pytest.fixture
def w():
    return World().bootstrap({"mara": "maintainer", "tom": "member"})


def test_custodial_hub_actor_refuses_a_verified_event_whose_user_is_aliased_under_another_hub(w):
    """The event is perfectly signed by the hub this workspace is bound to; its user id is one ANOTHER hub aliased to
    mara. Verification passes the chain; ``hub_actor`` stops it before admission."""
    # A' (a random hub) aliases mara to the vector user id; then the workspace binds the vector hub, unaliased
    hub_a, root_a = _hub_b()
    assert not hasattr(join_other(w, [alias(w, "mara", C_USER)], hub_a, root_a, 1)[1], "code")
    w.wev("hub.left", "sev", hub_id=hub_a, binding_seq=1)
    assert not hasattr(join(w, [], seq=2)[1], "code")  # the vector hub, no aliases of its own
    admitted = []
    with pytest.raises(HubRefused) as e:
        C.accept_event_deliver(
            _state(), {"log": LOG, "event": CASE["event"]}, ws=core(w).ws, now_ms=NOW, dev_mode=True, min_factor="password+totp",  # noqa: E501
            admit=lambda *a: admitted.append(a),
        )  # fmt: skip
    assert e.value.code == "other_user" and admitted == []


def test_custodial_hub_actor_resolves_the_actor_handed_to_admit(w):
    join(w, [alias(w, "mara", C_USER)])
    seen = []
    C.accept_event_deliver(
        _state(), {"log": LOG, "event": CASE["event"]}, ws=core(w).ws, now_ms=NOW, dev_mode=True, min_factor="password+totp",  # noqa: E501
        admit=lambda actor, verified, log, event: seen.append((actor, verified["channel"], log)),
    )  # fmt: skip
    assert seen == [(w.people["mara"], CASE["event"]["channel"], LOG)]
