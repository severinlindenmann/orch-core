from orch_dashboard.hub import share_view


def test_share_delivery_closed_display_shape():
    got = share_view({
        "tickets_state": {"DEMO-1": "sent", "DEMO-2": "waiting", "DEMO-3": "not_sent",
                          "DEMO-4": "refused:too_large", "DEMO-5": "refused:raw secret", "DEMO-6": {"line": "secret"}},
        "not_sent_count": 1,
    }, {})
    assert got["tickets_state"] == {"DEMO-1": "sent", "DEMO-2": "waiting", "DEMO-3": "not_sent",
                                    "DEMO-4": "refused:too_large", "DEMO-5": "refused:unknown"}
    assert share_view({"tickets_state": {"bad\nkey": "sent", "DEMO-7": "garbage"}}, {})["tickets_state"] == {}
    assert got["not_sent_count"] == 1
    assert "secret" not in str(got)
    assert len(share_view({"tickets_state": {str(i): "sent" for i in range(250)}}, {})["tickets_state"]) == 200
    assert share_view({}, {})["not_sent_count"] == 0
