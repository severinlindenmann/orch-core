"""Security gates in the active signing workflow and its artifact verifier."""

import hashlib
import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
JOBS = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())["jobs"]
spec = importlib.util.spec_from_file_location("wheel_hashes", ROOT / "scripts/release/wheel_hashes.py")
hashes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hashes)


def test_every_job_disables_caches_and_pins_uv():
    for job in JOBS.values():
        assert job["env"]["UV_NO_CACHE"] == "1"
        setups = [s for s in job["steps"] if s.get("uses", "").startswith("astral-sh/setup-uv@")]
        assert setups
        for setup in setups:
            assert setup["with"]["enable-cache"] is False
            assert setup["with"]["version"] == "0.13.3"
        for step in job["steps"]:
            assert not step.get("uses", "").startswith("actions/cache")
            assert "${{" not in step.get("run", "")
            if step.get("uses", "").startswith("actions/setup-node@"):
                assert "cache" not in step.get("with", {})


def test_build_dependency_hashes_are_required():
    build = next(s["run"] for s in JOBS["build"]["steps"] if "uv build" in s.get("run", ""))
    assert "--build-constraint scripts/release/build-constraints.txt" in build
    assert "--require-hashes" in build and "--require-build-hashes" in build
    constraints = (ROOT / "scripts/release/build-constraints.txt").read_text()
    assert "hatchling==1.27.0" in constraints
    assert "--hash=sha256:" in constraints


def test_smoke_and_publish_gate_on_built_hashes():
    smoke = JOBS["smoke"]
    assert "build" in smoke["needs"] and "smoke" in JOBS["publish"]["needs"]
    assert "environment" not in smoke and "secrets." not in str(smoke)
    assert any(s.get("uses", "").startswith("actions/download-artifact@") for s in smoke["steps"])
    install = next(s["run"] for s in smoke["steps"] if "uv pip install" in s.get("run", ""))
    assert "--require-hashes" in install and "-r dist/constraints.txt" in install
    assert "-r smoke-requirements.txt" in install and "wheel_hashes.py" in install
    assert "uv tool install" not in str(JOBS)
    steps = JOBS["publish"]["steps"]
    check = next(i for i, s in enumerate(steps) if "wheel_hashes.py" in s.get("run", ""))
    sign = next(i for i, s in enumerate(steps) if "secrets.ORCH_RELEASE_KEY" in str(s))
    assert check < sign
    outputs = JOBS["build"]["outputs"]
    assert outputs["artifact_id"] == "${{ steps.upload.outputs.artifact-id }}"
    assert outputs["sums_sha256"] == "${{ steps.hashes.outputs.sums_sha256 }}"
    build_steps = JOBS["build"]["steps"]
    sums = next(s for s in build_steps if s.get("id") == "hashes")
    assert "sha256sum *.whl constraints.txt changelog.txt > SHA256SUMS" in sums["run"]
    assert "sha256sum dist/SHA256SUMS" in sums["run"]
    assert 'echo "sums_sha256=${digest%% *}" >> "$GITHUB_OUTPUT"' in sums["run"]
    assert build_steps.index(sums) < next(i for i, s in enumerate(build_steps) if s.get("id") == "upload")
    for name in ("smoke", "publish"):
        download = next(s for s in JOBS[name]["steps"] if s.get("uses", "").startswith("actions/download-artifact@"))
        assert download["with"]["artifact-ids"] == "${{ needs.build.outputs.artifact_id }}"
        assert "name" not in download["with"]
        assert download["with"]["merge-multiple"] is True
    gate = steps[check]
    assert gate["env"]["EXPECTED_SUMS_SHA256"] == "${{ needs.build.outputs.sums_sha256 }}"
    script = gate["run"]
    assert (
        script.index("sha256sum dist/SHA256SUMS")
        < script.index('= "$EXPECTED_SUMS_SHA256"')
        < script.index("wheel_hashes.py")
    )
    assert "exit 1" in script


def test_candidates_use_test_tags_everywhere():
    plan = next(s["run"] for s in JOBS["plan"]["steps"] if s.get("id") == "plan")
    assert 'if [ "$test" = true ]; then tag="test-v${version}"; fi' in plan
    assert 'echo "tag=$tag"' in plan
    publish = JOBS["publish"]
    assert publish["env"]["TAG"] == "${{ needs.plan.outputs.tag }}"
    text = str(publish)
    assert 'gh release create "$TAG"' in text
    assert "/releases/download/${TAG}" in text
    assert '--exclude-tag "$TAG"' in text


@pytest.fixture
def artifact(tmp_path):
    lines = []
    for name in ("core", "dashboard", "hub", "relay", "publish"):
        wheel = tmp_path / f"orch_{name}-2.0.0-py3-none-any.whl"
        wheel.write_bytes(name.encode())
        lines.append(f"{hashlib.sha256(wheel.read_bytes()).hexdigest()}  {wheel.name}\n")
    for name in ("constraints.txt", "changelog.txt"):
        path = tmp_path / name
        path.write_text(name)
        lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {name}\n")
    (tmp_path / "SHA256SUMS").write_text("".join(lines))
    return tmp_path


def test_wheel_hashes_accept_exact_artifact(artifact):
    assert hashes.verify(artifact, "2.0.0").count("--hash=sha256:") == 5


@pytest.mark.parametrize("damage", ["changed", "missing", "extra", "missing_hash", "duplicate_hash", "extra_hash"])
def test_wheel_hashes_fail_closed(artifact, damage):
    wheel = artifact / "orch_core-2.0.0-py3-none-any.whl"
    sums = artifact / "SHA256SUMS"
    lines = sums.read_text().splitlines(keepends=True)
    if damage == "changed":
        wheel.write_bytes(b"tampered")
    elif damage == "missing":
        wheel.unlink()
    elif damage == "extra":
        (artifact / "extra.whl").write_bytes(b"extra")
    elif damage == "missing_hash":
        sums.write_text("".join(lines[1:]))
    elif damage == "duplicate_hash":
        sums.write_text("".join(lines + lines[:1]))
    else:
        sums.write_text("".join(lines) + "a" * 64 + "  extra.whl\n")
    with pytest.raises(ValueError):
        hashes.verify(artifact, "2.0.0")


@pytest.mark.parametrize("name", ["constraints.txt", "changelog.txt"])
@pytest.mark.parametrize("damage", ["changed", "missing_hash"])
def test_metadata_is_hash_verified(artifact, name, damage):
    if damage == "changed":
        (artifact / name).write_text("tampered")
    else:
        sums = artifact / "SHA256SUMS"
        sums.write_text(
            "".join(line for line in sums.read_text().splitlines(keepends=True) if not line.rstrip().endswith(name))
        )
    with pytest.raises(ValueError):
        hashes.verify(artifact, "2.0.0")
