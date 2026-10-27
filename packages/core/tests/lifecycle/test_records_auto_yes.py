"""``orch records auto on --yes``: the click's answer replaces the question (the dashboard's auto-on child)."""

from types import SimpleNamespace

from orch.records import cli


class Tips:
    def __init__(self):
        self.entries = {}

    def auto(self, wid):
        return self.entries.get(wid)

    def set_auto(self, wid, entry):
        if entry is None:
            self.entries.pop(wid, None)
        else:
            self.entries[wid] = entry

    def autos(self):
        return dict(self.entries)


class Timer:
    def install(self, replace=False):
        return True

    def start(self, changed=False):
        return None

    def remove(self):
        return "removed"


def switch(tmp_path, argv, answer):
    asked = []

    def confirm(text):
        asked.append(text)
        return answer

    args = cli._parser().parse_args(argv)
    run = cli._Run(args, {}, {"confirm": confirm, "timer": lambda state: Timer()})
    r = SimpleNamespace(
        tips=Tips(), workspace_id="W", mode="seam", root=tmp_path, state=tmp_path, link=lambda op, a: {}
    )
    return cli._do_auto_switch(run, r), asked, r
def test_auto_on_yes_does_not_ask(tmp_path):
    res, asked, r = switch(tmp_path, ["auto", "on", "--yes"], answer=False)
    assert asked == []
    assert res["auto"]["on"] is True
    assert r.tips.auto("W")["on"] is True


def test_auto_on_without_yes_still_asks(tmp_path):
    import pytest
    from orch.records import RecordsError

    with pytest.raises(RecordsError) as err:
        switch(tmp_path, ["auto", "on"], answer=False)
    assert err.value.code == "records.unconfirmed"
