"""Start, stop, restart a service and read its logs from the Services page (HUB-SV-02 / HUB-SV-03). A mixin of
:class:`~orch_hub.webapi.api.WebApi` (it only uses ``self.d`` and ``self._limit``).

Every route is hub-admin only (the pipeline's cookie, CSRF, origin rules; a switch-ticket session never reaches them).
The web layer holds no power of its own: ``prepare`` and ``run`` go to the helper, which makes the nonce, checks from the
stores that this person is an admin NOW and that THEIR channel key signed the exact context it made, and then runs one
fixed command. The console recomputes the digest from the context it shows (same shape as an access-change approval)."""

from __future__ import annotations

from typing import Any

from ..services.helper_client import RUN_TIMEOUT_S
from ..services.helper_proto import HelperError

PREPARE_LIMIT = (10, 60_000)
LOGS_LIMIT = (12, 60_000)

STATUS = {
    "helper_unavailable": 503,
    "not_set_up": 503,
    "busy": 409,
    "unknown_part": 404,
    "bad_action": 400,
    "update_unavailable": 409,
    "action_failed": 502,
    "bad_config": 503,
}


def _need_str(b: dict | None, key: str) -> str:
    v = (b or {}).get(key)
    if type(v) is not str:
        from .api import ApiError

        raise ApiError("malformed")
    return v


class ServiceHandlers:
    d: Any
    _limit: Any

    def _helper(self, op: str, **fields: Any) -> dict[str, Any]:
        from .api import ApiError

        if self.d.helper is None:
            raise ApiError("helper_unavailable")
        try:
            return self.d.helper.call(op, **fields)
        except HelperError as e:
            from .api import STATUS as ALL

            raise ApiError(e.code if e.code in ALL else "internal") from None

    def _service_helper_info(self) -> dict[str, Any]:
        if self.d.helper is None:
            return {"state": "not_set_up", "parts": []}
        return self.d.helper.info()

    def _h_a_service_prepare(self, c: Any, p: dict, q: dict, b: dict | None):
        self._limit.hit("service-prepare", c.user_id, PREPARE_LIMIT)
        return 200, self._helper(
            "prepare",
            user_id=c.user_id,
            channel_id=_need_str(b, "channel_id"),
            part=p["part"],
            action=_need_str(b, "action"),
        )

    def _h_a_service_run(self, c: Any, p: dict, q: dict, b: dict | None):
        self._limit.hit("service-run", c.user_id, PREPARE_LIMIT)
        got = self._helper(
            "run",
            timeout_s=RUN_TIMEOUT_S,
            user_id=c.user_id,
            channel_id=_need_str(b, "channel_id"),
            nonce=_need_str(b, "nonce"),
            proof=_need_str(b, "proof"),
        )
        return 200, {"ok": True, **got}

    def _h_a_service_logs(self, c: Any, p: dict, q: dict, b: dict | None):
        self._limit.hit("service-logs", c.user_id, LOGS_LIMIT)
        ch = q.get("channel_id", "")
        return 200, self._helper("logs", user_id=c.user_id, channel_id=ch, part=p["part"])


def service_routes(r: Any) -> tuple[Any, ...]:
    return (
        r(
            "POST",
            "/admin/services/{part}/prepare",
            "admin",
            "a_service_prepare",
            "Ask the helper for the action to review (start, stop, restart); it makes the nonce",
        ),
        r(
            "POST",
            "/admin/services/{part}/run",
            "admin",
            "a_service_run",
            "Run the reviewed action: the admin's channel proof over the helper's digest",
        ),
        r(
            "GET",
            "/admin/services/{part}/logs",
            "admin",
            "a_service_logs",
            "The last hour of a part's log, redacted by the helper (admin only, audited)",
        ),
    )
