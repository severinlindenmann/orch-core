"""W34b: one exported set of network-bound ops, built from the same table that runs them off the worker."""

from orch.host import dispatch
from orch.host.appops import NETWORK_OPS as APP
from orch.host.hubops import NETWORK_OPS as HUB
from orch.host.marketops import NETWORK_OPS as MARKET
from orch.host.orgops import NETWORK_OPS as ORG
from orch.host.peerops import NETWORK_OPS as PEER
from orch.host.publishing import NETWORK_OPS as PUBLISH
from orch.host.shareops import NETWORK_OPS as SHARE
from orch.host.spaceops import NETWORK_OPS as SPACE


def test_the_set_is_the_union_of_the_module_network_ops_and_publish_list():
    union = set().union(PUBLISH, APP, HUB, SHARE, MARKET, SPACE, ORG, PEER) | {"publish.list"}
    assert dispatch.NETWORK_BOUND_OPS == union
    assert {"publish.send", "hub.refresh", "peer.send", "share.sync", "app.send", "org.sync"} <= union


def test_every_second_phase_op_that_runs_a_network_execute_is_in_the_set():
    """A new network op added the normal way (a module NETWORK_OPS + its second phase) cannot be left out."""
    network_modules = set(dispatch.NETWORK_GROUPS)
    assert network_modules == {
        "publishing",
        "appops",
        "hubops",
        "shareops",
        "marketops",
        "spaceops",
        "orgops",
        "peerops",
    }
    grouped = {op for ops in dispatch.NETWORK_GROUPS.values() for op in ops}
    assert grouped == dispatch.NETWORK_BOUND_OPS
    assert grouped <= set(dispatch.SECOND_PHASE)
