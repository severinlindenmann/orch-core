"""One refusal type for the hub client: a code of the hub contract (protocol section 14) or a host-side code."""

from __future__ import annotations

__all__ = ["HubRefused"]


class HubRefused(Exception):
    """``code`` is a protocol section 14 code (``dev_mismatch``, ``other_hub``, ``wrong_mode``, ``unbound`` ...) or one
    of the host-side codes ``full_mode_later``, ``no_epoch``, ``rotation_pending``, ``hub_unreachable``,
    ``resend_limited``, and the retryable sender codes ``epoch_changed`` and ``live_check_failed``. The message never
    echoes input."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
