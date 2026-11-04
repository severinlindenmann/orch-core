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
        "name": "Team report", "kind": "page", "access": "group",
        "audience": "group:" + "44" * 16, "slug": "team-report",
        "reach": ["https://api.example.com"],
        "files": [{"path": "index.html", "size": len(data),
                   "sha256": hashlib.sha256(data).hexdigest()}],
        "pw": None, "label": "First release", "ticket": "01J9ZP00000000000000000001",
    }
    meta_hash = digest(b"orch/v2/app-meta|" + cj(meta))  # §11.4's meta_hash, unchanged
    release = {
        "v": 1, "op": "commit", "staged_id": "pa_" + "55" * 16,
        "workspace_id": ws, "hub_id": hub, "app_id": app,
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
    return {
        "doc": "Protocol 11.3 RG-1 to RG-8, 4 Nov 2026. Synthetic ids; no real keys. "
               "Commit cases assume valid request approval and exercise stored-meta verification only.",
        "status": "pending", "pending_reason": "Spec-only amendment: release fields and the commit meta_hash check are not implemented",
        "required": {"commit": [c["name"] for c in cases], "release_note": ["release_note_fields"],
                     "state": ["commit_to_stopped_app_stays_stopped"]},
        "commit_cases": cases,
        "state_cases": [{
            "name": "commit_to_stopped_app_stays_stopped", "status": "pending",
            **copy.deepcopy(base),
            "initial_state": {"running": False, "current_version": 6},
            "approval": "otherwise valid owner approval for this exact commit request",
            "expect": {"error": None, "running": False, "current_version": 7},
            "scope": "orch-publish commit state transition, even with valid approval; "
                     "only app.start with its own approval starts the app. "
                     "Separately, the signer must refuse a grant approval for a last-known stopped app.",
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


if __name__ == "__main__":
    Path(__file__).with_name("release_grant_v1.json").write_text(
        json.dumps(build(), indent=1, sort_keys=True) + "\n"
    )
