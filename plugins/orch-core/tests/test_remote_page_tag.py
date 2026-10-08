"""The host tags the answers a device may draw as a dashboard page (found by the end-to-end run, #94): the Remote
frame writes only an answer whose signed reply carries page: true, and never one from a route with its own policy."""
from types import SimpleNamespace

import pytest

pytest.importorskip("cryptography")
pytest.importorskip("fastapi")

from orch.dashboard.bridge_loop import HostLoop  # noqa: E402

HTML = SimpleNamespace(status=200, headers=[("Content-Type", "text/html; charset=utf-8")])
JSON = SimpleNamespace(status=200, headers=[("content-type", "application/json")])


@pytest.mark.parametrize("path,tagged", [
    ("/", True), ("/board?x=1", True), ("/t/DEMO-0001/raw", True), ("/t/DEMO-0001", True),
    ("/a/DEMO-0001/x.html", False), ("/w/preview/DEMO-0001", False), ("/wp/wiki/abc", False),
    ("/wpf/wiki/abc", False), ("/addons/some/files/tok", False), ("/a/DEMO-0001/x.html?v=1", False)])
def test_only_a_dashboard_page_is_tagged_as_one(path, tagged):
    assert HostLoop._head(HTML, None, path).get("page", False) is tagged  # noqa: SLF001


def test_other_answers_and_streams_are_never_tagged():
    assert "page" not in HostLoop._head(JSON, None, "/")  # noqa: SLF001
    assert "page" not in HostLoop._head(HTML, None)  # noqa: SLF001 - a stream's head carries no path
    assert "page" not in HostLoop._head(None, "timeout", "/")  # noqa: SLF001
