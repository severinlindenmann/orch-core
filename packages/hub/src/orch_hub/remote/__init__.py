"""Remote access through the hub (R4, #1592): the stream router and the remote-client endpoint."""

from collections.abc import Callable
from typing import Any

from .authz import Authz, AuthzError
from .endpoint import RemoteEndpoint
from .streams import Limits, Refused, StreamRouter


def wire_remote(
    channels: Any, registry: Any, access: Any, *, hub_id: str, count_op: Callable[[str, str, bool], None] | None = None
) -> tuple[StreamRouter, RemoteEndpoint]:
    """The production wiring (serve.py and the tests use it): the rail rule gates opens, each stream carries the recheck
    of the token that opened it, and signing out everywhere ends that user's streams at once."""
    authz = Authz.from_hub(channels, registry, access)
    router = StreamRouter(hub_id=hub_id, allowed=authz.may_open, count_op=count_op)
    channels.on_sessions_ended = router.revoke_user
    return router, RemoteEndpoint(router, authz)


__all__ = ["Authz", "AuthzError", "Limits", "Refused", "RemoteEndpoint", "StreamRouter", "wire_remote"]
