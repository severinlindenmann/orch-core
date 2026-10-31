"""Split-user trust: all processes and card reads are fakes."""

import hashlib

import pytest

from orch.install import cli, server, trust
from orch.install.parts import InstallError
from orch.install.serverflow import server_setup
from orch.install.state import load, save

from .test_install_cli import _ROOT, HUB_ID, real_card
from .test_server_setup import local, stub_ctx


def edge(ctx, **kw):
    return server_setup(ctx, "edge", names=server.Names(), local=local, **kw)


def admins(calls):
    return [c for c in calls if len(c) > 1 and c[1] == "admin"]


def test_edge_trust_rerun_rotation_status_and_preserve_settings(tmp_path):
    ctx, out, _, calls = stub_ctx(tmp_path)
    edge(ctx, trust_hub=HUB_ID, address="192.168.1.20")
    prefix = [str(ctx.layout.bin / "orch-publish"), "admin", "--data", str(ctx.layout.data("publish")), "hub"]
    assert admins(calls) == [[*prefix, "trust", HUB_ID]]
    before = load(ctx.layout)
    assert before["edge_trust"]["hub_id"] == HUB_ID
    assert before["parts"]["publish"]["pinned_hub_id"] == HUB_ID
    calls.clear()
    edge(ctx, trust_hub=HUB_ID)
    assert admins(calls) == [[*prefix, "trust", HUB_ID]]
    assert load(ctx.layout) == before
    calls.clear()
    edge(ctx, address="192.168.1.20")
    assert admins(calls) == [[*prefix, "trust", HUB_ID]]
    calls.clear()
    edge(ctx, trust_hub="b" * 32)
    assert admins(calls) == [[*prefix, "untrust", HUB_ID], [*prefix, "trust", "b" * 32]]
    out.seek(0)
    out.truncate()
    cli.status(ctx, False)
    assert f"Publish trusts hub {'b' * 32}\n" in out.getvalue()


@pytest.mark.parametrize("bad", ["A" * 32, "a" * 31, "g" * 32, "a" * 32 + "\n", ""])
def test_bad_id_is_one_line_before_changes(tmp_path, bad):
    ctx, _, _, calls = stub_ctx(tmp_path)
    with pytest.raises(InstallError, match="^--trust-hub needs exactly 32 lowercase hex characters$"):
        edge(ctx, trust_hub=bad)
    assert not calls and not ctx.layout.file.exists()


def test_hub_refuses_flag_and_prints_hint(tmp_path):
    ctx, out, _, calls = stub_ctx(tmp_path)
    with pytest.raises(InstallError, match="--trust-hub belongs to --role edge"):
        server_setup(ctx, "hub", names=server.Names(), trust_hub=HUB_ID, local=local)
    assert not calls
    server_setup(ctx, "hub", names=server.Names(), local=local)
    assert (
        "On the computer user that runs Publish, run: "
        f"`orch setup --server --role edge --trust-hub {HUB_ID}` (your other edge settings stay).\n"
    ) in out.getvalue()


@pytest.mark.parametrize("reachable", [True, False])
def test_trust_line_with_optional_fingerprint(tmp_path, monkeypatch, reachable):
    from orch.hubclient import connectlink
    from orch.hubclient.errors import HubRefused

    card = real_card(root_pub=_ROOT)

    def fetch(link):
        assert link.card_url == "https://hub.example.com/api/v1/hub/card"
        if not reachable:
            raise HubRefused("hub_unreachable")
        return card

    monkeypatch.setattr(connectlink, "fetch_card", fetch)
    ctx, out, _, _ = stub_ctx(tmp_path)
    edge(ctx, trust_hub=HUB_ID, address="192.168.1.20")
    st = load(ctx.layout)
    st["edge_trust"]["origin"] = "https://hub.example.com"
    save(ctx.layout, st)
    out.seek(0)
    out.truncate()
    trust.apply(ctx, ctx.env)
    fp = hashlib.sha256(card["o"]["hk_root_pub"].encode()).hexdigest()[:12]
    assert out.getvalue() == f"Publish trusts hub {HUB_ID}" + (f" (hub card fingerprint {fp})\n" if reachable else "\n")


def test_setup_and_install_flag(tmp_path):
    from orch.setup.flow import main

    ctx, out, err, calls = stub_ctx(tmp_path)
    for command in (cli.main, main):
        args = ["server"] if command is cli.main else ["--server"]
        # Role refusal proves both entry points forward the flag without running any process.
        assert (
            command(
                [*args, "--role", "hub", "--trust-hub", HUB_ID],
                env=ctx.env,
                out=out,
                err=err,
                **({"ctx": ctx} if command is cli.main else {"install_ctx": ctx}),
            )
            == 1
        )
    assert not calls


def test_update_reapplies_trust(tmp_path, monkeypatch):
    import io
    from pathlib import Path

    from orch.lifecycle.units import Env
    from orch.update.services import LifecycleServices

    ctx, out, _, calls = stub_ctx(tmp_path)
    edge(ctx, trust_hub=HUB_ID, address="192.168.1.20")
    monkeypatch.setattr(cli, "default_ctx", lambda *args: ctx)
    services = LifecycleServices(Env(env=ctx.env, out=out, err=io.StringIO()))
    monkeypatch.setattr(services, "_svcs", lambda: [])
    calls.clear()
    services.start(Path("/fake/orch"), ["publish"], quarantine=False)
    assert [c[-2:] for c in admins(calls)] == [["trust", HUB_ID]]


def test_failed_untrust_never_trusts_replacement(tmp_path):
    ctx, _, _, calls = stub_ctx(tmp_path)
    edge(ctx, trust_hub=HUB_ID, address="192.168.1.20")
    calls.clear()
    original = ctx.run

    def fail(argv, env=None):
        if "untrust" in argv:
            calls.append(argv)
            return 1, "refused"
        return original(argv, env)

    ctx.run = fail
    with pytest.raises(InstallError, match="refused"):
        edge(ctx, trust_hub="b" * 32)
    assert [c[-2:] for c in admins(calls)] == [["untrust", HUB_ID]]
    assert load(ctx.layout)["parts"]["publish"]["pinned_hub_id"] == HUB_ID


def test_other_hub_card_has_no_fingerprint(monkeypatch):
    from orch.hubclient import connectlink

    monkeypatch.setattr(connectlink, "fetch_card", lambda link: real_card())
    assert trust.fingerprint("https://hub.example.com", HUB_ID) is None
