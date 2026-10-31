"""P02 (#1290): who the hub vouches for at orch-publish (``make_admission``), the "share files" setting, and the publish
address in ``hub_where``."""

from __future__ import annotations

import pytest

from orch_hub import settings as S
from orch_hub.contract.hub import Refused
from orch_hub.contract.where import check_hub_where, publish_ok
from orch_hub.serve import make_admission, publish_hint

HUB = "a" * 32
WS = "b" * 32
OWNER = "c" * 32
ADMIN, MEMBER = "d" * 32, "e" * 32


class Reg:
    def __init__(self, status="bound", hub=HUB, seq=3, alias=ADMIN, pinned=True):
        self.st = None if status is None else {"status": status, "binding_seq": seq, "binding": {"hub_id": hub}}
        self.alias, self.pinned = alias, pinned

    def binding_state(self, ws):
        return self.st if ws == WS else None

    def pin(self, ws):
        return {"owner_person_id": OWNER} if self.pinned and ws == WS else None

    def owner_alias(self, ws):
        return self.alias


class Acc:
    def is_hub_admin(self, user):
        return user == ADMIN


def adm(tmp_path, reg, settings=None):
    if settings is not None:
        S.save(tmp_path, settings)
    return make_admission(tmp_path, reg, Acc(), HUB)


GOOD = {
    "v": 1,
    "name": "Hub",
    "company": "",
    "addresses": [{"kind": "server", "web": "http://127.0.0.1:8100", "link": None}],
}


def test_a_bound_workspace_of_an_admin_is_vouched_for_at_its_binding_seq(tmp_path):
    assert adm(tmp_path, Reg())(WS) == (OWNER, 3)  # no settings file at all: default on


@pytest.mark.parametrize(
    "reg",
    [
        Reg(status="unbound"),
        Reg(status=None),
        Reg(hub="f" * 32),  # bound to another hub
        Reg(alias=MEMBER),  # owner is not a hub admin
        Reg(alias=None),  # no hub user bound at the invite: unknown, so no
        Reg(pinned=False),
    ],
)
def test_nobody_else_is_vouched_for(tmp_path, reg):
    assert adm(tmp_path, reg)(WS) is None


def test_another_workspace_gets_nothing(tmp_path):
    assert adm(tmp_path, Reg())("9" * 32) is None


def test_the_setting_off_stops_the_next_admission_and_is_read_live(tmp_path):
    a = adm(tmp_path, Reg(), {**GOOD, "share_files": True})
    assert a(WS) == (OWNER, 3)
    S.save(tmp_path, {**GOOD, "share_files": False})
    assert a(WS) is None
    S.save(tmp_path, GOOD)  # absent = on
    assert a(WS) == (OWNER, 3)


def test_a_damaged_settings_file_fails_closed(tmp_path):
    (tmp_path / S.SETTINGS_FILE).write_text("{not json")
    (tmp_path / S.SETTINGS_FILE).chmod(0o600)
    assert adm(tmp_path, Reg())(WS) is None


def test_share_files_must_be_a_bool():
    with pytest.raises(S.SettingsError):
        S.validate({**GOOD, "share_files": "yes"})
    assert S.validate({**GOOD, "share_files": False})["share_files"] is False
    assert "share_files" not in S.validate(GOOD)


def test_where_carries_the_publish_hint_only_when_it_is_a_clean_origin(tmp_path):
    S.save(
        tmp_path, {**GOOD, "addresses": [{"kind": "server", "web": "http://127.0.0.1:8100", "link": "127.0.0.1:8105"}]}
    )
    a = S.where_answer(tmp_path, publish="http://127.0.0.1:8102")
    assert a["publish"] == "http://127.0.0.1:8102" and "publish" not in S.where_answer(tmp_path)
    assert publish_hint("http://127.0.0.1:8102") == "http://127.0.0.1:8102"
    assert publish_hint("https://share.example.com/") == "https://share.example.com"
    for bad in (
        "http://100.64.1.2:8102",
        "http://10.0.0.5:8102",
        "ftp://x",
        "https://u:p@x.com",
        "https://x.com/path",
        None,
    ):
        assert publish_hint(bad) is None


@pytest.mark.parametrize(
    "bad", ["http://evil.com", "https://x.com/p", "https://x.com:99999", "javascript:1", 5, "HTTPS://X.com", ""]
)
def test_check_hub_where_refuses_a_bad_publish(bad):
    base = {"addresses": [{"kind": "server", "link": "127.0.0.1:8105"}], "name": "Hub"}
    assert not publish_ok(bad)
    with pytest.raises(Refused):
        check_hub_where({**base, "publish": bad})
    # #1838: a clean legacy ``publish`` is still accepted in shape, but the checker never returns it (nothing suggests from it)
    assert check_hub_where({**base, "publish": "https://x.com"}) == {**base, "name": "Hub"}
