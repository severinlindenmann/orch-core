"""Independent oracle, part 2: everything that turns a small real ticket into the F1 gate hash inputs (ticket-format
§4, §5.6, §5.7) and the pure text/identity rules that feed it. ``hashlib``, ``json`` and ``re`` only; nothing here
imports ``orch`` (``tests/canon/test_f1_independence.py`` checks the imports).

Everything is derived from a *ticket state* dict (see :func:`sample_ticket`) by the rules of the spec, never copied
from ``orch``: the effective policy (§5.7), the people hash roles (§5.6), the sections of each gate (§4, §5.7), the
artifact references (§5.8), the source list and the repo identity (§5.7), ``prior`` (§5.7).
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

W = "0123456789abcdef0123456789abcdef"
UID = "01J9ZK4Q7M3R8T2V6X0B5N1C9D"
P_SEV = "p_" + "5e" * 16
P_MARA = "p_" + "3a" * 16
P_LENA = "p_" + "7c" * 16
GATES = ("requirements", "plan", "verify", "code")
TICKET_ROLES = ("assignees", "reviewers", "watchers")

_LABEL = {
    "gate": "orch/v2/gate|",
    "section": "orch/v2/section|",
    "policy": "orch/v2/policy|",
    "people": "orch/v2/people|",
}


def cj(o: Any) -> bytes:
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def hl(label: str, data: bytes) -> str:
    return "sha256:" + hashlib.sha256(_LABEL[label].encode() + data).hexdigest()


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def section_h(text: str) -> str:
    return hl("section", text.encode("utf-8"))


# --- policies (§5.7) ------------------------------------------------------------------------------

DEFAULT_POLICIES: dict[str, dict[str, Any]] = {  # the §2 config.json example
    "requirements": {"approvers": ["owner"], "count": 1, "not": [], "applies": "all", "independent": False},
    "plan": {"approvers": ["owner"], "count": 1, "not": [], "applies": "all", "independent": False},
    "verify": {"approvers": ["reviewers"], "count": 1, "not": ["assignees"], "applies": "all", "independent": False},
    "code": {
        "approvers": ["maintainer", "owner"],
        "count": 1,
        "not": ["assignees"],
        "applies": "off",
        "independent": True,
    },
}


def canonical_policy(p: dict[str, Any]) -> dict[str, Any]:
    applies = p["applies"]
    return {
        "approvers": sorted(set(p["approvers"])),
        "count": p["count"],
        "not": sorted(set(p["not"])),
        "applies": applies if applies in ("all", "off") else sorted(set(applies)),
        "independent": p["independent"],
    }


def _applies_union(a: Any, b: Any) -> Any:
    if a == "all" or b == "all":
        return "all"
    if a == "off" and b == "off":
        return "off"
    return sorted({t for x in (a, b) if isinstance(x, list) for t in x})


def effective_policy(ws: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    """§5.7: approvers = workspace intersect override, count = the larger, not = the union, independent = either, applies =
    the union (``all`` if either is, ``off`` only if both are, else the sorted de-duplicated list)."""
    if override is None:
        return canonical_policy(ws)
    return {
        "approvers": sorted(set(ws["approvers"]) & set(override["approvers"])),
        "count": max(ws["count"], override["count"]),
        "not": sorted(set(ws["not"]) | set(override["not"])),
        "applies": _applies_union(ws["applies"], override["applies"]),
        "independent": bool(ws["independent"] or override["independent"]),
    }


def policy_hash(gate: str, policy: dict[str, Any]) -> str:
    return hl("policy", cj({"gate": gate, "policy": canonical_policy(policy)}))


def applies_to(policy: dict[str, Any], ticket_type: str) -> bool:
    a = policy["applies"]
    return a == "all" or (a != "off" and ticket_type in a)


def named_ticket_roles(policy: dict[str, Any]) -> list[str]:
    """The ticket roles the people hash covers: those named in ``approvers`` or ``not``, plus ``assignees`` when
    ``independent`` is on (§5.6). Workspace roles (owner, maintainer, member) are not ticket roles."""
    named = {t for t in (*policy["approvers"], *policy["not"]) if t in ("ticket_owner", *TICKET_ROLES)}
    if policy["independent"]:
        named.add("assignees")
    return sorted(named)


def people_for(policy: dict[str, Any], t: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for role in named_ticket_roles(policy):
        out[role] = t["owner"] if role == "ticket_owner" else sorted(set(t["people"][role]))
    return out


def people_hash(people: dict[str, Any]) -> str:
    return hl("people", cj(people))


# --- text rules: section text (§4) ----------------------------------------------------------------

HEADINGS = {
    "summary": "Summary",
    "context": "Context",
    "requirements": "Requirements",
    "out_of_scope": "Out of scope",
    "plan": "Plan",
    "decisions": "Decisions",
    "verification": "Verification",
    "findings": "Findings",
    "current_state": "Current state",
}
_ALL = {"summary", "context", "requirements", "decisions", "current_state"}  # every type (§4 table)
SECTIONS_OF_TYPE = {
    "feature": _ALL | {"out_of_scope", "plan", "verification"},
    "bug": _ALL | {"out_of_scope", "plan", "verification"},
    "chore": _ALL | {"plan"},
    "spike": _ALL | {"plan", "findings"},
    "epic": _ALL | {"out_of_scope"},
}
_FENCE = re.compile(r"^(`{3,}|~{3,})")


class BodyRefused(ValueError):
    """The §4 refusal reason of a ``body.md`` (kept as a short stable token in ``args[0]``)."""


def parse_body(text: str, ticket_type: str) -> dict[str, str]:
    """§4: a section starts with ``## `` at column 0 outside a code fence; a fence opens at column 0 with three or more
    backticks or tildes and closes at column 0 with the same character, at least as long, followed by spaces only. The
    text of a section is what lies between its heading and the next one, leading and trailing LF removed."""
    by_heading = {h: s for s, h in HEADINGS.items()}
    out: dict[str, str] = {}
    cur: str | None = None
    buf: list[str] = []
    fence: str | None = None

    def close() -> None:
        if cur is not None:
            out[cur] = "\n".join(buf).strip("\n")

    for line in text.split("\n"):
        m = _FENCE.match(line)
        if fence is None:
            if line.startswith("## "):
                close()
                sid = by_heading.get(line[3:])
                if sid is None:
                    raise BodyRefused("body.unknown_section")
                if sid not in SECTIONS_OF_TYPE[ticket_type]:
                    raise BodyRefused("body.unknown_section")
                if sid in out:
                    raise BodyRefused("body.duplicate_section")
                cur, buf = sid, []
                continue
            if m:
                fence = m.group(1)
        elif (
            m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not line[len(m.group(1)) :].strip(" ")
        ):
            fence = None
        if cur is None:
            if line:
                raise BodyRefused("body.text_before_heading")
            continue
        buf.append(line)
    if fence is not None:
        raise BodyRefused("body.open_fence")
    close()
    return out


_REF = re.compile(r"\(artifact:([A-Za-z0-9][A-Za-z0-9._-]{0,127})\)")


def refs_of(text: str) -> list[str]:
    """§5.8: every match of the regex over the raw text (no Markdown parsing, fences included), sorted, unique."""
    return sorted(set(_REF.findall(text)))


# --- repo identity (§5.7) -------------------------------------------------------------------------


def repo_identity(raw: str, name: str) -> str:
    """From the raw ``remote.origin.url``: https and ssh/scp-like forms become ``https://host[:port]/path`` (userinfo
    removed, host lower-case, port kept except 443 for https and 22 for ssh, one trailing ``.git`` and ``/`` removed);
    anything else is ``local:<name>``."""
    m = re.fullmatch(r"(https|ssh)://(?:[^/@]*@)?([^/:@]+)(?::([0-9]+))?/(.+)", raw)
    if m:
        scheme, host, port, path = m.groups()
        keep = port is not None and port != ("443" if scheme == "https" else "22")
    else:
        s = re.fullmatch(r"(?:[^/@:]+@)?([^/:@]+):([^/].*)", raw)  # scp-like: [user@]host:path, no slash before ':'
        if not s or "://" in raw:
            return f"local:{name}"
        host, path = s.groups()
        port, keep = None, False
    path = path.removesuffix("/")
    path = path.removesuffix(".git")
    return f"https://{host.lower()}{':' + port if keep else ''}/{path}"


# --- the ticket and G (§5.7) ----------------------------------------------------------------------

REQ_SECTIONS = ("summary", "context", "requirements", "out_of_scope")


def gate_sections(gate: str, ticket_type: str) -> list[str]:
    """The core sections of a gate that this ticket's type has (§4, §5.7)."""
    if gate == "requirements":
        want: tuple[str, ...] = REQ_SECTIONS
    elif gate == "plan":
        want = ("plan", "decisions")
    elif gate == "verify":
        want = ("verification", "findings")
    else:
        want = ()
    return [s for s in want if s in SECTIONS_OF_TYPE[ticket_type]]


def _addon_sections(t: dict[str, Any], gate: str) -> list[str]:
    out = []
    for a in t["addons"].values():
        out += [s["id"] for s in a["binds"]["sections"] if gate in s["gate"] and t["type"] in s["types"]]
    return sorted(out)


def _addon_fields(t: dict[str, Any], gate: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name, a in sorted(t["addons"].items()):
        for field, gates in sorted(a["binds"]["fields"].items()):
            if gate in gates:
                out.setdefault(name, {})[field] = a["values"].get(field)
    return out


def source_list(t: dict[str, Any]) -> list[dict[str, str]]:
    """§5.7: one entry per linked repo with an observed head, sorted by repo identity."""
    out = []
    for name in t["links"]["repos"]:
        head = t["heads"].get(name)
        if head is not None:
            out.append(
                {
                    "repo": repo_identity(t["remotes"][name], name),
                    "ref": "refs/heads/" + t["links"]["branches"][name],
                    "sha": head,
                }
            )
    return sorted(out, key=lambda x: x["repo"])


def derive_policies(t: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {g: effective_policy(t["ws_policies"][g], t["overrides"].get(g)) for g in GATES}


def derive_G(t: dict[str, Any], gate: str) -> dict[str, Any]:
    """The 15-key gate hash input of §5.7 for ``gate``, with every inner hash derived from the ticket state."""
    pols = derive_policies(t)
    pol = pols[gate]
    sec_ids = gate_sections(gate, t["type"]) + _addon_sections(t, gate)
    sections = {s: section_h(t["sections"].get(s, "")) for s in sec_ids}
    if gate in ("requirements", "plan"):
        names = sorted({n for s in sec_ids for n in refs_of(t["sections"].get(s, ""))})
        names = [n for n in names if n in t["artifacts"]]
    elif gate == "verify":
        names = sorted(t["artifacts"])
    else:
        names = []
    artifacts = {
        n: {
            "kind": t["artifacts"][n]["kind"],
            "digest": digest(bytes.fromhex(t["artifacts"][n]["bytes_hex"])),
            "ac": t["artifacts"][n]["ac"],
            "task": t["artifacts"][n]["task"],
        }
        for n in names
    }
    packages = {}
    for name, a in sorted(t["addons"].items()):
        if any(gate in g for g in a["binds"]["fields"].values()) or any(
            gate in s["gate"] and t["type"] in s["types"] for s in a["binds"]["sections"]
        ):
            packages[name] = a["package_sha256"]
    prior = {}
    for e in GATES[: GATES.index(gate)]:
        if applies_to(pols[e], t["type"]):
            counting = t["prior"][e]["counting"]  # approval ids in seq order, one per person
            prior[e] = {"gen": t["prior"][e]["gen"], "approvals": sorted(counting[: pols[e]["count"]])}
    return {
        "workspace_id": t["workspace_id"],
        "uid": t["uid"],
        "gate": gate,
        "schema": "orch.ticket/2",
        "hash_v": 1,
        "sections": sections,
        "fields": {
            "ticket_type": t["type"],
            "size": t["size"],
            "acceptance": copy.deepcopy(t["acceptance"]),
            "links": copy.deepcopy(t["links"]) if gate in ("verify", "code") else None,
            "addons": _addon_fields(t, gate),
        },
        "addon_packages": packages,
        "tasks": copy.deepcopy(t["tasks"]) if gate == "plan" else [],
        "artifacts": artifacts,
        "receipts": copy.deepcopy(t["receipts"]) if gate == "verify" else {},
        "source_sha": source_list(t) if gate in ("verify", "code") else [],
        "prior": prior,
        "policy_hash": policy_hash(gate, pol),
        "people_hash": people_hash(people_for(pol, t)),
    }


def gate_hash_of(g: dict[str, Any]) -> str:
    return hl("gate", cj(g))


def sample_ticket() -> dict[str, Any]:
    """A small real ticket: feature DEMO-0043 owned by sev, assigned to mara, with one linked repo, an estimate addon
    bound to ``plan``, a plan override that tightens count and independence, and the code gate switched on."""
    sha = "b7e1f02c" * 5
    ws_pol = copy.deepcopy(DEFAULT_POLICIES)
    ws_pol["code"]["applies"] = "all"
    return {
        "workspace_id": W,
        "uid": UID,
        "type": "feature",
        "size": "m",
        "owner": P_SEV,
        "people": {"assignees": [P_MARA], "reviewers": [P_SEV], "watchers": []},
        "sections": {
            "summary": "Load tariffs.",
            "context": "",
            "requirements": "- R1 caf\u00e9\n![mock](artifact:mock.png)",
            "out_of_scope": "Nothing.",
            "plan": "1. export\n2. seed",
            "decisions": "",
            "verification": "Ran dbt seed.",
            "estimate.notes": "5 points",
        },
        "acceptance": [
            {"id": "AC1", "text": "`dbt seed` loads all 40 tariff tables"},
            {"id": "AC2", "text": "Model joins the seeds"},
        ],
        "tasks": [
            {"id": "T1", "text": "Export CSVs", "verify": {"cmd": "ls seeds | wc -l"}, "proves": []},
            {"id": "T2", "text": "Seed configs", "verify": None, "proves": ["AC1"]},
        ],
        "links": {
            "repos": ["acme-energy-dbt"],
            "branches": {"acme-energy-dbt": "feat/DEMO-0043"},
            "prs": [],
            "external": [],
        },
        "remotes": {"acme-energy-dbt": "git@github.com:acme/energy-dbt.git"},
        "heads": {"acme-energy-dbt": sha},
        "artifacts": {
            "mock.png": {"kind": "screenshot", "bytes_hex": b"png bytes".hex(), "ac": "AC1", "task": None},
            "run.log": {"kind": "log", "bytes_hex": b"log".hex(), "ac": None, "task": "T2"},
        },
        "receipts": {
            "T2": {"event": "01J9ZQ0000000000000000000C", "repo": "acme-energy-dbt", "commit": sha, "exit": 0}
        },
        "addons": {
            "estimate": {
                "package_sha256": digest(b"estimate package"),
                "binds": {
                    "fields": {"points": ["plan"]},
                    "sections": [{"id": "estimate.notes", "gate": ["plan"], "types": ["feature", "bug"]}],
                },
                "values": {"points": 5},
            }
        },
        "ws_policies": ws_pol,
        "overrides": {
            "plan": {"approvers": ["owner"], "count": 2, "not": ["reviewers"], "applies": "all", "independent": True}
        },
        "prior": {
            "requirements": {"gen": 2, "counting": ["01J9ZP0000000000000000000A"]},
            "plan": {"gen": 3, "counting": ["01J9ZP0000000000000000000D", "01J9ZP0000000000000000000B"]},
            "verify": {"gen": 1, "counting": ["01J9ZR0000000000000000000E"]},
        },
    }
