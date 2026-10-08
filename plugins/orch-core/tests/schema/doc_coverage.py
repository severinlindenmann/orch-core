"""Maps every schema to the format-doc section it implements, and lists what the doc names.

Maintained by hand; ``test_schemas.py::test_doc_coverage`` fails when a schema or a doc-named event is missing.
FORMAT = docs/architecture/orch-v2-ticket-format.md, CORE = docs/architecture/orch-v2-core.md.
"""

SCHEMA_TO_DOC = {
    "common": "FORMAT sections 2, 3, 5 (id, hash, timestamp, role types); T5 gate policy",
    "workspace": "FORMAT section 2 config.json; T5, T8",
    "keys-line": "FORMAT section 2 keys.jsonl; T1, T2",
    "ticket": "FORMAT section 3 ticket.json; T4, T6, T10, T11",
    "body": "FORMAT section 4 body.md sections table; T15",
    "event": "FORMAT section 5 event envelope",
    "event-host": "FORMAT section 5 / T9: envelope variant without actor (edit.external, projection.repaired)",
    "artifact": "FORMAT section 6 artifact manifest entry",
    "checkpoint": "FORMAT section 5 Checkpoints",
    "addon-manifest": "FORMAT section 8 orch-addon.json; T11, T13",
    "operation": "CORE section 3 operation registry fields",
    "cli-result": "FORMAT 10.4 item 3, 9",
    "cli-error": "FORMAT 10.4 item 4",
    "wait-result": "FORMAT 10.4 item 7",
}

# Event types the format doc names literally (backticked), with where. Each must have an event.<type> schema.
DOC_NAMED_EVENTS = {
    "gate.approved": "section 5 example",
    "task.done": "section 5 example, 10.3",
    "artifact.added": "section 5 example, section 6",
    "artifact.replaced": "section 6",
    "edit.external": "T9, section 5",
    "projection.repaired": "section 2, section 5",
    "member.added": "section 2",
    "role.changed": "section 2",
    "policy.changed": "section 2",
    "addon.granted": "section 2, section 8",
    "restore": "section 5 Checkpoints",
}

# Event types implied by the doc (signed human actions, claims, ask/log/handoff...) whose names are chosen here.
DOC_IMPLIED_EVENTS = {
    "ticket.created": "T1/T2 key assignment",
    "ticket.updated": "T9 edits with base_rev",
    "ticket.submitted": "10.3 submit",
    "status.changed": "T14 status from events",
    "ticket.closed": "section 5 signed close",
    "ticket.reopened": "section 5 signed reopen",
    "people.changed": "T4, section 5 changes to people",
    "gate.changes_requested": "section 5 change requests",
    "verdict.given": "section 5 verdicts",
    "question.asked": "T6, 10.3 ask",
    "question.answered": "T6, section 5 answers",
    "claim.taken": "T7 claim and takeover",
    "claim.released": "T7, section 5 member removal",
    "claim.expired": "T7 claims expire",
    "task.started": "A4 lease",
    "task.skipped": "10.3 task skip",
    "task.blocked": "10.3 task block",
    "task.reopened": "10.3 task reopen",
    "log": "10.3 log",
    "handoff": "10.3 handoff",
    "member.removed": "section 5 removing a member",
    "addon.purged": "section 5 addon purge",
    "session.granted": "T7, A3, section 5 session grants",
}

# Event fields the doc names. Each (field, where) must be a property of the envelope or of a schema that uses it.
DOC_NAMED_ENVELOPE_FIELDS = [
    "v",
    "id",
    "seq",
    "at",
    "type",
    "actor",
    "based_on",
    "prev",
    "hash_v",
    "sig",
    "host_sig",
    "presence",
    "list_seq",
    "policy_hash",
]
DOC_NAMED_ARTIFACT_FIELDS = ["name", "kind", "sha256", "bytes", "task", "ac", "label", "actor", "addon", "ref"]
DOC_NAMED_CORE_ARTIFACT_KINDS = [
    "screenshot",
    "log",
    "report",
    "link",
    "dataset",
    "build",
    "diagram",
    "receipt",
    "feedback",
    "other",
]
DOC_NAMED_TICKET_FIELDS = [
    "schema",
    "uid",
    "key",
    "title",
    "type",
    "priority",
    "size",
    "labels",
    "parent",
    "blocked_by",
    "due",
    "visibility",
    "links",
    "acceptance",
    "tasks",
    "questions",
    "addons",
]
DOC_NAMED_MANIFEST_KEYS = [
    "fields",
    "sections",
    "artifact_kinds",
    "needs",
    "cli",
    "skills",
    "agents_md",
    "capabilities",
]
DOC_NAMED_OPERATION_FIELDS = ["name", "input", "who", "pre", "emits", "output", "errors"]
