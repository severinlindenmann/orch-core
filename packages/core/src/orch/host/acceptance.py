"""Canonical manual acceptance statements. Pure; shared by preparers, signers and readers."""

from __future__ import annotations

import re
from typing import Any

from orch.canon.text import check_text, normalize_text
from orch.model.accept import criterion_hash

_AC = re.compile(r"AC[1-9][0-9]*")
NAME = re.compile(r"accept-(AC[1-9][0-9]*)-([1-9][0-9]*)\.md")


def artifact_name(ac_id: str, k: int) -> str:
    if not _AC.fullmatch(ac_id) or type(k) is not int or k < 1:
        raise ValueError("invalid acceptance artifact name")
    name = f"accept-{ac_id}-{k}.md"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name):
        raise ValueError("acceptance artifact name is too long")
    return name


def _block(value: str) -> str:
    return "".join("| " + line + "\n" for line in value.split("\n"))


def _build(ac_id: str, text: str, value: str | None, *, withdraw: bool) -> str:
    if not _AC.fullmatch(ac_id):
        raise ValueError("invalid criterion id")
    check_text(text)
    if value is not None:
        value = normalize_text(value)
        if not 1 <= len(value) <= 1000 or "\t" in value:
            raise ValueError("note or reason must have 1..1000 characters and no controls")
    elif withdraw:
        raise ValueError("withdrawal requires a reason")
    mark = " withdrawn" if withdraw else ""
    result = f"orch manual acceptance{mark} v1\ncriterion: {ac_id}\nhash: {criterion_hash(ac_id, text)}\ntext:\n"
    result += _block(text)
    if value is not None:
        result += ("reason:\n" if withdraw else "note:\n") + _block(value)
    return result


def build_accept(ac_id: str, text: str, note: str | None = None) -> str:
    return _build(ac_id, text, note, withdraw=False)


def build_withdraw(ac_id: str, text: str, reason: str) -> str:
    return _build(ac_id, text, reason, withdraw=True)


def parse(statement: str) -> dict[str, str | None] | None:
    if not isinstance(statement, str):
        return None
    lines = statement.split("\n")
    if len(lines) < 6 or lines[-1] != "":
        return None
    withdraw = lines[0] == "orch manual acceptance withdrawn v1"
    if not withdraw and lines[0] != "orch manual acceptance v1":
        return None
    if not lines[1].startswith("criterion: ") or not lines[2].startswith("hash: ") or lines[3] != "text:":
        return None
    ac, digest = lines[1][11:], lines[2][6:]
    blocks: list[list[str]] = [[]]
    marker = "reason:" if withdraw else "note:"
    for line in lines[4:-1]:
        if line == marker and len(blocks) == 1:
            blocks.append([])
        elif line.startswith("| "):
            blocks[-1].append(line[2:])
        else:
            return None
    if not all(blocks):
        return None
    text = "\n".join(blocks[0])
    value = "\n".join(blocks[1]) if len(blocks) == 2 else None
    try:
        rebuilt = _build(ac, text, value, withdraw=withdraw)
    except (ValueError, UnicodeError):
        return None
    if rebuilt != statement:
        return None
    return {
        "kind": "withdraw" if withdraw else "accept",
        "ac": ac,
        "hash": digest,
        "text": text,
        "reason" if withdraw else "note": value,
    }


def matches(event: dict, statement: str) -> bool:
    """Exact acceptance payload, including the preimage of its file digest; no envelope judgments here."""
    from orch.canon import artifact_digest
    from orch.host.review import ENVELOPE
    from orch.model.accept import label, withdraw_label

    parsed = parse(statement)
    if parsed is None:
        return False
    withdraw = parsed["kind"] == "withdraw"
    keys = {"name", "kind", "label", "sha256", "bytes", "replaces" if withdraw else "ac"}
    if set(event) - ENVELOPE != keys:
        return False
    name = NAME.fullmatch(event["name"]) if isinstance(event.get("name"), str) else None
    if name is None or name[1] != parsed["ac"] or len(event["name"]) > 128:
        return False
    expected_label = withdraw_label(parsed["ac"]) if withdraw else label(parsed["ac"], parsed["text"])
    return (
        event.get("type") == ("artifact.replaced" if withdraw else "artifact.added")
        and event.get("kind") == "feedback"
        and event.get("label") == expected_label
        and (withdraw or event.get("ac") == parsed["ac"])
        and (
            not withdraw
            or isinstance(event.get("replaces"), str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", event["replaces"]) is not None
        )
        and event.get("sha256") == artifact_digest(statement.encode("utf-8"))
        and type(event.get("bytes")) is int
        and event["bytes"] == len(statement.encode("utf-8"))
    )


def artifact_statement(art: Any, data: bytes) -> dict | None:
    """A verified file and its signed manifest must both say person acceptance."""
    from orch.canon import artifact_digest
    from orch.model.accept import parse_label

    if (
        art.kind != "feedback"
        or re.fullmatch(r"p_[0-9a-f]{32}", art.by) is None
        or not art.evidence_ok
        or artifact_digest(data) != art.digest
        or len(data) != art.bytes
    ):
        return None
    try:
        parsed = parse(data.decode("utf-8"))
    except UnicodeError:
        return None
    if (
        parsed is None
        or parsed["kind"] != "accept"
        or art.ac != parsed["ac"]
        or parse_label(art.label) != (parsed["ac"], parsed["hash"])
    ):
        return None
    return parsed
