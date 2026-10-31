"""The revocations floor baked into install.sh (scripts/release/floor.py)."""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "scripts" / "release"))

import floor  # noqa: E402

INSTALL_SH = ROOT / "install.sh"
REVS = ROOT / "release" / "keys" / "revocations.json"


def _list(tmp: Path, issued_at: str, min_serial: int) -> Path:
    doc = json.dumps(
        {"format": "orch.revocations/1", "min_serial": min_serial, "revoked": [], "issued_at": issued_at},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    p = tmp / "revocations.json"
    p.write_text(json.dumps({"doc": base64.b64encode(doc).decode(), "root_key_id": "root-1", "sig": ""}))
    return p


def test_bump_raises_the_floor_and_never_lowers_it(tmp_path):
    text = INSTALL_SH.read_text()
    at, serial = floor.read_floor(text)
    up = floor.bumped(text, "2099-01-01T00:00:00Z", serial + 3)
    assert floor.read_floor(up) == ("2099-01-01T00:00:00Z", serial + 3)
    assert floor.bumped(up, at, serial) == up  # an older list changes nothing
    assert sum(a != b for a, b in zip(up.splitlines(), text.splitlines(), strict=True)) == 2  # only the two lines


def test_cli_bump_then_check(tmp_path):
    sh = tmp_path / "install.sh"
    sh.write_text(INSTALL_SH.read_text())
    lst = _list(tmp_path, "2099-01-01T00:00:00Z", 9)
    assert floor.main(["--check", str(sh), str(lst)]) == 1  # the floor is older than that list
    assert floor.main([str(sh), str(lst)]) == 0
    assert floor.main(["--check", str(sh), str(lst)]) == 0
    assert floor.read_floor(sh.read_text()) == ("2099-01-01T00:00:00Z", 9)


def _roots_embedded() -> bool:
    return "ROOTS_B64 = {}" not in INSTALL_SH.read_text()


def test_revocations_json_must_exist_once_a_root_is_embedded():
    assert REVS.exists() or not _roots_embedded(), (
        "install.sh embeds a root key but release/keys/revocations.json is missing"
    )


@pytest.mark.skipif(not REVS.exists(), reason="no release/keys/revocations.json before the root ceremony")
def test_install_sh_floor_is_not_older_than_the_current_revocation_list():
    issued_at, min_serial = floor.read_list(REVS)
    assert not floor.is_stale(INSTALL_SH.read_text(), issued_at, min_serial), (
        "install.sh's revocations floor is older than release/keys/revocations.json: "
        "run `python3 scripts/release/floor.py install.sh release/keys/revocations.json`"
    )


def test_the_release_draft_compiles_wheels_only():
    draft = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "uv pip compile wheels.in" in draft and "--only-binary :all:" in draft  # no wheel: the release run fails
