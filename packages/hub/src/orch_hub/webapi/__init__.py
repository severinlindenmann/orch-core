"""The hub's JSON web API (V3-D13, orch-core#893): the admin console, the person home and the phone's workspace list.

``WebApi`` is transport-free (``handle(request) -> response``); ``WebServer`` serves it over plain HTTP on a loopback
address (dev, behind the edge's TLS). Every route and its checks are listed in ``api.ROUTES``.
"""

from .api import ROUTES, SessionBridge, WebApi, WebDeps
from .server import WebServer

__all__ = ["ROUTES", "SessionBridge", "WebApi", "WebDeps", "WebServer"]
