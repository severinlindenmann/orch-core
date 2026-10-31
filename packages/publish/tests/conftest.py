import pytest


@pytest.fixture(autouse=True)
def _formatted_data(monkeypatch, request):
    """``orch install publish`` leaves ``format.json`` in the data dir and pubsrv opens it only at that
    format: a data dir that a test names gets the same file first. A test that wants the real rule on a
    bare dir says ``raw_data``."""
    if request.node.get_closest_marker("raw_data"):
        return
    from pubsrv import db
    from tests.format_helpers import ensure_format

    real = db.open_data

    def open_formatted(path, kind, current, *args, **kw):
        ensure_format(path, kind, current)
        return real(path, kind, current, *args, **kw)

    monkeypatch.setattr(db, "open_data", open_formatted)
