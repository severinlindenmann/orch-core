"""Signed releases: the envelope parser and verifier (install-update §4, CONTRACT.md)."""

from __future__ import annotations

from .verify import Reason, UpdateRefused, Verified, verify_envelope

__all__ = ["Reason", "UpdateRefused", "Verified", "verify_envelope"]
