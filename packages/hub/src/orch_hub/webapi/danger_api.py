"""The danger zone (#1463): remove the hub. Admin only (``auth: "admin"``: a full cookie session; a switch ticket session
already answers ``reauth_required`` because these handlers are not in ``SWITCH_HANDLERS``). The flow is ``preview`` (a read),
``prepare`` (mints a single-use nonce and returns the context the page signs), ``confirm`` (the admin's password and a current
code in THIS request, the same step-up as the backup routes, plus the channel key's proof over the removal digest), ``status``
and ``cancel``. See :mod:`orch_hub.closing`."""

from __future__ import annotations

from typing import Any

from ..closing import ClosingError

STATUS = {
    "hub_closing": 409,
    "nonce_invalid": 403,
    "preflight_failed": 409,
    "removal_failed": 500,
    "removal_started": 409,
    "launch_failed": 500,
    "backup_failed": 500,
}


def danger_routes(r: Any) -> tuple[Any, ...]:
    return (
        r(
            "GET",
            "/admin/hub/remove/preview",
            "admin",
            "a_hub_remove_preview",
            "What removing the hub would do (changes nothing)",
        ),
        r(
            "POST",
            "/admin/hub/remove/prepare",
            "admin",
            "a_hub_remove_prepare",
            "Start a removal: a single-use nonce and the context to sign",
        ),
        r(
            "POST",
            "/admin/hub/remove/confirm",
            "admin",
            "a_hub_remove_confirm",
            "Remove the hub: password + code in the request and the channel key's proof over the removal digest",
        ),
        r("GET", "/admin/hub/remove/status", "admin", "a_hub_remove_status", "How far the removal is, in plain words"),
        r(
            "POST",
            "/admin/hub/remove/cancel",
            "admin",
            "a_hub_remove_cancel",
            "Stop the removal (only before company content is withdrawn)",
        ),
    )


def _err(e: ClosingError) -> Exception:
    from .api import ApiError  # late: api.py imports this module through features.py

    return ApiError(e.code)


def _token(c: Any) -> str:
    return str(c.token)


class DangerHandlers:
    d: Any

    def _closing(self):
        from .api import ApiError

        if self.d.closing is None:
            raise ApiError("unavailable")
        return self.d.closing

    def _h_a_hub_remove_preview(self, c: Any, p: dict, q: dict, b: dict | None):
        return 200, self._closing().preview()

    def _h_a_hub_remove_status(self, c: Any, p: dict, q: dict, b: dict | None):
        return 200, self._closing().status()

    def _h_a_hub_remove_prepare(self, c: Any, p: dict, q: dict, b: dict | None):
        from .api import ApiError

        if b is None or set(b) != {"channel_id"} or type(b["channel_id"]) is not str:
            raise ApiError("malformed")
        try:
            return 200, self._closing().prepare(c.user_id, b["channel_id"], _token(c))
        except ClosingError as e:
            raise _err(e) from None

    def _h_a_hub_remove_confirm(self, c: Any, p: dict, q: dict, b: dict | None):
        svc = self._closing()
        try:
            if b is not None and set(b) == {"password", "totp", "nonce", "proof"}:
                svc.check_nonce(c.user_id, b["nonce"], _token(c))  # before a code is spent
            b = self._stepup(c, b, "nonce", "proof")  # type: ignore[attr-defined]
            svc.confirm(c.user_id, b["nonce"], b["proof"], _token(c))
        except ClosingError as e:
            raise _err(e) from None
        return 202, {"ok": True}

    def _h_a_hub_remove_cancel(self, c: Any, p: dict, q: dict, b: dict | None):
        from .api import ApiError

        if b not in (None, {}):
            raise ApiError("malformed")
        try:
            self._closing().cancel(c.user_id)
        except ClosingError as e:
            raise _err(e) from None
        return 200, {"ok": True}
