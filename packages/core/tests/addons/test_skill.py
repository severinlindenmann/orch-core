"""The skill of an add-on (design addon-skills D1): the manifest key, the package rule, every refusal of the validator,
the command lint, and the pack warnings."""

from __future__ import annotations

import json

import pytest

from orch.addons import skill
from orch.addons.manifest import ManifestError, load_manifest, load_package_manifest
from orch.addons.package import from_files
from orch.market import MarketError
from orch.market import install as mi

GOOD = """---
name: orch-addon-notes
description: Use when the person asks for a note on a ticket. Not for the changelog (use orch-addon-changelog).
---
# Notes add-on (active)

Use when: the person says "add a note".
Do not use when: they want a changelog entry.

## Steps
1. Run `orch set REF notes.note="text"`.
2. Run `orch show` and check the note.

## If orch refuses
`addon.off`: tell the person this add-on is off here; do not work around it.
"""
BASE = {
    "schema": "orch.addon/3", "name": "notes", "version": "1.0.0", "title": "Notes", "api": ">=1.2,<2",
    "min_core": "2.0.0", "entry": {"cmd": ["{python}", "{pkg}/main.py"]}, "skill": "skills/main",
    "fields": {"note": {"type": "string", "max_len": 100, "set_by": ["agent"]}},
    "agents_md": "Use when the person wants a note. Not for the changelog.",
}  # fmt: skip


def files(text: str | bytes = GOOD, **over):
    man = {**BASE, **over}
    data = text if isinstance(text, bytes) else text.encode()
    return {"orch-addon.json": json.dumps(man).encode(), "main.py": b"", "skills/main/SKILL.md": data}


def check(text: str | bytes = GOOD, **over):
    f = files(text, **over)
    skill.check_package(load_manifest(f["orch-addon.json"]), f)


def refused(text: str | bytes, match: str, **over):
    with pytest.raises(skill.SkillError, match=match):
        check(text, **over)


def test_a_good_skill_passes_and_warns_about_nothing():
    check()
    f = files()
    assert skill.warnings(load_manifest(f["orch-addon.json"]), f) == []


def test_a_package_without_a_skill_is_unchanged():
    f = files()
    man = {k: v for k, v in BASE.items() if k != "skill"}
    f["orch-addon.json"] = json.dumps(man).encode()
    skill.check_package(load_manifest(f["orch-addon.json"]), f)


@pytest.mark.parametrize("extra", ["skills/main/notes.md", "skills/main/sub/x.md", "skills/main/orch.skill.json"])
def test_the_folder_holds_exactly_skill_md(extra):
    f = files()
    f[extra] = b"x"
    with pytest.raises(skill.SkillError, match="exactly SKILL.md"):
        skill.check_package(load_manifest(f["orch-addon.json"]), f)


def test_a_missing_skill_file_is_refused():
    f = files()
    del f["skills/main/SKILL.md"]
    with pytest.raises(skill.SkillError, match="exactly SKILL.md"):
        skill.check_package(load_manifest(f["orch-addon.json"]), f)


def test_the_name_must_be_the_installed_name():
    refused(GOOD.replace("name: orch-addon-notes", "name: orch-addon-other"), "must be orch-addon-notes")
    refused(GOOD.replace("name: orch-addon-notes", "name: notes"), "must be orch-addon-notes")


def test_the_frontmatter_is_exactly_name_and_description():
    extra = GOOD.replace("description:", "allowed-tools: Bash\ndescription:")
    refused(extra, "exactly name and description")
    refused(GOOD.replace("name: orch-addon-notes\n", ""), "exactly name and description")
    refused("no frontmatter\n", "start with ---")


@pytest.mark.parametrize("desc", ["x" * 301, "has a tab\there", "bell\x07"])
def test_the_description_is_1_to_300_printable_characters(desc):
    text = GOOD.replace(GOOD.splitlines()[2], f"description: {desc}")
    refused(text, "description|frontmatter|not printable")


def test_the_description_at_300_passes():
    check(GOOD.replace(GOOD.splitlines()[2], "description: " + "d" * 300))


def test_the_text_is_at_most_2400_characters():
    check(GOOD + "x" * (2400 - len(GOOD)))
    refused(GOOD + "x" * (2401 - len(GOOD)), "2401 characters")


@pytest.mark.parametrize(("text", "match"), [(GOOD + "\x00", "NUL"), (GOOD.replace("\n", "\r\n"), "CR")])
def test_no_nul_and_no_cr(text, match):
    refused(text, match)


def test_the_file_is_utf8():
    refused(b"---\n\xff\xfe", "not UTF-8")


def test_unknown_commands_are_an_error():
    refused(GOOD.replace("`orch show`", "`orch frobnicate now`"), "not an orch command")
    refused(GOOD.replace("`orch show`", "`orch task`"), "not an orch command")  # a group word is not a command


def test_commands_in_a_fenced_block_are_linted_too():
    block = GOOD.replace("2. Run `orch show` and check the note.", "```\n$ orch frobnicate\n```")
    refused(block, "not an orch command")
    check(GOOD.replace("2. Run `orch show` and check the note.", "```\norch show\n```"))


def test_core_commands_resolve_and_non_commands_are_ignored():
    check(GOOD.replace("`orch show`", "`orch task done T1 --run`, `ls`, `orchestra`, and `orch help work`"))


def test_orch_set_may_name_only_its_own_fields():
    refused(GOOD.replace("notes.note=", "other.note="), "own fields")
    refused(GOOD.replace("notes.note=", "notes.nope="), "own fields")
    check(
        GOOD.replace('`orch set REF notes.note="text"`', "`orch set REF title=x`")
    )  # a core key is not an add-on field


@pytest.mark.parametrize("bad", ["tickets/ABC-1.md", ".orch/AGENTS.orch.md"])
def test_no_ticket_or_store_paths(bad):
    refused(GOOD + f"Edit {bad} by hand.\n", "ticket files or the store folder")


@pytest.mark.parametrize("other", ["orch-addon-changelog", "orch-tickets", "orch-work-on-ticket", "orch-refine-ticket"])
def test_no_other_skill_names(other):
    refused(
        GOOD.replace("(use orch-addon-changelog)", "").replace("Do not use when:", f"See {other}. Do not use when:"),
        "another skill|built-in skill",
    )


def test_its_own_name_may_be_written_in_the_body():
    check(GOOD + "This is orch-addon-notes.\n")


# ---- the manifest key


def test_the_key_needs_schema_3_and_an_api_range_that_excludes_1_1():
    with pytest.raises(ManifestError, match="API 1.2"):
        load_manifest(files(api=">=1.1,<2")["orch-addon.json"])
    doc = json.loads(files()["orch-addon.json"])
    doc.update(schema="orch.addon/2")
    del doc["api"], doc["min_core"]
    with pytest.raises(ManifestError):
        load_manifest(json.dumps(doc).encode())
    assert load_manifest(files()["orch-addon.json"]).skill == "skills/main"


@pytest.mark.parametrize("path", ["/abs", "../up", "skills//main", "skills/", "a b", "x" * 65])
def test_the_key_is_a_plain_package_path(path):
    with pytest.raises(ManifestError):
        load_manifest(files(skill=path)["orch-addon.json"])


def test_the_reserved_skills_array_stays_reserved():
    with pytest.raises(ManifestError):
        load_manifest(files(skills=["skills/main"])["orch-addon.json"])


# ---- where a package is read


def test_a_package_read_refuses_a_bad_folder_but_does_not_lint():
    f = files(GOOD.replace("`orch show`", "`orch frobnicate`"))
    assert (
        load_package_manifest(from_files(f), "notes").skill == "skills/main"
    )  # core renamed a command: still readable
    f["skills/main/extra.md"] = b"x"
    with pytest.raises(ManifestError, match="skill: .*exactly SKILL.md"):
        load_package_manifest(from_files(f), "notes")


def test_an_install_check_refuses_with_package_invalid():
    def entry(f):
        pkg = from_files(f)
        return mi.Facts({"package_sha256": pkg.digest, "id": "notes", "version": "1.0.0", "capabilities": [],
                         "min_orch": "2.0.0"}, "unsigned", None, None), pkg  # fmt: skip

    facts, pkg = entry(files())
    assert mi.check_package(facts, pkg, "2.0.0").skill == "skills/main"
    facts, pkg = entry(files(GOOD.replace("`orch show`", "`orch frobnicate`")))
    with pytest.raises(MarketError) as e:
        mi.check_package(facts, pkg, "2.0.0")
    assert e.value.code == "market.package_invalid"


# ---- the pack warnings


def test_warnings_for_the_conventions():
    text = GOOD.replace("Use when the person asks", "When the person asks").replace(
        " Not for the changelog (use orch-addon-changelog)", ""
    )
    text = text.replace("## Steps", "## Plan").replace("Do not use when:", "Avoid:")
    f = files(text, agents_md="Notes for tickets.")
    got = skill.warnings(load_manifest(f["orch-addon.json"]), f)
    assert 'agents_md should read "Use when ... Not for ..."' in got[0]
    assert any('start with "Use when"' in w for w in got) and any('"Not for' in w for w in got)
    assert any('"Steps" section' in w for w in got) and any('"Do not use when" section' in w for w in got)
    assert not any('"Use when" section' in w or '"If orch refuses"' in w for w in got)


# ---- lint hardening (review of #1327)


@pytest.mark.parametrize(
    "bad",
    [
        "run `cd x && orch bogus`\n",
        "run `ORCH_X=1 orch bogus`\n",
        'run `bash -c "orch bogus"`\n',
        "```\ncd x && orch bogus\n```\n",
        "~~~\norch bogus\n~~~\n",
        "Example:\n\n    orch bogus\n",
        "Then orch task frobnicate runs.\n",
        '`orch set "other.note=v"`\n',
    ],
)
def test_the_lint_finds_a_bad_command_wherever_it_hides(bad):
    refused(GOOD + bad, "not an orch command|own fields")


def test_a_tilde_fence_and_prose_with_the_word_orch_are_fine():
    check(GOOD + "~~~\norch show\n~~~\nIf orch refuses, stop. The orch tool is strict.\n`orch`\n")


@pytest.mark.parametrize("ch", ["\u200b", "\u202e", "\u2060", "\ufeff"])
def test_invisible_characters_anywhere_in_the_text_are_refused(ch):
    refused(GOOD + f"run `orch{ch} bogus`\n", "not printable")


@pytest.mark.parametrize("bad", ["Tickets/ABC-1", "ORCH-ADDON-OTHER", "See .ORCH/x", "ORCH-TICKETS"])
def test_forbidden_names_match_without_regard_to_case(bad):
    refused(GOOD + bad + "\n", "ticket files|another skill|built-in skill")


def test_an_addon_whose_name_ends_in_a_dash_may_name_its_own_skill():
    text = (
        GOOD.replace("orch-addon-notes", "orch-addon-foo-").replace("notes.note", "foo-.note")
        + "See orch-addon-foo-.\n"
    )
    skill.check_skill("foo-", text.encode(), {"note": {}})
