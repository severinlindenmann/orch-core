"""#1198 R1-7: the generated Caddyfile is accepted by a real Caddy (``caddy validate``: it loads and provisions the
configuration but binds nothing). Needs ORCH_CADDY_BIN, the path of the pinned Caddy that CI downloads
(.github/workflows/orch-core.yml); skipped when it is unset, a failure in CI."""

from __future__ import annotations

import os
import subprocess

import pytest

from orch.setup.osfront import FrontPlan

CADDY = os.environ.get("ORCH_CADDY_BIN")


@pytest.fixture(scope="module")
def caddy() -> str:
    if not CADDY:
        if os.environ.get("CI"):
            pytest.fail("CI must run this test: ORCH_CADDY_BIN is not set")
        pytest.skip("ORCH_CADDY_BIN is not set")
    return CADDY


CASES = {
    "plain": (),
    "ipv4": ("10.0.0.0/8", "203.0.113.5/32"),
    "ipv6": ("2001:db8::/32",),
    "mixed": ("10.0.0.0/8", "172.16.0.0/12", "2001:db8::/32"),
}


@pytest.mark.parametrize("case", CASES)
def test_caddy_accepts_the_generated_caddyfile(caddy, tmp_path, case):
    text = FrontPlan("orch.example.com", "sev", 1000, CASES[case]).caddyfile()
    path = tmp_path / "Caddyfile"
    path.write_text(text)
    env = {"HOME": str(tmp_path), "XDG_DATA_HOME": str(tmp_path / "d"), "XDG_CONFIG_HOME": str(tmp_path / "c")}
    done = subprocess.run(
        [caddy, "validate", "--adapter", "caddyfile", "--config", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert "Valid configuration" in done.stderr + done.stdout


def test_the_trusted_proxy_options_reach_the_adapted_config(caddy, tmp_path):
    path = tmp_path / "Caddyfile"
    path.write_text(FrontPlan("orch.example.com", "sev", 1000, ("10.0.0.0/8",)).caddyfile())
    done = subprocess.run(
        [caddy, "adapt", "--adapter", "caddyfile", "--config", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
        env={"HOME": str(tmp_path)},
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    for want in ('"trusted_proxies_strict"', '"ranges":["10.0.0.0/8"]', "{http.vars.client_ip}", '"h1"', '"h2"'):
        assert want in done.stdout.replace(" ", ""), want
    assert '"h3"' not in done.stdout
