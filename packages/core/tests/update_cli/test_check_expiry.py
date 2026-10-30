"""``orch update --check`` warns 10 days before the installed update signature list expires (30 days after its date)."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime

import pytest

from orch.cli import update as cli

from .world import World, use_test_root


@pytest.fixture(autouse=True)
def _root():
    with use_test_root():
        yield


def check(tmp_path, now):
    w = World(tmp_path)  # the install record's list is dated 2026-10-20, so it expires on 2026-11-19
    w.source.publish("main", 42, expires_at="2027-01-01T00:00:00Z")
    w.now = now
    out, err = io.StringIO(), io.StringIO()
    rc = cli.main(["--check", "--json"], env=w.env, out=out, err=err, seams=w.seams())
    text = io.StringIO()
    cli.main(["--check"], env=w.env, out=text, err=io.StringIO(), seams=w.seams())
    return rc, out.getvalue(), err.getvalue(), text.getvalue()


def test_11_days_left_gives_no_warning(tmp_path):
    rc, out, _, text = check(tmp_path, datetime(2026, 11, 8, 12, tzinfo=UTC))
    assert rc == 0 and json.loads(out)["warnings"] == [] and "warning" not in text


def test_9_days_left_warns_in_plain_words(tmp_path):
    rc, out, _, text = check(tmp_path, datetime(2026, 11, 10, 12, tzinfo=UTC))
    msg = "the update signature list expires on 2026-11-19: ask your admin to renew it"
    assert rc == 0 and json.loads(out)["warnings"] == [msg]
    assert f"warning: {msg}" in text


def test_expired_is_refused_with_plain_words(tmp_path):
    rc, out, err, _ = check(tmp_path, datetime(2026, 11, 20, 12, tzinfo=UTC))
    doc = json.loads(out)
    assert rc != 0 and doc["ok"] is False and doc["code"] == "update.stale"
    assert "expired on 2026-11-19: ask your admin to renew it" in doc["message"]


def test_a_newer_fetched_list_replaces_the_admin_text(tmp_path):
    w = World(tmp_path)  # installed list dated 2026-10-20: 9 days left on 2026-11-10
    w.source.publish("main", 42, expires_at="2027-01-01T00:00:00Z")
    w.now = datetime(2026, 11, 10, 12, tzinfo=UTC)
    from orch.update import floor

    fresh = "2026-11-08T00:00:00Z"
    assert floor.list_warning("2026-10-20T00:00:00Z", fresh, w.now) == (
        "a renewed update signature list is available: run orch update"
    )
    # a newer list that is itself close to expiry: the admin text, with ITS date
    assert floor.list_warning("2026-10-01T00:00:00Z", "2026-10-20T00:00:00Z", w.now) == (
        "the update signature list expires on 2026-11-19: ask your admin to renew it"
    )
    # nothing near expiry on either side
    assert floor.list_warning("2026-11-01T00:00:00Z", "2026-11-09T00:00:00Z", w.now) is None
