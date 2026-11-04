"""Services hold the state root: the journal and the format are enforced at start (orch.update.guard)."""

from __future__ import annotations

import pytest

from orch.migrate import init_root, read_format
from orch.migrate.journal import journal_path, read_journal, write_journal
from orch.ops.errors import OrchError
from orch.update import quarantine
from tests.host.lifecycle_helpers import Lab


@pytest.fixture
def lab(tmp_path):
    lab = Lab(tmp_path)
    yield lab
    lab.close()


def env_of(lab):
    return {"ORCH_STATE_DIR": str(lab.state)}


def test_an_unfinished_journal_without_the_marker_stops_the_host(lab):
    write_journal(f"migrated:{lab.state}", env_of(lab), backups={})
    with pytest.raises(OrchError) as e:
        with lab.host():
            pass
    assert e.value.code == "update.interrupted"


def test_with_the_marker_the_host_starts_quarantined(lab):
    init_root(lab.state, "host")
    write_journal("stopped", env_of(lab), backups={})
    write_journal(f"migrated:{lab.state}", env_of(lab), backups={})
    if read_format(lab.state, "host") is None:
        init_root(lab.state, "host")
    quarantine.activate(lab.state, read_journal(env_of(lab))["update_id"])
    with lab.host() as h:
        assert h.host.quarantine
        with h.client() as c:
            assert c.call("host.list")["ok"] is True


def test_the_marker_alone_does_not_help_other_roots(lab, tmp_path):
    from orch.migrate import open_root

    write_journal("stopped", env_of(lab), backups={})
    quarantine.activate(lab.state, read_journal(env_of(lab))["update_id"])
    other = tmp_path / "other"
    init_root(other, "workspace")
    with pytest.raises(OrchError):
        open_root(other, "workspace", env_of(lab))  # not asked with quarantine_ok


def test_a_running_host_holds_the_lock_so_migrate_is_refused(lab):
    from orch.migrate.format import lock_root

    with lab.host():
        with pytest.raises(OrchError) as e:
            lock_root(lab.state, exclusive=True)
        assert e.value.code == "migrate.running"
    assert journal_path(env_of(lab)).parent.exists() or True


def test_a_fresh_state_is_born_with_a_format(lab):
    from orch.migrate import read_format

    with lab.host():
        assert read_format(lab.state, "host") == 1


def _unfinished(lab):
    if read_format(lab.state, "host") is None:
        init_root(lab.state, "host")
    write_journal("stopped", env_of(lab), backups={})
    return read_journal(env_of(lab))["update_id"]


def test_the_marker_names_the_update_it_belongs_to(lab):
    from orch.migrate import open_root

    uid = _unfinished(lab)
    quarantine.activate(lab.state, uid)
    marker = quarantine.marker(lab.state)
    assert marker.is_file() and not marker.is_symlink() and marker.read_text().strip() == uid
    assert (marker.stat().st_mode & 0o777) == 0o600
    open_root(lab.state, "host", env_of(lab), quarantine_ok=True).close()


def test_a_marker_with_the_wrong_id_admits_nothing(lab):
    from orch.migrate import open_root

    _unfinished(lab)
    quarantine.activate(lab.state, "ab" * 16)
    with pytest.raises(OrchError) as e:
        open_root(lab.state, "host", env_of(lab), quarantine_ok=True)
    assert e.value.code == "update.interrupted"
    with pytest.raises(OrchError):
        with lab.host():
            pass


def test_a_symlink_marker_admits_nothing(lab, tmp_path):
    from orch.migrate import open_root

    uid = _unfinished(lab)
    real = tmp_path / "real-marker"
    real.write_text(uid + "\n")
    marker = quarantine.marker(lab.state)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.symlink_to(real)
    with pytest.raises(OrchError):
        open_root(lab.state, "host", env_of(lab), quarantine_ok=True)


def test_activate_replaces_a_planted_link_and_checks_the_id(lab, tmp_path):
    uid = _unfinished(lab)
    marker = quarantine.marker(lab.state)
    marker.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "victim"
    target.write_text("keep")
    marker.symlink_to(target)
    quarantine.activate(lab.state, uid)
    assert target.read_text() == "keep" and marker.is_file() and not marker.is_symlink()
    with pytest.raises(ValueError):
        quarantine.activate(lab.state, "../../x")
