"""The remote client on the person's computer (R5, #1593; spec ``orch-v2-hub-remote.md`` section 5, design
``r5-client-design.md``): reaches a workspace through the hub, end to end, with a presence key that stays in hardware.

Public API (all blocking, thread safe): :class:`Client`, :class:`Session`, :class:`Store`, :class:`ClientError`, the
presence backends (:class:`SecureEnclaveBackend`, :class:`Fido2Backend`) and :class:`Prompt`. The loopback server and
the UI
(``orch_dashboard.remote_client``) use only these."""

from __future__ import annotations

from ..hubclient.netconfig import NetConfigError, load_net  # this computer's own proxy settings, for the UI process
from .client import Client
from .codes import ClientError, plain
from .fido2_backend import Fido2Backend, HidAuthenticator
from .presence import PresenceBackend, Prompt
from .se_backend import SecureEnclaveBackend
from .session import REMOTE_API, Response, Session, WebSocket, WsRefused
from .state import Store, default_root

__all__ = [
    "NetConfigError",
    "load_net",
    "REMOTE_API",
    "Client",
    "ClientError",
    "Fido2Backend",
    "HidAuthenticator",
    "PresenceBackend",
    "Prompt",
    "Response",
    "SecureEnclaveBackend",
    "Session",
    "Store",
    "WebSocket",
    "WsRefused",
    "default_root",
    "plain",
]
