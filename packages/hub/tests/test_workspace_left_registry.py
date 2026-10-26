"""Leaving preserves the timestamp; forgetting removes pins and aliases, but not counts."""

from orch_hub.link.registry import Registry


def test_workspace_left_registry(tmp_path):
    path = str(tmp_path / "registry.db")
    reg = Registry(path)
    ws, other = "ab" * 16, "cd" * 16
    bound = {"status": "bound", "binding": {"mode": "full"}}
    unbound = {"status": "unbound", "binding": {"mode": "full"}}
    obj = {"o": {"unbound_ms": 1730000000000}}
    try:
        assert reg.unbound_ms(ws) is None
        reg.commit_join(ws, "owner", "wsk", "pk", {"name": "Example"}, bound, owner_alias="user")
        reg.commit_join(other, "owner", "wsk", "pk", {}, bound, owner_alias="other-user")
        assert reg.unbound_ms(ws) is None
        reg.set_binding_state(ws, unbound)
        assert reg.unbound_ms(ws) is None
        reg.set_binding_state(ws, unbound, obj)
        assert reg.unbound_ms(ws) == obj["o"]["unbound_ms"]
        reg.set_binding_state(ws, bound)
        assert reg.unbound_ms(ws) is None
        reg.set_binding_state(ws, unbound, obj)
        reg.count_op(ws, "hub.unbound", True)
        reg.forget(ws)
        reg.forget(ws)  # Already absent is harmless.
    finally:
        reg.close()
    reg = Registry(path)
    try:
        assert ws not in reg.pins()
        assert reg.pin(ws) is None
        assert reg.card(ws) is None
        assert reg.binding_state(ws) is None
        assert reg.unbound_ms(ws) is None
        assert reg.owner_alias(ws) is None
        assert ws not in reg.binding_objects()
        assert reg.pin(other) is not None
        assert reg.owner_alias(other) == "other-user"
        assert reg._db.execute("SELECT count FROM op_counts WHERE ws=?", (ws,)).fetchone()[0] == 1
    finally:
        reg.close()
