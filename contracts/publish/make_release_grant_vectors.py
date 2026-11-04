"""Build pending RG amendment vectors, independently of runtime verification.

Run: python contracts/publish/make_release_grant_vectors.py
meta_hash is the existing M4 (§11.4) domain-separated hash.
These ASCII/integer fixtures have identical sorted compact JSON and JCS bytes.
"""

import base64
import copy
import hashlib
import json
from pathlib import Path


def cj(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(data):
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def build():
    ws, hub, app = "11" * 16, "22" * 16, "33" * 16
    data = b"<h1>Team report</h1>"
    meta = {
        "app_id": app, "new": False, "name": "Team report", "kind": "page", "access": "group",
        "audience": "group:" + "44" * 16, "slug": "team-report",
        "reach": ["https://api.example.com"],
        "files": [{"path": "index.html", "size": len(data),
                   "sha256": hashlib.sha256(data).hexdigest()}],
        "pw": None, "label": "First release", "ticket": "01J9ZP00000000000000000001",
    }
    meta_hash = digest(b"orch/v2/app-meta|" + cj(meta))  # §11.4's meta_hash, unchanged
    release = {
        "v": 1, "op": "commit", "staged_id": "pa_" + "55" * 16,
        "workspace_id": ws, "hub_id": hub, "app_id": app, "new": False,
        "meta_hash": meta_hash, "access": meta["access"],
        "audience": meta["audience"], "meta": meta,
    }
    text = "\n".join([
        "App approval: let an app go live",
        "App name: Team report", "Kind: page", "Address slug: team-report",
        "Who can open it: group unknown (" + "44" * 16 + ")",
        "The app may contact: https://api.example.com",
        "The app runs code in the browser of everyone who opens it",
        f"File: index.html ({len(data)} bytes, sha256 {meta['files'][0]['sha256']})",
        f"Workspace: unknown ({ws})", f"Hub: unknown ({hub})", f"App id: {app}",
        "Staged release: " + release["staged_id"], f"App hash: {meta_hash}",
    ])
    # Canonical unsigned event: the ordinary F1 envelope plus the new signed payload.
    # Signature cryptography is covered by F1; this vector pins the new signing content.
    event = {
        "v": 2, "id": "01J9ZP0000000000000000EVT1", "type": "log.added",
        "actor": {"kind": "person", "id": "p_" + "77" * 16, "device": "d_" + "88" * 16},
        "auth": "convenience", "hash_v": 1, "roster_v": 1,
        "based_on": "sha256:" + "66" * 32, "text": text, "release": release,
    }
    body = {
        "op": "commit", "app_id": app, "version": 7, "meta_hash": meta_hash,
        "reach_hash": digest(b"orch/v2/app-reach|" + cj(meta["reach"])),
        "access": meta["access"], "audience": meta["audience"],
    }
    stored = {f["path"]: [f["size"], f["sha256"]] for f in meta["files"]}
    base = {"body": body, "meta": meta, "stored_files": stored,
            "forbidden": ["https://hub.example.com", "https://publish.example.com"]}
    cases = []
    for name, expected in [("commit_meta_hash_ok", None),
                           ("commit_meta_hash_mismatch", "meta_mismatch"),
                           ("commit_meta_hash_missing", "malformed"),
                           ("commit_meta_hash_without_domain", "meta_mismatch"),
                           ("commit_meta_hash_reach_changed", "meta_mismatch")]:
        case = {"name": name, **copy.deepcopy(base), "expect": expected}
        if name == "commit_meta_hash_mismatch":
            case["body"]["meta_hash"] = digest(b"orch/v2/app-meta|" + b"different canonical meta")
        elif name == "commit_meta_hash_without_domain":
            case["body"]["meta_hash"] = digest(cj(meta))
        elif name == "commit_meta_hash_missing":
            del case["body"]["meta_hash"]
        elif name == "commit_meta_hash_reach_changed":
            case["meta"]["reach"] = ["https://other.example.com"]
            # Keep reach_hash correct so only meta_hash causes this refusal.
            case["body"]["reach_hash"] = digest(b"orch/v2/app-reach|" + cj(case["meta"]["reach"]))
        cases.append(case)
    result = {
        "doc": "Protocol 11.3 RG-1 to RG-9, 4 Nov 2026. Synthetic ids; no real keys. "
               "Commit cases assume valid request approval and exercise stored-meta verification only.",
        "status": "pending", "pending_reason": "Spec-only amendment: release fields, commit meta_hash checking and RG-9 start custody are not implemented",
        "required": {"commit": [c["name"] for c in cases], "release_note": ["release_note_fields"],
                     "state": ["commit_to_stopped_app_stays_stopped", "first_commit_new_app_stays_stopped",
                               "release_new_app_start_ok", "release_existing_app_start_refused"]},
        "commit_cases": cases,
        "state_cases": [{
            "name": "commit_to_stopped_app_stays_stopped", "status": "pending",
            **copy.deepcopy(base),
            "initial_state": {"running": False, "current_version": 6},
            "approval": "otherwise valid owner approval for this exact commit request",
            "expect": {"error": None, "running": False, "current_version": 7},
            "scope": "orch-publish commit state transition, even with valid approval; "
                     "only app.start with its own approval starts the app. "
                     "The signer state check uses its own last signed app.start/app.stop approval for that app; "
                     "commit approvals do not change the persistent signer-owned advisory record; "
                     "an absent record shows no warning; a stopped indication only warns in the signer's own review and never refuses: "
                     "This app was stopped the last time this computer approved it; publishing keeps it stopped. "
                     "orch-publish preserves running state regardless of that check.",
        }, {
            "name": "first_commit_new_app_stays_stopped", "status": "pending",
            **copy.deepcopy(base),
            "initial_state": {"exists": False},
            "approval": "otherwise valid owner approval for this exact commit request",
            "expect": {"error": None, "running": False, "current_version": 7},
            "scope": "New app creation is stopped and first commit preserves that state; "
                     "RG-9 permits a separate matching start approval from the same grant after commit.",
        }, {
            "name": "release_new_app_start_ok", "status": "pending",
            "release": copy.deepcopy(release),
            "initial_state": {"exists": False, "slug": meta["slug"]},
            "presses": 1,
            "requests": [
                {"at_ms_from_press": 1000, "nonce": "aa" * 16, "body": copy.deepcopy(body)},
                {"at_ms_from_press": 2000, "nonce": "bb" * 16,
                 "body": {**copy.deepcopy(body), "op": "start"}},
            ],
            "signer_commit_approval_signed": True,
            "expect": {"commit_approvals": 1, "start_approvals": 1,
                       "running": True, "current_version": 7},
            "scope": "RG-9: preallocated app id and new: true; signer signs commit approval before "
                     "start, version remains current, both approvals have valid independent 120 s "
                     "windows/nonces within the original 600000 ms grant life; no second press.",
        }, {
            "name": "release_existing_app_start_refused", "status": "pending",
            "release": copy.deepcopy(release),
            "initial_state": {"exists": True, "running": False, "current_version": 6},
            "presses": 1,
            "requests": [
                {"at_ms_from_press": 1000, "nonce": "cc" * 16, "body": copy.deepcopy(body)},
                {"at_ms_from_press": 2000, "nonce": "dd" * 16,
                 "body": {**copy.deepcopy(body), "op": "start"}},
            ],
            "expect": {"commit_approvals": 1, "start_approvals": 0,
                       "start_refused": True, "running": False, "current_version": 7},
            "scope": "RG-8: an existing-app release never grants start authority, even after a "
                     "successful matching commit with otherwise valid timestamps and distinct nonces.",
        }],
        "release_note_cases": [{
            "name": "release_note_fields", "event": event,
            "workspace_id": ws, "log": meta["ticket"],
            "event_canonical_utf8": cj(event).decode(),
            "meta": meta, "meta_canonical_utf8": cj(meta).decode(), "meta_hash": meta_hash,
            "expect": {"ok": True},
            "scope": "Unsigned canonical event content, before the ordinary F1 device signature; "
                     "text is an example signer rendering, not a new fixed prose grammar. "
                     "A valid signature alone does not prove a fresh press or create a grant.",
        }],
    }


    new_meta = {**copy.deepcopy(meta), "new": True}
    new_hash = digest(b"orch/v2/app-meta|" + cj(new_meta))
    new_release = {**copy.deepcopy(release), "new": True, "meta": new_meta, "meta_hash": new_hash}
    for case in result["state_cases"]:
        for request in case.get("requests", []):
            request["method"] = "POST"
            request["target"] = f"/v1/ws/{ws}/apps/{app}/" + request["body"]["op"]
        if case["name"] in {"release_new_app_start_ok", "first_commit_new_app_stays_stopped"}:
            case["release"] = copy.deepcopy(new_release)
            case["meta_put"] = {"meta": copy.deepcopy(new_meta), "expect": {"error": None, "running": False}}
            if "meta" in case:
                case["meta"] = copy.deepcopy(new_meta)
                case["body"]["meta_hash"] = new_hash
            for request in case.get("requests", []):
                request["body"]["meta_hash"] = new_hash
    result["create_cases"] = []
    for name, is_new, initial, error in [
        ("new_app_id_exists_refused", True, {"app_ids": [app], "slugs": []}, "app_exists"),
        ("new_app_slug_taken_refused", True, {"app_ids": ["99" * 16], "slugs": [meta["slug"]]}, "slug_taken"),
        ("existing_app_unknown_refused", False, {"app_ids": [], "slugs": []}, "not_found"),
    ]:
        result["create_cases"].append({"name": name, "status": "pending",
            "release": copy.deepcopy(new_release if is_new else release),
            "meta": copy.deepcopy(new_meta if is_new else meta), "initial_state": initial,
            "method": "PUT", "target": f"/v1/ws/{ws}/apps/{app}/versions/7",
            "expect": {"error": error, "state_unchanged": True}})
    result["start_cases"] = []
    for name, current, mismatch in [
        ("start_version_not_current", 8, False),
        ("start_no_current_version", None, False),
        ("start_meta_hash_mismatch", 7, True),
    ]:
        start_body = {**copy.deepcopy(body), "op": "start", "meta_hash": new_hash}
        if mismatch:
            start_body["meta_hash"] = digest(b"different approved meta")
        result["start_cases"].append({"name": name, "status": "pending",
            "body": start_body, "meta": copy.deepcopy(new_meta), "stored_files": stored,
            "method": "POST", "target": f"/v1/ws/{ws}/apps/{app}/start",
            "initial_state": {"running": False, "current_version": current},
            "approval": "otherwise valid owner approval; signer has signed this grant's commit approval",
            "expect": {"error": "meta_mismatch" if mismatch else "version_not_current", "running": False}})
    for field in ("app_id", "new"):
        invalid = copy.deepcopy(event)
        del invalid["release"][field]
        result["release_note_cases"].append({"name": f"release_missing_{field}_refused",
            "event": invalid, "event_canonical_utf8": cj(invalid).decode(), "expect": {"ok": False}})
    for name, field, value in [("release_null_app_id_refused", "app_id", None),
                               ("release_non_boolean_new_refused", "new", 1)]:
        invalid = copy.deepcopy(event)
        invalid["release"][field] = value
        result["release_note_cases"].append({"name": name, "event": invalid,
            "event_canonical_utf8": cj(invalid).decode(), "expect": {"ok": False}})
    for category in ("create", "start", "release_note"):
        result["required"][category] = [c["name"] for c in result[category + "_cases"]]
    return result


if __name__ == "__main__":
    Path(__file__).with_name("release_grant_v1.json").write_text(
        json.dumps(build(), indent=1, sort_keys=True) + "\n"
    )
