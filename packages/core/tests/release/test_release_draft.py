"""Invariants of scripts/release/release.yml.draft (the manager copies it to .github/workflows/release.yml)."""

from __future__ import annotations

import re
from pathlib import Path

DRAFT = (Path(__file__).resolve().parents[4] / "scripts" / "release" / "release.yml.draft").read_text()


def _steps() -> list[tuple[str, str]]:
    """(step name, step text) for every step with a ``- name:`` line."""
    parts = re.split(r"(?m)^      - (?=name:|uses:|id:)", DRAFT)
    out = []
    for p in parts[1:]:
        m = re.match(r"name: (.*)", p)
        out.append((m.group(1) if m else "", p))
    return out


def test_the_key_is_read_only_by_sign_steps():
    users = [(n, t) for n, t in _steps() if "secrets." in t]
    assert users and all(n.startswith("Sign the manifest") for n, _ in users), [n for n, _ in users]
    assert DRAFT.count("secrets.") == len(users)  # nowhere else in the file (jobs, env blocks)


def test_signing_jobs_use_the_gated_environments():
    assert "environment: ${{ needs.plan.outputs.test == 'true' && 'release-test' || 'release' }}" in DRAFT
    assert "    environment: release\n" in DRAFT  # the refresh job
    assert "allow ONLY `main` and `develop`" in DRAFT  # the branch policy is documented in the header


def test_no_develop_channel_and_only_main_or_test_channels():
    assert "branches: [main]" in DRAFT and "branches: [main, develop]" not in DRAFT
    assert "test-[a-z0-9][a-z0-9-]{0,30}" in DRAFT
    assert not re.search(r"channel[^\n]*\bdevelop\b", DRAFT.split("\nname: release\n", 1)[1])


def test_the_build_job_holds_no_secret_and_the_prebuilt_dashboard_is_required():
    build = DRAFT.split("\n  build:\n", 1)[1].split("\n  publish:\n", 1)[0]
    assert "secrets." not in build and "environment:" not in build
    assert "ORCH_DASHBOARD_REQUIRE_PREBUILT: '1'" in build and "npm run build" in build


def test_a_weekly_refresh_exists_and_signs_only_the_verified_pointer():
    assert "cron:" in DRAFT and "mode == 'refresh'" in DRAFT
    refresh = DRAFT.split("\n  refresh:\n", 1)[1]
    assert "scripts/release/refresh.py" in refresh and "scripts/release/manifest.py" not in refresh
