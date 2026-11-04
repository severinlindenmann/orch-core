"""GET /me/channels is scoped by the session user on the server (Opus condition 18): no parameter picks another person."""

import pytest
from webapi_helpers import Web


@pytest.fixture
def w(tmp_path):
    web = Web(tmp_path)
    yield web
    web.close()


def test_a_non_admin_sees_only_their_own_channels_whatever_they_ask(w):
    ann, admin = w.login(w.ann), w.login(w.admin)
    _, ann_channel = w.register_channel(ann)
    _, admin_channel = w.register_channel(admin)
    for query in ("", f"?user_id={w.admin}", f"?user={w.admin}"):
        status, body, _ = w.call("GET", "/me/channels" + query, token=ann)
        assert status == 200
        assert [c["channel_id"] for c in body["channels"]] == [ann_channel]
        assert {"channel_id", "kind", "label", "status", "created_ms"} <= body["channels"][0].keys()
    assert w.call("GET", "/admin/channels", token=ann)[0] == 403  # the all-channels list stays admin-only
    assert admin_channel not in str(w.call("GET", "/me/channels", token=ann)[1])
