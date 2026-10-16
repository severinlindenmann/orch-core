"""B1b commit 1: ``orch keys primary init --workspace W --relay O --invite-file F`` (orch-v2-host 11A.15.23):
write-ahead, resume, 409, invite handling, no secrets in the state directory. Fakes for the relay client and the
recovery-code source (the real ones are covered in tests/relay_owner and the T2 test)."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from orch import crypto
from orch.custody import KdfParams, PassphraseBackend, primary, primary_register
from orch.custody import cli as keys_cli
from orch.custody.personkey import PersonMismatch
from orch.relay_owner import OwnerRefused, OwnerTransportError
from tests.relay_owner.kit import VecPK

FAST = KdfParams(n=2**10)
PHRASE = "Zq7!mPx2-vL9#rTb4w"
ORIGIN = "https://relay.example"
INVITE = "INVITE-secret-0123456789abcdef"
OWNER = VecPK()


@pytest.fixture(autouse=True)
def fast_backend(monkeypatch):
    asked = []

    def provider(req):
        asked.append(req.kind)
        return PHRASE

    monkeypatch.setattr(
        primary, "open_backend", lambda d: PassphraseBackend(d, _passphrase_provider=provider, _kdf=FAST, _min_n=2**10)
    )
    monkeypatch.setattr(keys_cli, "has_terminal", lambda: True)
    return asked


class FakeClient:
    """Stands for ``OwnerClient``: records calls; ``script`` maps a method to an exception or a value."""

    def __init__(self, script=None):
        self.script = script or {}
        self.calls = []
        self.closed = False

    def _do(self, name, *a):
        self.calls.append((name, a))
        got = self.script.get(name)
        if isinstance(got, list):
            got = got.pop(0) if got else None
        if isinstance(got, Exception):
            raise got
        return got

    def login(self):
        return self._do("login")

    def create_account(self, invite, pk_pub, cert):
        self._do("create_account", invite, pk_pub, cert)
        return crypto.person_id(pk_pub).hex()

    def register_device(self, cert, pk_pub):
        return self._do("register_device", cert, pk_pub)

    def close(self):
        self.closed = True


class FakePK:
    """A recovery-code source: counts entries; may refuse like the real one on a wrong code."""

    def __init__(self, refuse=None):
        self.refuse, self.entered = refuse, 0

    def __call__(self, **kw):
        assert kw["pk_pub"] == OWNER.pk_pub and kw["labels"] == {"sig_device_cert"}
        self.kw = kw
        return self

    def __enter__(self):
        self.entered += 1
        if self.refuse:
            raise self.refuse
        return OWNER

    def __exit__(self, *a):
        return False


def invite_file(tmp_path: Path, text: str = INVITE + "\n", mode: int = 0o600) -> Path:
    f = tmp_path / "invite"
    f.write_text(text)
    f.chmod(mode)
    return f


def reg(tmp_path, client, pk=None, **kw):
    state = tmp_path / "state"
    if "invite_file" not in kw:
        kw["invite_file"] = invite_file(tmp_path)
    return primary_register.register(
        state, origin=kw.pop("origin", ORIGIN), owner_pk=OWNER.pk_pub, pk_factory=pk or FakePK(),
        client_factory=lambda origin, session: client, label=kw.pop("label", "Mac (primary)"), **kw,
    )  # fmt: skip


def tree_bytes(root: Path) -> bytes:
    return b"".join(p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file() and p.name != "invite")


def test_happy_path_writes_cert_and_relay_and_keeps_no_secret(tmp_path, fast_backend):
    client, pk = FakeClient(), FakePK()
    res = reg(tmp_path, client, pk)
    state = tmp_path / "state"
    assert fast_backend == ["create", "unlock"] or fast_backend[0] == "create"
    assert pk.entered == 1 and [c[0] for c in client.calls] == ["create_account"] and client.closed
    assert res["origin"] == ORIGIN and res["person_id"] == crypto.person_id(OWNER.pk_pub).hex()
    cert = primary_register.read_cert(state)
    rel = primary_register.read_relay(state)
    info = primary.load(state)
    assert cert["registered"] is True and cert["cert"]["o"]["device_id"] == info.device_id.hex()
    assert rel["device_id"] == info.device_id.hex() and rel["label"] == "Mac (primary)"
    for p in (primary_register.cert_path(state), primary_register.relay_path(state)):
        assert oct(p.stat().st_mode & 0o777) == "0o600"
    blob = tree_bytes(state)
    assert INVITE.encode() not in blob and OWNER.pk_pub.hex().encode() not in blob
    st = primary.status(state)
    assert st["certificate"]["registered"] is True and st["relay"]["origin"] == ORIGIN


def test_invite_file_rules(tmp_path):
    state_before = tmp_path / "state"
    for make in (
        lambda: invite_file(tmp_path, mode=0o644),
        lambda: invite_file(tmp_path, text="a\nb\n"),
        lambda: invite_file(tmp_path, text="\n"),
        lambda: invite_file(tmp_path, text="has space in it\n"),
        lambda: tmp_path,  # a directory
    ):
        f = make()
        with pytest.raises(primary_register.RegisterError) as e:
            primary_register.read_invite_file(f)
        assert e.value.code == "primary.invite_file" and INVITE not in str(e.value)
    real = invite_file(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(primary_register.RegisterError):
        primary_register.read_invite_file(link)
    with pytest.raises(primary_register.RegisterError):
        primary_register.read_invite_file(tmp_path / "missing")
    assert primary_register.read_invite_file(real) == INVITE
    assert not state_before.exists()


def test_bad_invite_file_is_refused_before_the_recovery_code(tmp_path):
    pk, client = FakePK(), FakeClient()
    with pytest.raises(primary_register.RegisterError) as e:
        reg(tmp_path, client, pk, invite_file=invite_file(tmp_path, mode=0o644))
    assert e.value.code == "primary.invite_file" and pk.entered == 0 and client.calls == []
    assert primary_register.read_cert(tmp_path / "state") is None


def test_person_mismatch_sends_nothing_and_writes_no_cert(tmp_path):
    client = FakeClient()
    with pytest.raises(PersonMismatch):
        reg(tmp_path, client, FakePK(refuse=PersonMismatch("not the owner")))
    assert client.calls == [] and primary_register.read_cert(tmp_path / "state") is None


def test_crash_between_post_and_rewrite_resumes_by_login_without_code_or_invite(tmp_path):
    first = FakeClient({"create_account": OwnerTransportError("deadline", "r")})
    with pytest.raises(primary_register.RegisterError) as e:
        reg(tmp_path, first)
    assert e.value.code == "primary.account_failed"
    state = tmp_path / "state"
    stored = primary_register.read_cert(state)
    assert stored["registered"] is False and primary_register.read_relay(state) is None
    pk, second = FakePK(), FakeClient()  # login works: the account exists
    res = reg(tmp_path, second, pk, invite_file=None)
    assert res["resumed"] is True and pk.entered == 0 and [c[0] for c in second.calls] == ["login"]
    assert (
        primary_register.read_cert(state)["registered"] is True
        and primary_register.read_relay(state)["origin"] == ORIGIN
    )
    assert primary_register.read_cert(state)["cert"] == stored["cert"]


def test_resume_when_login_fails_reposts_the_stored_certificate_bytes(tmp_path):
    first = FakeClient({"create_account": OwnerTransportError("connect", "r")})
    with pytest.raises(primary_register.RegisterError):
        reg(tmp_path, first)
    stored = primary_register.read_cert(tmp_path / "state")["cert"]
    refused = OwnerRefused("POST /v2/session/login", 401, "cert_invalid", None)
    second, pk = FakeClient({"login": refused}), FakePK()
    reg(tmp_path, second, pk)
    names = [c[0] for c in second.calls]
    assert names == ["login", "create_account"] and pk.entered == 0
    assert second.calls[1][1][2] == stored  # the same certificate, not a second one for the device


def test_409_account_exists_registers_the_device_and_logs_in(tmp_path):
    client = FakeClient({"create_account": OwnerRefused("POST /v2/accounts", 409, "account_exists", None)})
    res = reg(tmp_path, client)
    assert [c[0] for c in client.calls] == ["create_account", "register_device", "login"]
    assert client.calls[1][1][0] == client.calls[0][1][2]  # same cert
    assert res["device_id"] and primary_register.read_cert(tmp_path / "state")["registered"] is True


@pytest.mark.parametrize("code", ["cert_conflict", "other_person"])
def test_409_then_device_refused_maps_to_a_clear_code(tmp_path, code):
    client = FakeClient({
        "create_account": OwnerRefused("POST /v2/accounts", 409, "account_exists", None),
        "register_device": OwnerRefused("PUT /v2/devices/{device}", 409, code, None),
    })  # fmt: skip
    with pytest.raises(primary_register.RegisterError) as e:
        reg(tmp_path, client)
    assert e.value.code == "primary.account_exists"
    assert primary_register.read_cert(tmp_path / "state")["registered"] is False


def test_403_invite_unusable_and_the_invite_is_nowhere(tmp_path, capsys):
    client = FakeClient({"create_account": OwnerRefused("POST /v2/accounts", 403, "not_eligible", None)})
    with pytest.raises(primary_register.RegisterError) as e:
        reg(tmp_path, client)
    assert e.value.code == "primary.invite_unusable" and "new invite" in (e.value.hint or "")
    assert INVITE not in str(e.value) and INVITE not in repr(e.value)
    assert primary_register.read_relay(tmp_path / "state") is None


@pytest.mark.parametrize(
    "exc",
    [OwnerRefused("POST /v2/accounts", 503, "overloaded", None), OwnerTransportError("truncated", "r"),
     OwnerRefused("POST /v2/accounts", 500, None, None)],
)  # fmt: skip
def test_5xx_and_connection_reset_never_show_the_invite(tmp_path, capsys, exc):
    state = tmp_path / "state"
    with pytest.raises(primary_register.RegisterError) as e:
        reg(tmp_path, FakeClient({"create_account": exc}))
    text = str(e.value) + repr(e.value) + (e.value.hint or "")
    assert INVITE not in text and e.value.__cause__ is None
    assert INVITE.encode() not in tree_bytes(state)
    cap = capsys.readouterr()
    assert INVITE not in cap.out + cap.err


def test_one_relay_per_install(tmp_path):
    reg(tmp_path, FakeClient())
    with pytest.raises(primary_register.RegisterError) as e:
        reg(tmp_path, FakeClient(), origin="https://other.example")
    assert e.value.code == "primary.relay_mismatch"
    again = reg(tmp_path, FakeClient())  # the same relay: already registered, nothing is sent
    assert again["already"] is True


def test_relay_mismatch_also_for_a_pending_registration(tmp_path):
    with pytest.raises(primary_register.RegisterError):
        reg(tmp_path, FakeClient({"create_account": OwnerTransportError("connect", "r")}))
    with pytest.raises(primary_register.RegisterError) as e:
        reg(tmp_path, FakeClient(), origin="https://other.example")
    assert e.value.code == "primary.relay_mismatch"


def test_other_primary_key_in_a_stored_cert_is_refused(tmp_path):
    state = tmp_path / "state"
    with pytest.raises(primary_register.RegisterError):
        reg(tmp_path, FakeClient({"create_account": OwnerTransportError("connect", "r")}))
    doc = json.loads(primary_register.cert_path(state).read_text())
    doc["cert"]["o"]["device_id"] = "00" * 16
    primary_register.cert_path(state).write_text(json.dumps(doc))
    with pytest.raises(primary_register.RegisterError) as e:
        reg(tmp_path, FakeClient())
    assert e.value.code == "primary.not_registered"


# -- the CLI ----------


def run(*argv, env=None):
    out, err = io.StringIO(), io.StringIO()
    code = keys_cli.main(list(argv), env=env or {}, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def wired(monkeypatch, tmp_path):
    client, pk = FakeClient(), FakePK()
    monkeypatch.setattr(keys_cli, "_one", lambda state, pick: {"workspace_id": "w" * 32})
    monkeypatch.setattr(keys_cli, "owner_pk_from_workspace", lambda state, entry: OWNER.pk_pub)
    monkeypatch.setattr(keys_cli, "_client_factory", lambda origin, session: client)
    monkeypatch.setattr(keys_cli, "_pk_factory", pk)
    return client, pk


def test_cli_init_with_relay_registers(tmp_path, wired):
    f = invite_file(tmp_path)
    code, out, err = run("primary", "init", "--state", str(tmp_path / "s"), "--json", "--workspace", "w",
                         "--relay", ORIGIN, "--invite-file", str(f))  # fmt: skip
    assert code == 0, out
    res = json.loads(out)["result"]
    assert res["origin"] == ORIGIN and res["certificate"]["registered"] is True
    assert INVITE not in out + err and "orch host relay link" in err


def test_cli_requires_workspace_and_invite_and_terminal_and_not_grant(tmp_path, wired, monkeypatch):
    st = ("--state", str(tmp_path / "s"), "--json")
    f = invite_file(tmp_path)
    code, out, _ = run("primary", "init", *st, "--relay", ORIGIN, "--invite-file", str(f))
    assert code != 0 and json.loads(out)["error"]["code"] == "invalid.input"  # no --workspace
    code, out, _ = run("primary", "init", *st, "--workspace", "w", "--relay", ORIGIN)
    assert json.loads(out)["error"]["code"] == "invalid.input"  # no invite file, nothing to resume
    code, out, _ = run("primary", "init", *st, "--workspace", "w", "--invite-file", str(f))
    assert json.loads(out)["error"]["code"] == "invalid.input"  # a file without --relay
    code, out, _ = run("primary", "init", *st, "--workspace", "w", "--relay", ORIGIN, "--invite-file", str(f),
                       env={"ORCH_GRANT": "g"})  # fmt: skip
    assert json.loads(out)["error"]["code"] == "human_only"
    monkeypatch.setattr(keys_cli, "has_terminal", lambda: False)
    code, out, _ = run("primary", "init", *st, "--workspace", "w", "--relay", ORIGIN, "--invite-file", str(f))
    assert json.loads(out)["error"]["code"] == "custody.no_prompt"
    assert not (tmp_path / "s").exists() or not primary.keys_dir(tmp_path / "s").exists()


def test_cli_error_codes_carry_a_hint_and_never_the_invite(tmp_path, wired):
    client, _ = wired
    client.script["create_account"] = OwnerRefused("POST /v2/accounts", 403, "not_eligible", None)
    f = invite_file(tmp_path)
    code, out, err = run("primary", "init", "--state", str(tmp_path / "s"), "--json", "--workspace", "w",
                         "--relay", ORIGIN, "--invite-file", str(f))  # fmt: skip
    body = json.loads(out)["error"]
    assert code == 3 and body["code"] == "primary.invite_unusable" and "new invite" in body["message"]
    assert INVITE not in out + err
    assert os.listdir(tmp_path / "s" / "person" / "primary")  # keys stay; a new invite resumes


def test_cli_plain_init_is_unchanged_and_names_the_next_step(tmp_path):
    code, out, err = run("primary", "init", "--state", str(tmp_path / "s"), "--json")
    assert code == 0 and "--invite-file" in err


def test_owner_pk_comes_from_the_workspace_genesis(tmp_path):
    from tests.host.lifecycle_helpers import Lab

    lab = Lab(tmp_path)
    try:
        from orch.ops.caps import Files
        from orch.ops.runtime import _config

        wid = _config(Path(lab.root), Files.direct())["workspace"]["id"]
        got = keys_cli.owner_pk_from_workspace(lab.state, {"workspace_id": wid, "root": lab.root})
        assert got == lab.ws.owner.pk_pub
        with pytest.raises(keys_cli._Fail):
            keys_cli.owner_pk_from_workspace(lab.state, {"workspace_id": "0" * 32, "root": lab.root})
    finally:
        lab.close()
