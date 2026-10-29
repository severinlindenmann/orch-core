"""Hub settings routes (M4, #1460). A mixin of :class:`~orch_hub.webapi.api.WebApi` (it only uses ``self.d``) plus its
route table. Read only: the hub name, the company and the addresses fixed at install, and how people sign in. The answer
is built from ``hub-settings.json`` and constants; it carries no key, token or password."""

from __future__ import annotations

from typing import Any

from ..contract.phase4 import render_text
from ..settings import SettingsError, load

#: how people sign in today (read only: password + code is the one method, so it cannot be turned off)
SIGN_IN = (
    {"id": "password", "label": "Name + password + 6-digit code", "note": "For everyone", "on": True},
    {"id": "passkey", "label": "Passkey (Touch ID / Face ID)", "note": "Later", "on": False},
    {"id": "company", "label": "Company login (Microsoft Entra / Google)", "note": "Later", "on": False},
)


def settings_routes(r: Any) -> tuple[Any, ...]:
    return (
        r(
            "GET",
            "/admin/settings",
            "admin",
            "a_settings",
            "Hub settings: name, company, the addresses fixed at install, sign-in methods (read only)",
        ),
    )


class SettingsHandlers:
    d: Any

    def _h_a_settings(self, c: Any, p: dict, q: dict, b: dict | None):
        file = self.d.settings_file
        try:
            s = load(file.parent) if file is not None else None
        except SettingsError:
            return 200, {"configured": False, "problem": "The settings file cannot be read."}
        if s is None:
            return 200, {"configured": False, "problem": None}
        return 200, {
            "configured": True,
            "name": render_text(s["name"]),
            "company": render_text(s["company"]),
            "addresses": [{"kind": a["kind"], "web": a["web"]} for a in s["addresses"]],
            "fixed_at_install": True,
            "sign_in": list(SIGN_IN),
        }
