"""L2 phase-4 review (state/l2-review-p4.md, L2P4-C1): aliases are not scoped to the hub they were made for.

``user_id`` is 16 random bytes chosen by a hub (hub contract 2.3), so ``u_x`` names a user only together with a
``hub_id``. ``ws.hub["aliases"]`` survives ``hub.left`` and is carried into a join of another hub, and
``identity.canonical`` maps ``u_x`` to the aliased person whatever hub is joined now. Both tests are xfail(strict)
while the finding is open."""

import pytest

from orch.crypto import suite
from orch.crypto.encoding import b64u
from orch.hubclient import wire
from orch.model import identity
from tests.model.test_identity_aliases import HUB, ROOT, U_MARA, alias, core, join
from tests.model.world import World

U_MARA_ON_B = "5d0e3c8a0f1b2c3d4e5f60718293a4b5"


@pytest.fixture
def w():
    return World().bootstrap({"mara": "maintainer", "tom": "member"})


def _hub_b():
    pub = suite.public_bytes(suite.generate_private_key())
    return wire.hub_id(pub), b64u(pub)


def _switch_to_hub_b(w, aliases):
    _, ok = join(w, [alias(w, "mara", U_MARA)])
    assert not hasattr(ok, "code")
    w.wev("hub.left", "sev", hub_id=HUB, binding_seq=1)
    hub_b, root_b = _hub_b()
    assert hub_b != HUB and root_b != ROOT
    return _join_b(w, aliases, hub_b, root_b)


@pytest.mark.xfail(strict=True, reason="L2P4-C1: hub A's alias still maps u_<id> while hub B is joined")
def test_l2p4_c1_alias_of_a_left_hub_does_not_name_a_user_of_the_next_hub(w):
    _, r = _switch_to_hub_b(w, [])
    assert not hasattr(r, "code")
    ws = core(w).ws
    # hub B picks its own user ids; a hub-B user with hub A's id must not become mara (owner tokens, independence)
    assert identity.canonical(ws, "u_" + U_MARA) == "u_" + U_MARA


@pytest.mark.xfail(strict=True, reason="L2P4-C1: mara cannot alias her hub-B user: alias_repointed across hubs")
def test_l2p4_c1_owner_can_alias_their_user_on_a_new_hub(w):
    _, r = _switch_to_hub_b(w, [alias(w, "mara", U_MARA_ON_B)])
    assert not hasattr(r, "code"), r
    assert identity.canonical(core(w).ws, "u_" + U_MARA_ON_B) == w.people["mara"]


def _join_b(w, aliases, hub_b, root_b):
    return w.try_(
        "workspace",
        "hub.joined",
        "sev",
        hub_id=hub_b,
        hk_root_pub=root_b,
        mode="selective",
        binding_seq=2,
        aliases=sorted(aliases, key=lambda a: a["person_id"]),
    )
