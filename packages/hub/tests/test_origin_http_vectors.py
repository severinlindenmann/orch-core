import json
import pathlib

import pytest

from orch_hub.contract import hub
from orch_hub.publish.forwarder import _endpoint

V = json.loads((pathlib.Path(__file__).parent.parent / "vectors" / "origin_http.json").read_text())


@pytest.mark.parametrize("o", V["accept"])
def test_accept(o):
    assert hub._relay_url(o)
    _endpoint(o)


@pytest.mark.parametrize("o", V["refuse"])
def test_refuse(o):
    assert not hub._relay_url(o)
    with pytest.raises(ValueError):
        _endpoint(o)
