"""Invariants of the release workflow, .github/workflows/release.yml (the one source; the old draft copy is gone)."""

from __future__ import annotations

import re
from pathlib import Path

DRAFT = (Path(__file__).resolve().parents[4] / ".github" / "workflows" / "release.yml").read_text()


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
    assert "allow ONLY `main` (NOT develop" in DRAFT  # the branch policy is documented in the header
    assert "allow ONLY `main` and `develop`" not in DRAFT
    assert "can reach the key" in DRAFT and "its OWN key" in DRAFT


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


def test_no_install_or_build_work_in_a_step_that_holds_the_key():
    keyed = [(n, t) for n, t in _steps() if "ORCH_RELEASE_SIGNING_KEY" in t]
    assert len(keyed) == 2
    for name, text in keyed:
        assert "--no-sync" in text, name
        assert not re.search(r"uv sync|uv build|uv pip|npm |pip install", text), name
    # the environment those steps run with is installed earlier, without the secret
    # (#1873: with no cache, so nothing a poisoned shared cache holds reaches a signing step)
    syncs = re.findall(r"uv sync [^\n]*--project packages/core --no-dev[^\n]*", DRAFT)
    assert len(syncs) == 2 and all("--no-cache" in s and "--frozen" in s for s in syncs), syncs
    assert not re.search(r"uv run (?!--no-sync)", DRAFT)


def test_tag_protection_and_immutable_releases_are_marked_required():
    assert "REQUIRED (the refresh anchor list is git tags" in DRAFT and "immutable releases" in DRAFT


def test_constraints_are_resolved_with_an_age_cutoff_and_the_residual_is_documented():
    assert '--exclude-newer "$cutoff"' in DRAFT and "cdate - 7*86400" in DRAFT
    assert "Pilot residual (constraints)" in DRAFT


def test_seq_is_run_number_plus_offset_and_the_pointer_guard_fails_closed():
    assert "RUN_NUMBER + SEQ_OFFSET" in DRAFT and "RELEASE_SEQ_OFFSET" in DRAFT
    assert "scripts/release/channel_seq.py" in DRAFT and "REFUSES to move the pointer" in DRAFT


def test_publish_and_refresh_checkouts_fetch_the_full_history_for_the_tag_list():
    for job in ("publish", "refresh"):
        text = DRAFT.split(f"\n  {job}:\n", 1)[1].split("\n  refresh:\n" if job == "publish" else "\0", 1)[0]
        checkout = text.split("actions/checkout@", 1)[1].split("- uses: astral-sh", 1)[0]
        assert "fetch-depth: 0" in checkout, job


def test_seq_offset_is_validated_before_the_shell_arithmetic():
    i_check = DRAFT.index('[[ "$SEQ_OFFSET" =~ ^[0-9]{1,9}$ ]]')
    assert i_check < DRAFT.index("seq=$((RUN_NUMBER + SEQ_OFFSET))")


def test_version_and_seq_of_a_test_candidate_derive_from_the_same_number():
    assert 'version="${base}.dev${seq}"' in DRAFT and "RUN_NUMBER}" not in DRAFT.replace("${RUN_NUMBER + ", "")
    assert "dev${RUN_NUMBER}" not in DRAFT


def test_the_dependency_cutoff_is_min_of_commit_date_and_now():
    assert '[ "$cdate" -le "$now" ] || cdate=$now' in DRAFT and "cdate - 7*86400" in DRAFT
