"""r7-P07: a title in an edit_sequence is carried as {was, text} from the host's current title, one line."""

import pytest
from orch.host import sequence

from orch_dashboard.errors import HostError
from orch_dashboard.signing import EditTicket


@pytest.fixture
def data():
    return {
        "doc": {
            "key": "DEMO-0001",
            "uid": "01AAAAAAAAAAAAAAAAAAAAAAAA",
            "type": "feature",
            "title": "Invoice export",
            "head": {"seq": 7, "head": "sha256:" + "a" * 64},
            "sections": {"summary": "Before"},
        }
    }


def made(data, **kw):
    body = {"action": "edit_sequence", "key": "DEMO-0001", "head_seq": 7, **kw}
    return EditTicket.model_validate(body).intent(data)


def test_a_new_title_is_one_set_step_with_the_old_title_beside_it(data):
    intent = made(data, title="  Invoice export for Ben ".strip())
    assert intent["title"] == {"was": "Invoice export", "text": "Invoice export for Ben"}
    assert [s["args"]["pairs"] for s in sequence.steps(intent)] == [["title=Invoice export for Ben"]]


def test_an_unchanged_title_is_no_change_and_a_newline_is_refused(data):
    with pytest.raises(HostError):
        made(data, title="Invoice export")
    with pytest.raises(HostError):
        made(data, title="two\nlines")
    assert "title" not in made(data, title="Invoice export", sections={"summary": "After"})
