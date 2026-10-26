"""The refusal a pure identity check raises: a stable ``code`` token, no message parsing needed."""

from __future__ import annotations

__all__ = ["Refused"]


class Refused(Exception):
    """An identity object or event is refused. ``code`` is the protocol's refusal code where one exists
    (``cert_invalid``, ``cert_expired``, ``revoked``, ``bad_signature``, ``malformed``, ``other_person``,
    ``delegation_mismatch``) and an ``identity.*``/``grant.*``/``genesis.*`` token otherwise."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail
