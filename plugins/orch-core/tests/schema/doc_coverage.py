"""Maps every schema to the format-doc section it implements, and what the doc names that the tests cross-check.

Maintained by hand; ``test_schemas.py`` fails when a schema, an event type, a payload field, an actor rule or a
value list in the doc has no counterpart (and the other way round).
FORMAT = docs/architecture/orch-v2-ticket-format.md (F1), CORE = docs/architecture/orch-v2-core.md.
"""

SCHEMA_TO_DOC = {
    "common": "FORMAT 5.7 policy, 11.1 ids, 11.4 value lists, 5.3 signed objects (certificate, revocation)",
    "event": "FORMAT 5.1 envelope, 5.2 actors, 5.3 signed person events; event.<type> are 5.4.1 and 5.4.2",
    "workspace": "FORMAT 2 config.json; T5, T8, D59, D60",
    "keys-line": "FORMAT 2 keys.jsonl; T1, T2",
    "ticket": "FORMAT 3 ticket.json (17 keys, defaults); T4, T6, T10, T11",
    "body": "FORMAT 4 body.md sections table; T15",
    "artifact": "FORMAT 6 artifacts (manifest entry)",
    "gate-input": "FORMAT 5.7 gate hash input G (artifacts {kind, digest, ac, task}, receipts)",
    "checkpoint": "FORMAT 5.10 checkpoints (protocol 2.4 signed object, two kinds)",
    "addon-manifest": "FORMAT 8 minimal manifest; T11, T13",
    "operation": "CORE 3 operation registry fields; FORMAT A1",
    "cli-result": "FORMAT 10.4 items 3, 9",
    "cli-error": "FORMAT 10.4 item 4",
    "wait-result": "FORMAT 10.4 item 7",
}

# Headings the schemas cite; the test fails if the doc drops or renames one.
DOC_HEADINGS = [
    "## 2. Workspace layout",
    "## 3. `ticket.json`",
    "## 4. `body.md`",
    "### 5.1 The envelope",
    "### 5.2 Actors",
    "### 5.3 Human signatures, devices and `auth`",
    "### 5.4 Event types",
    "#### 5.4.1 Ticket log",
    "#### 5.4.2 Workspace log",
    "### 5.6 Hashes",
    "### 5.7 Gates",
    "### 5.10 Checkpoints and restore",
    "## 6. Artifacts",
    "## 8. Addons",
    "### 10.4 Output and errors",
    "### 11.1 Field types and ids",
    "### 11.4 Value lists",
]

# Payload fields whose F1 name is the envelope's name (5.1: "Payload fields never reuse an envelope name"): the doc
# name -> the name the schema uses. invalid.acknowledged's "seq" is the lone case.
RENAMED_PAYLOAD_FIELDS = {"invalid.acknowledged": {"seq": "invalid_seq"}}

# Event rows whose Payload cell is not a plain "`field`; `field?`" list.
IRREGULAR_PAYLOAD_CELLS = {"artifact.added", "artifact.replaced"}
ARTIFACT_FIELDS = {
    "artifact.added": ({"name", "kind"}, {"sha256", "bytes", "addon", "ref", "task", "ac", "label"}),
    "artifact.replaced": ({"name", "kind"}, {"sha256", "bytes", "task", "ac", "label", "replaces"}),
}

ENVELOPE_FIELDS = [
    "v", "id", "seq", "at", "type", "actor", "based_on", "prev", "hash_v",
    "ws_seq", "roster_v", "auth", "sig", "host_sig",
]  # fmt: skip

# 11.4 value lists: doc row name -> (schema, def name or JSON path into properties)
VALUE_LISTS = {
    "ticket type": ("common", "ticketType"),
    "status": ("common", "status"),
    "member role": ("common", "memberRole"),
    "ticket people (T4, `people.changed`)": ("common", "ticketPeopleRole"),
    "approver token (policies)": ("common", "approverToken"),
    "question `to` role": ("common", "questionToRole"),
    "gate": ("common", "gateName"),
    "verdict outcome": ("common", "verdictOutcome"),
    "close resolution": ("common", "closeResolution"),
    "claim release reason": ("common", "claimReleaseReason"),
    "`gate.invalidated` cause": ("common", "invalidatedCause"),
    "`device.revoked` reason": ("common", "revokeReason"),
    "`projection.repaired` cause": ("common", "repairCause"),
    "`auth`": ("common", "auth"),
    "grant scope": ("common", "grantScope"),
    "core artifact kind": ("common", "coreArtifactKind"),
    "addon capability": ("common", "capability"),
}
