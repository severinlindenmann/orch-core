from __future__ import annotations

import hashlib

import yaml

from orch.core.canonical import canonical_json
from orch.core.model import yaml_load
from orch.errors import NotFoundError, ValidationError

QTYPES = ("single", "multi", "confirm", "text")
CONFIRM_OPTIONS = ({"key": "yes", "label": "Yes", "cost": None}, {"key": "no", "label": "No", "cost": None})
HASH_FIELDS = ("id", "text", "why", "type", "options", "recommended", "blocking")


def question_hash(q: dict) -> str:
    """What a remote answer is bound to. Answer fields (answer, note, answered, via, asked) are left out."""
    core = {k: q.get(k) for k in HASH_FIELDS}
    core["options"] = [{"key": o.get("key"), "label": o.get("label"), "cost": o.get("cost")}
                       for o in (q.get("options") or []) if isinstance(o, dict)]
    core["blocking"] = bool(q.get("blocking", True))
    return "sha256:" + hashlib.sha256(canonical_json(core)).hexdigest()


def parse_ask_file(text: str) -> list:
    try:
        data = yaml_load(text.lstrip("﻿"))
    except yaml.YAMLError as e:
        raise ValidationError(f"invalid question file: {e}") from e
    if isinstance(data, dict):
        data = data.get("questions")
    if not isinstance(data, list) or not data:
        raise ValidationError("question file must contain a non-empty 'questions' list")
    return data


def _next_number(existing: list[dict]) -> int:
    nums = [int(str(q.get("id"))[1:]) for q in existing if str(q.get("id", "")).startswith("Q") and str(q.get("id"))[1:].isdigit()]
    return max(nums, default=0) + 1


def _match_key(options: list[dict], value) -> str | None:
    if isinstance(value, bool):  # YAML turns bare yes/no into booleans
        value = "yes" if value else "no"
    wanted = str(value).strip().upper()
    for o in options:
        if o["key"].upper() == wanted:
            return o["key"]
    return None


def _options(i: int, qtype: str, raw) -> list[dict]:
    if qtype == "confirm":
        return [dict(o) for o in CONFIRM_OPTIONS]
    if qtype == "text":
        return []
    if not isinstance(raw, list) or len(raw) < 2:
        raise ValidationError(f"question {i}: {qtype} questions need at least 2 options")
    opts, seen = [], set()
    for j, o in enumerate(raw):
        if isinstance(o, bool):  # YAML turns a bare yes/no option label into a boolean
            o = {"label": "Yes" if o else "No"}
        elif isinstance(o, str):
            o = {"label": o}
        if not isinstance(o, dict) or not o.get("label"):
            raise ValidationError(f"question {i}: option {j + 1} needs a label")
        key = str(o.get("key") or chr(ord("A") + j)).strip()
        if key.upper() in seen:
            raise ValidationError(f"question {i}: duplicate option key {key}")
        seen.add(key.upper())
        opts.append({"key": key, "label": str(o["label"]), "cost": str(o["cost"]) if o.get("cost") else None})
    return opts


def _recommended(i: int, qtype: str, options: list[dict], rec):
    if rec is None or rec == "":
        return None
    if qtype == "text":
        return str(rec)
    values = rec if isinstance(rec, list) else (str(rec).split(",") if qtype == "multi" and isinstance(rec, str) else [rec])
    keys = []
    for v in values:
        key = _match_key(options, v)
        if key is None:
            raise ValidationError(f"question {i}: recommended {v!r} is not an option key")
        keys.append(key)
    return keys if qtype == "multi" else keys[0]


def build_questions(raw: list, existing: list[dict], asked_at: str) -> list[dict]:
    n = _next_number(existing)
    out = []
    for i, r in enumerate(raw, 1):
        if not isinstance(r, dict):
            raise ValidationError(f"question {i}: must be a mapping")
        text = str(r.get("text") or "").strip()
        if not text:
            raise ValidationError(f"question {i}: 'text' is required")
        qtype = r.get("type", "single")
        if qtype not in QTYPES:
            raise ValidationError(f"question {i}: type must be one of {', '.join(QTYPES)}")
        options = _options(i, qtype, r.get("options"))
        out.append({
            "id": f"Q{n}",
            "text": text,
            "why": str(r["why"]).strip() if r.get("why") else None,
            "type": qtype,
            "options": options,
            "recommended": _recommended(i, qtype, options, r.get("recommended")),
            "blocking": bool(r.get("blocking", True)),
            "asked": asked_at,
            "answer": None,
            "note": None,
            "answered": None,
            "via": None,
        })
        n += 1
    return out


def validate_answer(q: dict, value: str) -> str | list[str]:
    value = str(value).strip()
    if not value:
        raise ValidationError("answer must not be empty")
    qtype = q.get("type", "single")
    if qtype == "text":
        return value
    if qtype == "confirm":
        v = value.lower()
        if v in ("y", "yes", "true", "ja"):
            return "yes"
        if v in ("n", "no", "false", "nein"):
            return "no"
        raise ValidationError(f"{q['id']} expects yes or no")
    options = q.get("options") or []
    hint = "options: " + ", ".join(o["key"] for o in options)
    if qtype == "multi":
        out = []
        for part in (p.strip() for p in value.split(",")):
            if not part:
                continue
            key = _match_key(options, part)
            if key is None:
                raise ValidationError(f"{q['id']}: {part!r} is not an option", hint=hint)
            if key not in out:
                out.append(key)
        if not out:
            raise ValidationError("answer must not be empty")
        return out
    key = _match_key(options, value)
    if key is None:
        raise ValidationError(f"{q['id']}: {value!r} is not an option", hint=hint)
    return key


def find_question(ticket, qid: str) -> dict:
    for q in ticket.meta.get("questions") or []:
        if str(q.get("id", "")).upper() == qid.strip().upper():
            return q
    raise NotFoundError(f"{ticket.id} has no question {qid}")
