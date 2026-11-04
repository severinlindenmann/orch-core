import pytest
from route_paths import concrete_path

from orch_hub.webapi.api import ROUTES, Route


def test_every_real_route_gets_a_path_that_routes():
    for rt in ROUTES:
        assert rt.rx.fullmatch(concrete_path(rt)), rt.path


def test_overrides_are_used():
    rt = next(r for r in ROUTES if "{channel_id}" in r.path)
    assert "c" * 32 in concrete_path(rt, {"channel_id": "c" * 32})


def test_a_new_param_shape_fails_loudly():
    rt = Route("GET", "/api/v1/x/{widget_id}", "admin", "t", "h")
    object.__setattr__(rt, "rx", __import__("re").compile(r"/api/v1/x/(?P<widget_id>[0-9]{3})"))
    with pytest.raises(AssertionError, match="widget_id"):
        concrete_path(rt)
