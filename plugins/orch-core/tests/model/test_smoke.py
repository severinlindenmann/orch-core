from tests.model.world import World


def test_smoke():
    w = World().bootstrap({"mara": "maintainer"})
    uid = w.ticket()
    w.fill(uid)
    s = w.state()
    assert not s.chain_errors, s.chain_errors
    assert not s.workspace.invalid, s.workspace.invalid
    v = s.tickets[uid]
    assert v.status == "open"
    assert v.gates["requirements"].hash is not None
    print(v.gates["requirements"].hash)
