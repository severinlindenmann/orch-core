"""What the host decided about one request envelope, and the fixed refusal codes (spec §6.2)."""
from __future__ import annotations

from dataclasses import dataclass, field

from orch.remote.bridge_host.envelope import Header

CODES = frozenset({"malformed", "not_paired", "revoked", "bad_signature", "rid_conflict", "already_done",
                   "stale_timestamp", "stale_sequence", "pairing_closed", "forbidden_scope", "assertion_required",
                   "lease_required", "assertion_failed", "scope_changed", "stopped",
                   "busy"})  # busy: this host's addition (request store quota), carries no field
# The only fields a refusal carries, per code: never anything taken from the request.
REFUSAL_FIELDS = {"stale_sequence": {"high"}, "stale_timestamp": {"host_ms"}, "already_done": {"status"},
                  "assertion_required": {"purpose", "scope", "expires_ms", "nonce", "subject"},
                  "lease_required": {"purpose", "scope", "expires_ms", "nonce", "subject"}}

PAIR_REFUSALS = frozenset({"pairing_closed", "bad_signature", "stale_timestamp", "malformed"})


@dataclass(frozen=True)
class Verdict:
    """result is one of:
    drop               answer nothing (`why` says which rule, for the host's own log only)
    refuse             send a sealed, host-signed refusal `code` with `fields`
    accept             the request passed §6.1 steps 1-8 and is recorded with no outcome yet: authorize it next
    replay             the stored outcome of a finished request: send it again, re-sealed; it never runs again
    run                run `meta`/`data` now (after authorize); `fresh` when a fresh assertion allowed exactly this
    pair_pending, pair_status, credential_begin, credential_finish, cancel
                       answer the device with `fields` (pairing, §8.1 and §9.2), or close a stream it opened
    """
    result: str
    why: str = ""
    code: str | None = None
    fields: dict = field(default_factory=dict)
    header: Header | None = None
    device: str | None = None  # hex of the signing device, once known
    scope: str | None = None  # the device's scope (accept, run)
    meta: dict | None = None
    data: bytes = b""
    outcome: dict | None = None  # replay: the stored response head
    body: bytes = b""  # replay: the stored body
    fresh: bool = False
    rid: str | None = None  # run: the request that runs (R1 after an assertion)
    answer_rids: tuple[str, ...] = ()  # run: every request whose record takes the result (R1, and R2 for a fresh run)
    # what the decision was taken on (accept, run): the registry entry read once, its generation, the host clock at
    # the decision, and when a fresh-assertion or lease grant behind a run ends (None: no grant needed)
    entry: object = None
    gen: int | None = None
    at_ms: int | None = None
    until_ms: int | None = None


def drop(why: str) -> Verdict:
    return Verdict("drop", why=why)


def refuse(code: str, why: str = "", **fields) -> Verdict:
    allowed = REFUSAL_FIELDS.get(code, set())
    if code in PAIR_REFUSALS:  # §6.2: every refusal to an op=pair request also carries host_pub
        allowed = allowed | {"host_pub"}
    if code not in CODES or set(fields) - allowed:
        raise ValueError(f"not a refusal the protocol has: {code} {sorted(fields)}")
    return Verdict("refuse", why=why, code=code, fields=fields)
