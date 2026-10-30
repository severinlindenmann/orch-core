"""The remote client's loopback app (R5, #1593; design ``r5-client-design.md`` sections 1, 4 and 6).

A separate minimal app, never the dashboard app: it imports neither ``orch_dashboard.app`` nor an HTTP client.
The client core (``orch.remoteclient``) does the hub, the end-to-end session and the presence keys; this package
serves the person's browser over ``127.0.0.1``: one origin for the client's own screens (the home page) and one per
open workspace."""

from orch.remoteclient import REMOTE_API

from .controller import Controller, GatedPresence, workspace_label
from .origin import Origin
from .static import StaticSite, installed_static_dir

__all__ = [
    "REMOTE_API",
    "Controller",
    "GatedPresence",
    "Origin",
    "StaticSite",
    "installed_static_dir",
    "workspace_label",
]
