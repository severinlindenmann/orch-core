from __future__ import annotations

import hashlib

import yaml

from orch.core.canonical import canonical_json
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


_STR_TAG = "tag:yaml.org,2002:str"
_PLAIN_TAGS = ("tag:yaml.org,2002:bool", "tag:yaml.org,2002:int", "tag:yaml.org,2002:float")


def _as_text(node) -> None:
    """Keep a bare option label or key as written: `No`, `Yes`, `On`, `Off` and `1.0` are not YAML booleans or numbers here."""
    if isinstance(node, yaml.ScalarNode) and node.tag in _PLAIN_TAGS:
        node.tag = _STR_TAG


MAX_ASK_NODES = 20000  # distinct YAML nodes in a question file: far above any real one, a bound on hostile input


def _keep_option_text(root) -> None:
    """Walk the node graph once (aliases share nodes, so a node is visited once whatever points at it: an alias bomb
    or a recursive alias cannot make this exponential or endless) and keep option labels and keys as written."""
    seen: set[int] = set()
    todo = [root]
    while todo:
        node = todo.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if len(seen) > MAX_ASK_NODES:
            raise ValidationError(f"invalid question file: more than {MAX_ASK_NODES} YAML nodes")
        if isinstance(node, yaml.SequenceNode):
            todo.extend(node.value)
        elif isinstance(node, yaml.MappingNode):
            for k, v in node.value:
                if isinstance(k, yaml.ScalarNode) and k.value == "options" and isinstance(v, yaml.SequenceNode):
                    seen.add(id(v))
                    for opt in v.value:
                        if isinstance(opt, yaml.MappingNode):
                            seen.add(id(opt))
                            for ok, ov in opt.value:
                                if isinstance(ok, yaml.ScalarNode) and ok.value in ("label", "key"):
                                    _as_text(ov)
                                else:
                                    todo.append(ov)
                        elif isinstance(opt, yaml.ScalarNode):
                            _as_text(opt)
                        else:
                            todo.append(opt)
                else:
                    todo.append(v)


MAX_ASK_BYTES = 256 * 1024
MAX_ASK_DEPTH = 64  # a question file is questions > options > fields: a handful of levels


def _refuse_aliases(text: str) -> None:
    """Question files never need anchors or aliases, and an alias bomb that survives parsing explodes when the data is
    serialised later: refuse them on the event stream (linear in the text) before anything is composed."""
    try:
        depth = 0
        for ev in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(ev, yaml.AliasEvent) or getattr(ev, "anchor", None) is not None:
                raise ValidationError("invalid question file: anchors and aliases are not allowed in question files")
            if isinstance(ev, (yaml.SequenceStartEvent, yaml.MappingStartEvent)):
                depth += 1
                if depth > MAX_ASK_DEPTH:  # the composer is recursive (libyaml's C one overflows the stack and crashes)
                    raise ValidationError(f"invalid question file: nested more than {MAX_ASK_DEPTH} levels deep")
            elif isinstance(ev, (yaml.SequenceEndEvent, yaml.MappingEndEvent)):
                depth -= 1
    except yaml.YAMLError:
        pass  # a syntax error is reported by the real load below


def _load_ask_yaml(text: str):
    try:
        return _load_ask_yaml_unbounded(text)
    except RecursionError:  # a few KB of `[[[[…` or `{a: {a: …` overflows the recursive composer: a refusal, not a crash
        raise ValidationError("invalid question file: nested too deeply") from None


def _load_ask_yaml_unbounded(text: str):
    from orch.core import model  # the same loader as every other orch YAML file (C loader when available)
    if len(text.encode("utf-8", "replace")) > MAX_ASK_BYTES:
        raise ValidationError(f"invalid question file: larger than {MAX_ASK_BYTES // 1024} KB")
    _refuse_aliases(text)
    for cls in (model._Loader, model._PyLoader):
        loader = cls(text)
        try:
            node = loader.get_single_node()
            if node is None:
                return None
            _keep_option_text(node)
            return loader.construct_document(node)
        except yaml.YAMLError:
            if cls is model._PyLoader:
                raise
        finally:
            loader.dispose()


def parse_ask_file(text: str) -> list:
    try:
        data = _load_ask_yaml(text.lstrip("﻿"))
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
        if isinstance(o, dict) and isinstance(o.get("label"), bool):  # JSON true/false; YAML files keep the source text
            raise ValidationError(f"question {i}: option {j + 1}: label must be text, not true/false; "
                                  f"quote it, e.g. label: \"No\"")
        if isinstance(o, dict) and isinstance(o.get("label"), (int, float)):
            o = {**o, "label": str(o["label"])}
        if not isinstance(o, dict) or not o.get("label"):
            raise ValidationError(f"question {i}: option {j + 1} needs a label")
        if not isinstance(o["label"], str):
            raise ValidationError(f"question {i}: option {j + 1}: label must be text; quote it, e.g. label: \"No\"")
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
