"""Add-on skills on disk (design addon-skills D2): the writer, the reconciler, the quarantine, worktrees, the
``.gitignore`` lines, the sidecar and the paths that must stay safe."""

from __future__ import annotations

import hashlib
import json
import os
import time

import pytest

from orch.instructions import (
    AddonRow,
    UnsafePath,
    check_sidecar,
    stale_findings,
    sync_addon_part,
    write_workspace_files,
)
from orch.instructions.addon_skills import IGNORE_LINES, reconcile, skill_rels
from orch.instructions.harness import COPY_HEADER
from orch.instructions.skills import SkillError

TEXT = "---\nname: orch-addon-notes\ndescription: Use when notes. Not for logs.\n---\n# Notes\n\nSteps here.\n"
CANON = ".agents/skills/orch-addon-notes"
CLAUDE = ".claude/skills/orch-addon-notes"
DIGEST = "sha256:" + "a" * 64
NOW = 1_790_000_000.0
STAMP = time.strftime("%Y%m%dT%H%M%S", time.gmtime(NOW))


def row(name="notes", state="active", text=TEXT, digest=DIGEST, version="1.0.0"):
    return AddonRow(name, state, version, digest, "Use when notes. Not for logs.", text.encode() if text else None)


def sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def root(tmp_path):
    (tmp_path / ".orch").mkdir()
    return tmp_path


def sync(root, rows, **kw):
    return sync_addon_part(root, rows, store=root / ".orch", now=NOW, **kw)


def test_an_active_skill_is_written_to_both_places_with_its_sidecar(root):
    changed, kept = sync(root, [row()])
    assert kept == []
    assert (root / CANON / "SKILL.md").read_text() == TEXT
    claude = (root / CLAUDE / "SKILL.md").read_text()
    assert COPY_HEADER in claude and claude.replace(COPY_HEADER + "\n", "") == TEXT
    for base, text in ((CANON, TEXT), (CLAUDE, claude)):
        side = json.loads((root / base / "orch.skill.json").read_text())
        assert side == {
            "scope": "addon", "addon": "notes", "version": "1.0.0", "package_sha256": DIGEST, "text_sha256": sha(text),
        }  # fmt: skip
        assert check_sidecar(side) == side
    assert {f"{CANON}/SKILL.md", f"{CANON}/orch.skill.json", f"{CLAUDE}/SKILL.md", ".gitignore"} <= set(changed)


def test_a_second_sync_changes_nothing(root):
    sync(root, [row()])
    assert sync(root, [row()]) == ([], [])


def test_an_update_rewrites_the_text_and_the_sidecar(root):
    sync(root, [row()])
    new = TEXT.replace("Steps here.", "New steps.")
    changed, _ = sync(root, [row(text=new, digest="sha256:" + "b" * 64, version="1.1.0")])
    assert (root / CANON / "SKILL.md").read_text() == new
    assert json.loads((root / CANON / "orch.skill.json").read_text())["version"] == "1.1.0"
    assert f"{CANON}/SKILL.md" in changed


def test_the_gitignore_gets_the_generated_folders_once(root):
    (root / ".gitignore").write_text("node_modules/\n")
    sync(root, [row()])
    sync(root, [row()])
    lines = (root / ".gitignore").read_text().splitlines()
    assert lines[0] == "node_modules/" and [x for x in lines if "orch-addon" in x] == list(IGNORE_LINES)
    assert IGNORE_LINES == (".agents/skills/orch-addon-*/", ".claude/skills/orch-addon-*/")


def test_no_skill_means_no_files_and_no_gitignore_churn(root):
    assert sync(root, [row(text=None)]) == ([".orch/AGENTS.orch.md"], [])
    assert not (root / ".gitignore").exists() and not (root / ".agents").exists()


@pytest.mark.parametrize("state", ["disabled", "missing", "changed", "incompatible"])
def test_a_skill_of_an_addon_that_is_not_active_is_removed_when_unedited(root, state):
    sync(root, [row()])
    changed, kept = sync(root, [row(state=state)])
    assert kept == [f"removed {CANON}", f"removed {CLAUDE}"] and not (root / CANON).exists()
    assert not (root / CLAUDE).exists() and (root / ".agents/skills").is_dir()
    assert not (root / ".orch" / "removed-skills").exists()


def test_a_purged_addon_is_gone_from_the_rows_and_its_skill_goes_too(root):
    sync(root, [row()])
    sync(root, [])
    assert not (root / CANON).exists()


def test_a_hand_edited_skill_is_moved_to_removed_skills_not_deleted(root):
    sync(root, [row()])
    (root / CANON / "SKILL.md").write_text(TEXT + "my own rule\n")
    _, kept = sync(root, [row(state="disabled")])
    moved = root / ".orch" / "removed-skills" / f"orch-addon-notes-{STAMP}"
    assert f"moved {CANON} (edited by hand) to .orch/removed-skills/orch-addon-notes-{STAMP}" in kept
    assert (moved / "SKILL.md").read_text().endswith("my own rule\n") and not (root / CANON).exists()
    assert not (root / CLAUDE).exists()  # the unedited Claude copy was simply removed


def test_a_second_quarantine_in_the_same_second_gets_its_own_folder(root):
    for _ in range(2):
        sync(root, [row()])
        (root / CANON / "SKILL.md").write_text("edited\n")
        sync(root, [row(state="disabled")])
    assert sorted(p.name for p in (root / ".orch/removed-skills").iterdir()) == [
        f"orch-addon-notes-{STAMP}",
        f"orch-addon-notes-{STAMP}-2",
    ]


def test_an_extra_file_in_the_folder_counts_as_an_edit(root):
    sync(root, [row()])
    (root / CANON / "mine.md").write_text("x")
    _, kept = sync(root, [row(state="disabled")])
    assert any(x.startswith(f"moved {CANON}") for x in kept)


def test_an_edited_skill_of_an_active_addon_is_kept_unless_forced(root):
    sync(root, [row()])
    (root / CANON / "SKILL.md").write_text("edited\n")
    _, kept = sync(root, [row()])
    assert kept == [f"kept {CANON} (edited by hand; --force overwrites it)"]
    assert (root / CANON / "SKILL.md").read_text() == "edited\n"
    sync(root, [row()], force=True)
    assert (root / CANON / "SKILL.md").read_text() == TEXT


def test_a_folder_core_did_not_write_is_never_touched(root):
    (root / CANON).mkdir(parents=True)
    (root / CANON / "SKILL.md").write_text("the owner's own skill\n")  # no sidecar
    other = root / ".agents/skills/orch-addon-other"
    other.mkdir()
    (other / "SKILL.md").write_text("mine\n")
    (other / "orch.skill.json").write_text(json.dumps({"scope": "workspace"}))  # another scope
    _, kept = sync(root, [row()])
    assert kept == [f"kept {CANON} (not orch's: no add-on sidecar)"]
    assert (root / CANON / "SKILL.md").read_text() == "the owner's own skill\n"
    sync(root, [])  # nothing is wanted: still left alone
    assert (other / "SKILL.md").read_text() == "mine\n" and (root / CANON / "SKILL.md").exists()


def test_a_sidecar_of_another_addon_is_not_taken_over(root):
    sync(root, [row()])
    side = json.loads((root / CANON / "orch.skill.json").read_text())
    (root / CANON / "orch.skill.json").write_text(json.dumps({**side, "addon": "wiki"}))
    _, kept = sync(root, [row()])
    assert kept[0].startswith(f"kept {CANON}") and (root / CANON / "SKILL.md").read_text() == TEXT


def test_builtin_skills_and_their_sidecars_are_untouched(root):
    write_workspace_files(root, store=root / ".orch", addons=[row()])
    assert (root / ".agents/skills/orch-tickets/SKILL.md").exists() and (root / CANON / "SKILL.md").exists()
    write_workspace_files(root, store=root / ".orch", addons=[])
    assert (root / ".agents/skills/orch-tickets/SKILL.md").exists() and not (root / CANON).exists()
    assert stale_findings(root, root / ".orch", []) == []


def test_a_worktree_gets_the_same_copies_and_loses_them_too(root, tmp_path_factory):
    wt = tmp_path_factory.mktemp("wt")
    changed, _ = sync(root, [row()], worktrees=[wt])
    assert (wt / CANON / "SKILL.md").read_text() == TEXT and (wt / CLAUDE / "SKILL.md").exists()
    assert f"{wt}/{CANON}/SKILL.md" in changed and ".gitignore" in changed
    assert not (wt / ".gitignore").exists()  # the repo's own ignore file is the root's
    (wt / CANON / "SKILL.md").write_text("edited\n")
    _, kept = sync(root, [row(state="disabled")], worktrees=[wt])
    assert f"removed {root / CANON}" not in kept and any(x.startswith(f"moved {wt}/{CANON}") for x in kept)
    assert not (wt / CLAUDE).exists() and not (root / CANON).exists()
    assert len(list((root / ".orch/removed-skills").iterdir())) == 1  # the quarantine is the workspace's, one place


def test_a_bad_worktree_is_a_line_not_a_failure_and_the_others_are_done(root, tmp_path_factory):
    good, linked = tmp_path_factory.mktemp("good"), tmp_path_factory.mktemp("lk")
    link = linked / "link"
    link.symlink_to(good)
    unsafe = tmp_path_factory.mktemp("unsafe")
    (unsafe / ".agents").symlink_to(good)  # .agents is a link: no write through it
    _, kept = sync(root, [row()], worktrees=[link, "relative", unsafe, good])
    assert (good / CANON / "SKILL.md").exists()
    text = "\n".join(kept)
    assert f"skipped worktree {link}" in text and "skipped worktree relative" in text
    assert f"could not update worktree {unsafe}" in text and "symbolic link" in text


def test_the_root_is_not_done_twice_when_it_is_also_a_worktree(root):
    changed, kept = sync(root, [row()], worktrees=[root, str(root)])
    assert kept == [] and changed.count(f"{CANON}/SKILL.md") == 1


def test_a_link_in_the_root_stops_everything_before_a_write(root, tmp_path_factory):
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    (root / ".agents").symlink_to(elsewhere)
    with pytest.raises(UnsafePath, match="symbolic link"):
        sync(root, [row()])
    assert list(elsewhere.iterdir()) == []


def test_a_linked_skill_folder_is_not_followed_on_removal(root, tmp_path_factory):
    sync(root, [row()])
    victim = tmp_path_factory.mktemp("victim")
    (victim / "SKILL.md").write_text("precious\n")
    for p in (root / CANON).iterdir():
        p.unlink()
    (root / CANON).rmdir()
    (root / CANON).symlink_to(victim)
    sync(root, [row(state="disabled")])  # not a folder core wrote (a link): left exactly as it is
    assert (victim / "SKILL.md").read_text() == "precious\n" and (root / CANON).is_symlink()


def test_dry_run_writes_nothing(root):
    changed, _ = sync(root, [row()], dry_run=True)
    assert f"{CANON}/SKILL.md" in changed and not (root / ".agents").exists() and not (root / ".gitignore").exists()
    sync(root, [row()])
    (root / CANON / "SKILL.md").write_text("edited\n")
    _, lines = sync(root, [row(state="disabled")], dry_run=True)
    assert f"would move {CANON}" in lines and (root / CANON / "SKILL.md").read_text() == "edited\n"


def test_the_paths_to_check_are_listed_for_the_pre_write_check():
    assert f"{CANON}/SKILL.md" in skill_rels([row()]) and skill_rels([row(text=None), row("x", "disabled")]) == []


def test_reconcile_reports_in_the_form_the_sync_prints(root):
    changed, lines = reconcile(root, [row()], store=root / ".orch", now=NOW)
    assert lines == [] and f"{CANON}/SKILL.md" in changed


# ---- the sidecar


@pytest.mark.parametrize(
    "bad",
    [
        {"scope": "addon"},
        {"scope": "addon", "addon": "Notes", "version": "1", "package_sha256": DIGEST, "text_sha256": DIGEST},
        {"scope": "addon", "addon": "notes", "version": "", "package_sha256": DIGEST, "text_sha256": DIGEST},
        {"scope": "addon", "addon": "notes", "version": "1", "package_sha256": "x", "text_sha256": DIGEST},
        {"scope": "addon", "addon": "notes", "version": "1", "package_sha256": DIGEST, "text_sha256": DIGEST, "x": 1},
    ],
)
def test_the_addon_sidecar_is_exact(bad):
    with pytest.raises(SkillError):
        check_sidecar(bad)


# ---- review of #1327: order, budget, worktrees in the stale check, names, legacy layout


def test_the_stamp_is_the_commit_marker_a_failed_skill_leaves_the_old_one(root, tmp_path_factory):
    from orch.instructions import render_agents_md, stamp_set

    sync(root, [])
    before = (root / ".orch/AGENTS.orch.md").read_text()
    (root / ".agents").mkdir()
    (root / ".agents/skills").symlink_to(tmp_path_factory.mktemp("elsewhere"))
    with pytest.raises(UnsafePath):
        sync(root, [row()])
    assert (root / ".orch/AGENTS.orch.md").read_text() == before and stamp_set(before) == ""
    assert stamp_set(render_agents_md([row()])) != ""


def test_write_workspace_files_checks_the_skill_paths_before_it_writes(root, tmp_path_factory):
    (root / ".claude").mkdir()
    (root / ".claude/skills").symlink_to(tmp_path_factory.mktemp("elsewhere"))
    with pytest.raises(UnsafePath):
        write_workspace_files(root, store=root / ".orch", addons=[row()])
    assert not (root / ".orch/AGENTS.orch.md").exists() and not (root / ".agents").exists()


def test_removals_run_even_when_the_block_is_over_budget(root):
    from orch.instructions import AddonBlockError

    sync(root, [row()])
    many = [AddonRow(f"a{i:02d}", "active", "1", DIGEST, "Use when x.", None) for i in range(13)]
    with pytest.raises(AddonBlockError):
        sync(root, [*many, row(state="disabled")])
    assert not (root / CANON).exists() and not (root / CLAUDE).exists()  # the disabled add-on's skill went anyway
    with pytest.raises(AddonBlockError):
        write_workspace_files(root, store=root / ".orch", addons=[*many])


def test_the_off_line_is_capped_with_a_count():
    from orch.instructions import render_agents_md

    off = [AddonRow(f"addon-number-{i:03d}", "disabled", "1", DIGEST, "Use when x.") for i in range(200)]
    text = render_agents_md([row(), *off])
    line = next(x for x in text.splitlines() if x.startswith("Off here:"))
    assert " more. If a request" in line and len(text) < 3000 and "addon-number-000 (disabled)" in line


def test_names_that_are_no_addon_names_are_never_a_folder():
    from orch.instructions.addon_skills import wanted

    bad = [row("../x"), row("a/b"), row("Notes"), row("a" * 41), row("x\n")]
    assert wanted(bad) == {} and list(wanted([row("ok-1")])) == ["orch-addon-ok-1"]


def test_a_listing_ignores_folders_whose_suffix_is_no_addon_name(root):
    odd = root / ".agents/skills/orch-addon-Odd_Name"
    odd.mkdir(parents=True)
    (odd / "orch.skill.json").write_text("{}")
    sync(root, [])
    assert odd.exists()


def test_the_legacy_layout_makes_no_store_folder_for_a_quarantine(tmp_path):
    sync_addon_part(tmp_path, [row()], store=None, now=NOW)
    (tmp_path / CANON / "SKILL.md").write_text("edited\n")
    _, kept = sync_addon_part(tmp_path, [row(state="disabled")], store=None, now=NOW)
    assert any(x.startswith(f"kept {CANON} (edited by hand, could not move it") for x in kept)
    assert not (tmp_path / ".orch").exists() and (tmp_path / CANON / "SKILL.md").read_text() == "edited\n"


def test_an_unreadable_folder_is_a_line_not_a_crash(root, monkeypatch):
    sync(root, [row()])
    real = os.listdir

    def listdir(path):
        if str(path).endswith(CANON):
            raise PermissionError(13, "denied")
        return real(path)

    monkeypatch.setattr(os, "listdir", listdir)
    _, kept = sync(root, [row(state="disabled")])
    assert any(x.startswith(f"kept {CANON} (cannot be read") for x in kept)


def test_the_stale_check_covers_each_worktree_and_session_start_repairs_it(root, tmp_path_factory):
    from orch.instructions import sync_if_changed

    def addon_findings(*a):  # the built-in skills are not written by the add-on part of the sync
        return [
            f
            for f in stale_findings(*a)
            if "orch-tickets" not in f["where"] and "orch-work" not in f["where"] and "orch-refine" not in f["where"]
        ]

    wt = tmp_path_factory.mktemp("wt")
    rows = [row()]
    sync(root, rows, worktrees=[wt])
    assert addon_findings(root, root / ".orch", rows, [wt]) == []
    # a new worktree appears, an old copy of a disabled add-on survives in another
    new = tmp_path_factory.mktemp("new")
    found = addon_findings(root, root / ".orch", rows, [wt, new])
    assert [(f["where"], f["what"]) for f in found] == [(f"{new}/{CANON}", "missing"), (f"{new}/{CLAUDE}", "missing")]
    assert sync_if_changed(root, rows, store=root / ".orch", worktrees=[wt, new])  # the stamp matches; the check fails
    assert (new / CANON / "SKILL.md").read_text() == TEXT and addon_findings(
        root, root / ".orch", rows, [wt, new]
    ) == []
    off = [row(state="disabled")]
    assert [f["what"] for f in addon_findings(root, root / ".orch", off, [wt])][:1] == [
        "the add-ons changed since it was written"
    ]
    sync(root, off, worktrees=[wt])
    (wt / CANON).mkdir(parents=True)
    (wt / CANON / "SKILL.md").write_text(TEXT)
    (wt / CANON / "orch.skill.json").write_text(
        json.dumps(
            {"scope": "addon", "addon": "notes", "version": "1", "package_sha256": DIGEST, "text_sha256": sha(TEXT)}
        )
    )
    assert any(f["what"] == "the add-on is not active" for f in addon_findings(root, root / ".orch", off, [wt]))
    assert sync_if_changed(root, off, store=root / ".orch", worktrees=[wt])
    assert not (wt / CANON).exists()


class FakeStore:
    def __init__(self, root, addons):
        from types import SimpleNamespace as NS

        self.layout = NS(store=root / ".orch")
        self.state = NS(workspace=NS(addons=addons))
        self.locks = 0

    def locked(self):
        import contextlib

        @contextlib.contextmanager
        def cm():
            self.locks += 1
            yield

        return cm()


def test_the_host_refresh_takes_the_store_lock_and_tells_about_a_quarantine(root):
    from orch.host import addonsync
    from orch.model.types import Addon

    store = FakeStore(root, {})
    sync(root, [row()])
    (root / CANON / "SKILL.md").write_text("edited\n")
    lines = addonsync.refresh(root, store)
    assert store.locks == 1 and any(x.startswith(f"moved {CANON}") for x in lines)
    assert not any(x.startswith("removed") for x in lines)
    assert Addon  # (the model type the real store holds)


def test_after_never_raises_whatever_the_service_looks_like():
    from orch.host import addonsync

    assert addonsync.after(object(), [{"type": "ticket.created"}]) == []
    got = addonsync.after(object(), [{"type": "addon.granted"}])
    assert got and got[0].startswith("could not refresh the agent instructions")
