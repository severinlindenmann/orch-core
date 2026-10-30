"""r11 P12b: the state of one join link, for the Connect a workspace panel. A mixin of
:class:`~orch_hub.webapi.api.WebApi` (it uses ``self._inv`` and ``self._user_name``) plus its routes.

Only the admin who made the link may read it. A missing id, another admin's link, a reset or recovery link and a
malformed id all answer the same ``unknown_invite``, so the route is no oracle. ``user_id`` and ``name`` appear only once
the link was redeemed (the store records who, in the redeem's own transaction); a voided or expired link is ``ended``."""

from __future__ import annotations

from typing import Any

STATUS = {"unknown_invite": 404}


class InviteStatusHandlers:
    def _h_a_invite_status(self: Any, c: Any, p: dict, q: dict, b: dict | None):
        from .api import ApiError

        st = self._inv().status(p["invite"], c.user_id)
        if st is None:
            raise ApiError("unknown_invite")
        company = {"company": True} if st.get("company") else {}
        if st["state"] != "joined":
            return 200, {"state": st["state"], **company}
        uid = st["user_id"]
        if not self.d.access.has_user(uid):
            return 200, {"state": "ended", **company}  # redeemed, then removed: nobody to connect
        out = {"state": "joined", "user_id": uid, "name": self._user_name(uid), **company}
        if (
            company
        ):  # what the company login said, as display text for the maker only; the invite's name is the one in use
            idp = st.get("company_name", "")
            out.update(
                company_email=st.get("company_email", ""),
                company_name=idp,
                name_differs=bool(idp) and idp != out["name"],
            )
        return 200, out


def invite_status_routes(r: Any) -> tuple[Any, ...]:
    return (r("GET", "/admin/invites/{invite}", "admin", "a_invite_status", "Has the person used my sign-in link yet"),)
