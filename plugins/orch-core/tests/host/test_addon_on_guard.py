"""V3 G7 structural guard (second finding of one class, review of #1213): every host feature that runs an add-on asks
``hubpolicy.running`` (or the runner gate); none reads the grant's ``enabled`` flag by itself. A new host file that
decides "is add-on X on" from the grant alone fails here, so the hub policy cannot be bypassed by a new feature."""

from __future__ import annotations

import re
from pathlib import Path

HOST = Path(__file__).parents[2] / "src/orch/host"
#: a read of an add-on's enabled flag: ``model.enabled``, ``found.enabled``, ``addon["enabled"]`` ... (not the event type name "addon.enabled", which a quote precedes)
READ = re.compile(r"(?<![\"'])\b(?:model|found|granted|addon|a|g)\.enabled\b|\baddons?\[[\"']enabled[\"']\]")
#: files that may read it, each with the reason (they do not run the add-on, or they ask the gate right there)
ALLOWED = {
    "hubpolicy.py": "the one place that combines the grant with the policy",
    "addonenv.py": "state of a dependency environment (build / listing); runs no add-on code (hardening: F7)",
    "marketinstall.py": "install records, uninstall and listings: state, not execution",
    "routing.py": "picks the choose_model add-on, then asks run_gate before the memo and in runner.call(gate=...)",
}


def test_no_host_file_decides_that_an_addon_is_on_from_the_grant_alone():
    offenders = {}
    for path in sorted(HOST.glob("*.py")):
        if path.name in ALLOWED:
            continue
        hits = [n for n, line in enumerate(path.read_text().splitlines(), 1) if READ.search(line)]
        if hits:
            offenders[path.name] = hits
    assert not offenders, (
        f"these host files read an add-on's enabled flag directly; use hubpolicy.running(service, name): {offenders}"
    )


def test_the_allowed_files_exist_and_every_runner_call_in_the_host_passes_the_gate():
    for name in ALLOWED:
        assert (HOST / name).is_file(), name
    for path in sorted(HOST.glob("*.py")):
        text = path.read_text()
        for m in re.finditer(r"runner\.(?:call|propose)\(", text):
            tail = text[m.end() : m.end() + 600]
            assert "gate=" in tail.split("\n\n")[0], f"{path.name}: runner call without gate="
