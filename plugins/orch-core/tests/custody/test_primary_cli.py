"""``orch keys primary init|status`` (orch-v2-host 11A.15.9, 11A.15.10 item 9)."""

from __future__ import annotations

import io
import json

import pytest

from orch.custody import KdfParams, PassphraseBackend, primary
from orch.custody import cli as keys_cli

FAST = KdfParams(n=2**10)
PHRASE = "Zq7!mPx2-vL9#rTb4w"


def run(*argv, env=None):
    out, err = io.StringIO(), io.StringIO()
    code = keys_cli.main(list(argv), env=env or {}, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def cli(monkeypatch):
    asked = []

    def provider(req):
        asked.append(req.kind)
        return PHRASE

    monkeypatch.setattr(
        primary, "open_backend", lambda d: PassphraseBackend(d, _passphrase_provider=provider, _kdf=FAST, _min_n=2**10)
    )
    monkeypatch.setattr(keys_cli, "has_terminal", lambda: True)
    return asked


def test_init_then_status(tmp_path, cli):
    st = ("--state", str(tmp_path), "--json")
    code, out, _ = run("primary", "status", *st)
    assert code == 0 and json.loads(out)["result"] == {"present": False, "custody": None}
    code, out, err = run("primary", "init", *st)
    assert code == 0, out
    res = json.loads(out)["result"]
    info = primary.load(tmp_path)
    assert res["device"] == info.device and res["custody"] == "passphrase" and res["purposes"] == ["cert-request"]
    assert "certificate" in err and cli == ["create"]
    code, out, _ = run("primary", "status", *st)
    assert code == 0 and json.loads(out)["result"]["device"] == info.device
    assert cli == ["create"], "status never prompts"
    code, out, _ = run("primary", "init", *st)
    assert code != 0 and json.loads(out)["error"]["code"] == "custody.key_exists"


def test_init_is_human_only_and_needs_the_terminal(tmp_path, cli, monkeypatch):
    st = ("--state", str(tmp_path), "--json")
    code, out, _ = run("primary", "init", *st, env={"ORCH_GRANT": "g"})
    assert code == 3 and json.loads(out)["error"]["code"] == "human_only"
    monkeypatch.setattr(keys_cli, "has_terminal", lambda: False)
    code, out, _ = run("primary", "init", *st)
    assert json.loads(out)["error"]["code"] == "custody.no_prompt"
    assert cli == [] and not primary.keys_dir(tmp_path).exists()
    code, out, _ = run("primary", "status", *st, env={"ORCH_GRANT": "g"})
    assert code == 0  # public facts only, as `orch keys custody status`


def test_no_convenience_env_changes_nothing_for_the_primary(tmp_path, cli):
    """``ORCH_NO_CONVENIENCE`` only narrows (11A.15.11 N8a); the primary is passphrase custody either way."""
    st = ("--state", str(tmp_path), "--json")
    code, out, _ = run("primary", "init", *st, env={"ORCH_NO_CONVENIENCE": "1"})
    assert code == 0 and json.loads(out)["result"]["custody"] == "passphrase"
