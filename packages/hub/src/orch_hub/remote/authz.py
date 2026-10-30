"""Who may open a remote stream to which workspace (R4 design §4). Abuse limiting only: it grants nothing (D87).

* The token is a hub session of scope ``remote`` (made on the private network, ``client: "remote"``). Any other scope
  (a browser, a phone) is ``no_session`` here, and a ``remote`` token reaches no web route.
* The hub user may open to workspace W iff W is in the user's rail (:func:`orch_hub.switch.workspace_rows`: owner alias
  of a selective workspace, or a role in a full one) AND W has a live link. The rail answer is one bool: unknown,
  unbound, invisible and offline are all "not allowed", so a user learns nothing about workspaces outside the rail.
* The device's person is NOT compared to the hub user: the hub has no mapping from device ids to people and must not
  get one. The host's gate (device listed for a current owner) and the presence signature decide.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from ..channels.service import ChannelError


class AuthzError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Authz:
    def __init__(self, channels: Any, rail: Callable[[str], Iterable[str]]) -> None:
        self._ch, self._rail = channels, rail

    @classmethod
    def from_hub(cls, channels: Any, registry: Any, access: Any) -> Authz:
        from ..switch import workspace_rows

        return cls(channels, lambda user: [w["workspace_id"] for w in workspace_rows(registry, access, user)])

    def authenticate(self, token: object) -> str:
        """The hub user behind a ``remote``-scope token, re-checked live (an inactive or removed user is ``no_session``)."""
        if type(token) is not str:
            raise AuthzError("no_session")
        try:
            info = self._ch.session_info(token)
        except ChannelError:
            raise AuthzError("no_session") from None
        if info["scope"] != "remote":
            raise AuthzError("no_session")
        return info["user_id"]

    def may_open(self, user: str, workspace_id: str) -> bool:
        try:
            return workspace_id in set(self._rail(user))
        except Exception:  # noqa: BLE001  # fail closed
            return False
