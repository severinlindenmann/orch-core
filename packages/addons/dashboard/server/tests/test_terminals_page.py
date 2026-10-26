"""The Terminals page (menu entry and route) is a session feature only while the Terminals add-on is ON and served."""

from test_shares import Host  # the shared stub host


def test_the_session_lists_terminals_page_only_when_the_addon_is_on_and_served(gateway, stub, monkeypatch):
    class Link(Host):
        def has(self, cap):
            return False

        def call(self, op, args=None, **kw):
            return {"addons": self.rows}

        def supports(self, op, cap):
            return self.served and cap in ("run/1", "term/1")

    link = Link()
    link.served = True
    monkeypatch.setattr(gateway, "enabled", lambda ws: link)
    link.rows = [{"name": "terminals", "state": "active"}]
    features = gateway.features(stub.ws)
    assert "terminals" in features and "terminals_page" in features
    for state in ("not_granted", "disabled", "changed", "missing", "purged"):
        link.rows = [{"name": "terminals", "state": state}]
        assert "terminals_page" not in gateway.features(stub.ws), state
    link.rows = [{"name": "terminals", "state": "active"}]
    link.served = False  # a host without run/1 and term/1: nothing to list
    assert "terminals_page" not in gateway.features(stub.ws)
