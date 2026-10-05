"""The host half of the Orch Remote bridge protocol, version 1 (the specification's bridge-protocol.md; the contract is the
vector file in tests/fixtures/bridge).

A library only: no HTTP, no network, no dashboard import and no module-level state. Every clock, random source and
storage location is passed in by the caller, so everything here runs in tests against fake time and a temporary
config directory. The integration with the dashboard (the mailbox client, dispatch, heartbeat, the Remote tab) is
built on top of this, elsewhere.

Importing this package needs nothing beyond orch-core itself. The modules that do cryptography need the
`cryptography` package, which comes with the `dashboard` extra; without it they raise MissingCryptography with the fix,
and the rest of orch (the dashboard without the bridge included) is unaffected.

Layout, one concern per module:
  envelope      header layout, framing, canonical JSON, the idempotency digest (no keys)
  keys          HKDF, the workspace channel key and per-envelope keys, AES-256-GCM sealing, ids, MACs, the host pin
  signatures    ECDSA P-256 with raw r||s and the scalar range rule
  shown         cleaning the text a person approves, and the subject hash
  budgets       sliding-window counters (refusal budgets, the fresh-assertion rate limit)
  files         where the bridge's records live (the guarded permits folder) and how they are written and read
  replay_store  the durable request store: one record per request id, persisted before anything runs
  registry      the paired devices and the audit log of every change to them
  pairing       pairing offers, the op=pair checks, pending devices and their credential registration
  assertion     WebAuthn: challenges, registration and assertion verification
  host_check    the normative order of checks for a request, streams, leases and sealed responses
"""
from __future__ import annotations

CRYPTO_HINT = ("Orch Remote needs the Python package 'cryptography'. Install orch-core with its dashboard extra "
               "(for example: uv sync --extra dashboard, or pip install 'orch-core[dashboard]').")


class MissingCryptography(ImportError):
    """The `cryptography` package is not installed; the message says how to get it."""


def available() -> bool:
    """Whether the bridge can run here (the `cryptography` package imports)."""
    try:
        import cryptography.hazmat.primitives.ciphers.aead  # noqa: F401
    except ImportError:
        return False
    return True
