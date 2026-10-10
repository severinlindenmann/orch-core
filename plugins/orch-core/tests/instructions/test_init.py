"""``orch init`` end to end: a temp workspace through the test seams, then an agent session in it, and the log replays
clean. The refusals leave nothing behind."""

from __future__ import annotations

import io
import json
import re
import secrets
import time
from pathlib import Path

import pytest

from orch import canon, crypto
from orch.cli.main import main
from orch.custody import FileBackend
from orch.identity import check_recovery_code, derive_person_key, new_ulid, person_ref, sign_person_event
from orch.ops import workspace_init as wi
from orch.ops.commands.init import OP
from orch.ops.errors import OrchError
from orch.store import BackendSigner, Store
from tests.instructions.conftest import init_ctx

ARGS = {"prefix": "DEMO", "name": "Severin"}
SESSION = "s_01J9ZP0000000000000000000S"


def run_init(where, **kw):
    return OP.handler(init_ctx(where, **kw), dict(ARGS))


def tree(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*")}


def config(where) -> dict:
    return json.loads((where["root"] / "config.json").read_text())


def workspace_id(where) -> str:
    return config(where)["workspace"]["id"]


# ------------------------------------------------------------------------------------------------------ the happy path


def test_init_creates_workspace_keys_pin_and_instructions(where, term, passphrases):
    res = run_init(where)
    root, sd = where["root"], where["state"]
    wid = workspace_id(where)
    assert res.data == {"prefix": "DEMO", "workspace_id": wid} and res.seq == 1
    cfg = config(where)
    assert cfg["schema"] == "orch.workspace/2" and cfg["workspace"]["prefix"] == "DEMO"
    assert cfg["workspace"]["name"] == "work" and cfg["members"][0]["name"] == "Severin"
    assert cfg["members"][0]["role"] == "owner"
    assert (root / "keys.jsonl").read_bytes() == b""
    assert (root / "events" / "workspace.jsonl").read_bytes().count(b"\n") == 1
    # the pin and the keys are in the state dir, the keys are private
    assert (sd / "hosts" / wid / "genesis").read_text().startswith("sha256:")
    assert (sd / "hosts" / wid / "keys" / "wsk.filekey.json").stat().st_mode & 0o077 == 0
    assert (sd / "hosts" / wid / "person" / "dk.key.json").stat().st_mode & 0o077 == 0
    # the instruction files
    for rel in (
        "AGENTS.orch.md",
        "AGENTS.md",
        "CLAUDE.md",
        ".gitignore",
        ".claude/skills/orch-tickets/SKILL.md",
        ".claude/skills/orch-tickets/orch.skill.json",
        ".claude/skills/orch-work-on-ticket/SKILL.md",
        ".claude/skills/orch-refine-ticket/SKILL.md",
    ):
        assert (root / rel).is_file(), rel
    assert "@AGENTS.orch.md" in (root / "CLAUDE.md").read_text().split("\n")
    assert ".state/" in (root / ".gitignore").read_text().split("\n")
    assert res.hints and res.hints[0].startswith("orch grant")


def test_recovery_code_is_shown_once_on_the_terminal_and_nowhere_else(where, term, passphrases):
    res = run_init(where)
    assert len(term.shown) == 1 and len(term.waits) == 1
    words = re.findall(r"^[a-z]+(?: [a-z]+){23}$", term.shown[0], flags=re.M)
    assert len(words) == 1
    code = words[0]
    check_recovery_code(code)  # 24 valid BIP-39 words with a good checksum
    # the code is the person key: its person id is the owner of the genesis
    assert config(where)["members"][0]["person"] == person_ref(crypto.public_bytes(derive_person_key(code)))
    # ... and no file anywhere holds it, nor any run of its words, nor the result
    first_half = " ".join(code.split()[:6])
    for base in (where["root"], where["state"]):
        for p in base.rglob("*"):
            if p.is_file():
                assert first_half.encode() not in p.read_bytes(), p
    blob = json.dumps([res.data, res.lines, res.hints])
    assert first_half not in blob and " ".join(code.split()[:2]) not in blob
    # a second init in another directory draws another code
    other = {**where, "root": where["root"].parent / "second", "state": where["state"].parent / "state2"}
    other["root"].mkdir()
    run_init(other)
    assert term.shown[1] != term.shown[0]


def test_passphrase_is_asked_through_the_backend_for_the_key_and_for_the_genesis(where, term, passphrases):
    run_init(where)
    assert [r.kind for r in passphrases.requests] == ["create", "unlock"]  # create the device key, sign the genesis
    unlock = passphrases.requests[1]
    assert dict(unlock.fields)["type"] == "workspace.created"  # the person sees what is signed
    assert unlock.key_id == "dk"


def test_the_log_replays_clean_and_check_says_so(where, term, passphrases):
    run_init(where)
    root, sd = where["root"], where["state"]
    wid = workspace_id(where)
    s = Store.open(root, expected_workspace_id=wid, host_state_dir=sd, load="all")  # read-only, pin from the state dir
    try:
        assert s.chain_errors() == [] and s.reports == []
        ws = s.state.workspace
        assert ws.genesis == (sd / "hosts" / wid / "genesis").read_text().strip()
        assert list(ws.members) == [config(where)["members"][0]["person"]]
        assert ws.roster_v == 1
        (device,) = ws.devices.values()
        assert not device.removed and not device.revoked
    finally:
        s.close()
    out, err = io.StringIO(), io.StringIO()
    code = main(["check"], env={"ORCH_WORKSPACE": str(root), "ORCH_STATE_DIR": str(sd)}, stdout=out, stderr=err)
    assert code == 0 and out.getvalue().splitlines()[0] == "ok check 0", out.getvalue() + err.getvalue()


def issue_grant(where) -> str:
    """The person's ``orch grant`` (C7), done by hand: a grant signed with the device key ``init`` made."""
    root, sd, wid = where["root"], where["state"], workspace_id(where)
    store = Store.open(
        root,
        expected_workspace_id=wid,
        host=BackendSigner(FileBackend(sd / "hosts" / wid / "keys"), "wsk"),
        host_state_dir=sd,
        load="lazy",
    )
    try:
        backend = wi.open_backend(wi.person_dir(sd, wid))
        device_id, device = next(iter(store.state.workspace.devices.items()))
        now = int(time.time())

        def iso(t):
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))

        secret = secrets.token_bytes(32)
        gid = "gr_" + new_ulid()
        e = {
            "v": 2,
            "id": new_ulid(),
            "type": "grant.issued",
            "actor": {"kind": "person", "id": device.person, "device": device_id},
            "auth": backend.auth,
            "hash_v": 1,
            "roster_v": store.state.workspace.roster_v,
            "based_on": store.log_head("workspace"),
            "grant": gid,
            "scope": "all",
            "verbs": "agent",
            "issued_at": iso(now),
            "hours": 8,
            "expires_at": iso(now + 8 * 3600),
            "secret_hash": canon.grant_secret_hash(secret),
            "label": "init-test",
        }
        e["sig"] = sign_person_event(backend, wi.DEVICE_KEY, wid, "workspace", e, action="grant")
        store.append(e, log="workspace")
        return f"{gid}.{crypto.b64u(secret)}"
    finally:
        store.close()


def test_an_agent_session_works_in_the_new_workspace(where, term, passphrases):
    run_init(where)
    grant = issue_grant(where)
    env = {
        "ORCH_STATE_DIR": str(where["state"]),
        "HOME": str(where["home"]),
        "ORCH_SESSION": SESSION,
        "ORCH_GRANT": grant,
        "ORCH_WORKSPACE": str(where["root"]),
    }

    def orch(*argv):
        out, err = io.StringIO(), io.StringIO()
        code = main(list(argv), env=env, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    code, out, err = orch("status")
    assert code == 0 and out.startswith("ok status Severin"), out + err
    assert "genesis pin created" not in out  # init pinned it
    code, out, err = orch("new", "First ticket", "-m", "Do the thing.")
    assert code == 0 and out.startswith("ok DEMO-0001 ticket.created"), out + err
    assert orch("claim", "DEMO-0001")[0] == 0
    code, out, err = orch("ask", "Which way?", "--options", "a,b", "--rec", "a")
    assert code == 0 and "question.asked Q1" in out, out + err
    code, out, err = orch("instructions", "hook", "session-start")  # the hook text of the new session
    assert code == 0 and out.startswith("ok session-start Severin") and len(out.splitlines()) <= 6, out + err
    # the whole log, genesis, grant and ticket events, still replays clean
    s = Store.open(where["root"], expected_workspace_id=workspace_id(where), host_state_dir=where["state"], load="all")
    try:
        assert s.chain_errors() == [] and s.reports == []
    finally:
        s.close()
    assert orch("check")[1].splitlines()[0] == "ok check 0"


def test_init_owner_name_defaults_to_the_user(where, term, passphrases):
    OP.handler(init_ctx(where), {"prefix": "DEMO"})
    assert config(where)["members"][0]["name"] == "severin"


def test_dry_run_creates_nothing(where, term, passphrases):
    ctx = init_ctx(where)
    ctx.dry_run = True
    res = OP.handler(ctx, dict(ARGS))
    assert res.lines and not term.shown and not passphrases.requests
    assert tree(where["root"]) == set() and not where["state"].exists()


# ---------------------------------------------------------------------------------------------------------- refusals


def test_refuses_with_a_grant_before_anything_exists(where, term, passphrases):
    with pytest.raises(OrchError) as e:
        run_init(where, grant="gr_" + "0" * 26 + "." + "A" * 43)
    assert e.value.code == "human_only"
    assert tree(where["root"]) == set() and not where["state"].exists() and not term.shown


def test_refuses_without_human_presence(where, term, passphrases):
    with pytest.raises(OrchError) as e:
        run_init(where, human=False)
    assert e.value.code == "human_only" and not where["state"].exists()


def test_refuses_without_a_terminal_and_creates_nothing(where, term, passphrases):
    term.ok = False
    with pytest.raises(OrchError) as e:
        run_init(where)
    assert e.value.code == "human_only" and "terminal" in e.value.message
    assert tree(where["root"]) == set() and not where["state"].exists() and not passphrases.requests


def test_refuses_when_a_workspace_exists_here_or_above(where, term, passphrases):
    run_init(where)
    snapshot = {p: p.read_bytes() for p in where["root"].rglob("*") if p.is_file()}
    with pytest.raises(OrchError) as e:
        run_init(where)
    assert e.value.code == "invalid.input" and "already exists" in e.value.message
    sub = where["root"] / "sub"
    sub.mkdir()
    with pytest.raises(OrchError) as e:
        run_init({**where, "root": sub})
    assert e.value.code == "invalid.input"
    sub.rmdir()
    assert {p: p.read_bytes() for p in where["root"].rglob("*") if p.is_file()} == snapshot


def test_refuses_a_directory_with_workspace_leftovers(where, term, passphrases):
    (where["root"] / "events").mkdir()
    with pytest.raises(OrchError) as e:
        run_init(where)
    assert e.value.code == "invalid.input" and not where["state"].exists()


def test_refuses_a_state_directory_inside_the_workspace(where, term, passphrases):
    with pytest.raises(OrchError) as e:
        run_init(where, ORCH_STATE_DIR=str(where["root"] / "keys-here"))
    assert e.value.code == "invalid.input" and "inside" in e.value.message


def test_a_too_short_passphrase_rolls_everything_back(where, term, passphrases):
    passphrases.answer = "short"  # under the 8 character floor: the backend refuses to create the key
    with pytest.raises(OrchError) as e:
        run_init(where)
    assert e.value.code == "invalid.input" and "nothing was created" in e.value.message
    assert tree(where["root"]) == set()
    assert not (where["state"] / "hosts").exists() or not list((where["state"] / "hosts").iterdir())


def test_a_failed_genesis_leaves_no_files_and_a_second_try_works(where, term, passphrases, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    with monkeypatch.context() as m:
        m.setattr(Store, "append", boom)
        with pytest.raises(OrchError):
            run_init(where)
    assert tree(where["root"]) == set()
    assert not (where["state"] / "hosts").exists() or not list((where["state"] / "hosts").iterdir())
    run_init(where)
    assert (where["root"] / "config.json").is_file()


def test_existing_agents_and_claude_files_are_extended_not_replaced(where, term, passphrases):
    (where["root"] / "AGENTS.md").write_text("# Repo rules\nBe kind.\n")
    (where["root"] / "CLAUDE.md").write_text("Use tabs.")
    (where["root"] / ".gitignore").write_text("node_modules/\n")
    run_init(where)
    a = (where["root"] / "AGENTS.md").read_text()
    assert a.startswith("# Repo rules\nBe kind.\n") and "AGENTS.orch.md" in a
    assert (where["root"] / "CLAUDE.md").read_text() == "Use tabs.\n@AGENTS.orch.md\n"
    assert (where["root"] / ".gitignore").read_text() == "node_modules/\n.state/\n"


def test_no_production_path_skips_the_prompts():
    """The seams (a fake terminal, an injected passphrase provider, a lowered scrypt cost) are reachable only from
    tests: no production module outside ``custody/passphrase.py`` names them, and ``init`` takes no option for them."""
    from tests.custody.test_seams import SEAMS, SRC

    offenders = []
    for path in SRC.rglob("*.py"):
        if path.name == "passphrase.py":
            continue
        text = path.read_text()
        offenders += [(path.name, s) for s in SEAMS if re.search(rf"(?<![A-Za-z0-9]){s}\b", text)]
    assert offenders == []
    assert set(OP.input["properties"]) == {"prefix", "name"}
    assert wi.TERMINAL.__class__ is wi.Terminal  # the module default is the real /dev/tty terminal
