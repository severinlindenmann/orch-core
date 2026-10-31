"""The revocation floor and freshness around the verifier (orch.update.floor)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from orch.ops.errors import OrchError
from orch.update import floor
from orch.update.verify import Verified

from .world import World, record, use_test_root


@pytest.fixture(autouse=True)
def _root():
    with use_test_root():
        yield


def verified(issued="2026-10-20T00:00:00Z", min_serial=2):
    return Verified("main", 42, "2.0.0.dev42", min_serial, issued, {})


NOW = datetime(2026, 10, 22, tzinfo=UTC)


def code(**kw):
    with pytest.raises(OrchError) as e:
        floor.check_revocations(
            verified(**kw.pop("v", {})), kw.pop("now", NOW), stored_issued_at=None, stored_min_serial=None
        )
    return e.value.code


def test_a_list_at_the_floor_and_fresh_passes():
    floor.check_revocations(verified(), NOW, stored_issued_at=None, stored_min_serial=None)


def test_below_the_baked_floor_is_refused():
    assert code(v={"issued": "2026-10-19T23:59:59Z"}) == "update.unverified"
    assert code(v={"min_serial": 0}) == "update.unverified"


def test_below_the_stored_values_is_refused():
    with pytest.raises(OrchError):
        floor.check_revocations(verified(min_serial=2), NOW, stored_issued_at=None, stored_min_serial=5)
    with pytest.raises(OrchError):
        floor.check_revocations(
            verified("2026-10-20T00:00:00Z"), NOW, stored_issued_at="2026-10-21T00:00:00Z", stored_min_serial=1
        )


def test_a_list_30_days_old_is_stale():
    assert code(now=datetime(2026, 11, 19, 0, 0, 1, tzinfo=UTC)) == "update.stale"
    floor.check_revocations(
        verified(), datetime(2026, 11, 18, 23, tzinfo=UTC), stored_issued_at=None, stored_min_serial=None
    )


def test_the_floor_lines_have_the_shape_floor_py_rewrites():
    import re
    from pathlib import Path

    text = Path(floor.__file__).read_text()
    assert re.search(r'^REVOCATIONS_FLOOR_ISSUED_AT = "\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ"$', text, re.M)
    assert re.search(r"^REVOCATIONS_FLOOR_MIN_SERIAL = \d+$", text, re.M)


def test_update_refuses_a_stale_list_and_a_lower_min_serial(tmp_path):
    w = World(tmp_path)
    w.source.publish("main", 42, expires_at="2027-01-01T00:00:00Z")
    w.now = datetime(2026, 11, 25, tzinfo=UTC)
    with pytest.raises(OrchError) as e:
        w.updater().update()
    assert e.value.code == "update.stale"
    assert w.layout.current_version() == "2.0.0.dev41" and w.services.events == []
    w.now = datetime(2026, 10, 22, tzinfo=UTC)
    from orch.update.layout import write_record

    write_record(w.layout, record(min_serial=9))
    with pytest.raises(OrchError) as e:
        w.updater().update()
    assert e.value.code == "update.unverified" and w.services.events == []


def test_a_future_list_is_refused():
    assert code(v={"issued": "2026-10-24T00:00:00Z"}) == "update.unverified"  # NOW is 22 Oct: more than a day ahead
    floor.check_revocations(verified("2026-10-22T20:00:00Z"), NOW, stored_issued_at=None, stored_min_serial=None)
