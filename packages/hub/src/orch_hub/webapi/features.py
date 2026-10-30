"""The one list of web API features. A feature is a ``*_api.py`` module with a handler mixin (methods of
:class:`~orch_hub.webapi.api.WebApi`) and, usually, a ``routes(r)`` function that builds its routes with ``api._r``.

To add a feature: write ``webapi/xxx_api.py``, then add ONE ``Feature(...)`` line to :data:`FEATURES` below. ``api.py``
builds the ``WebApi`` bases, the ROUTES table and the STATUS table from this list; it needs no edit. (This module never
imports ``api``, so no import cycle appears.) The order is the order of the mixin bases and of the routes.

A route's ``handler`` name maps to the mixin method ``_h_<handler>``. New refusal codes go in the feature's ``status``
(code -> HTTP status), merged into STATUS. A route not named in api.py's session-scope tables (``_PAIR``, ``_CHAN``)
is reachable only from a full browser session, which is right for admin pages; only a route that a pairing or channel
session must reach needs a line there."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .ask_phone_api import STATUS as ASK_PHONE_STATUS
from .ask_phone_api import AskPhoneHandlers, ask_phone_routes
from .backup_api import STATUS as BACKUP_STATUS
from .backup_api import BackupHandlers, backup_routes
from .changes_api import ChangeHandlers
from .invite_status_api import STATUS as INVITE_STATUS
from .invite_status_api import InviteStatusHandlers, invite_status_routes
from .oidc_api import STATUS as OIDC_STATUS
from .oidc_api import OidcHandlers, oidc_routes
from .org_api import OrgHandlers
from .published_api import PublishedHandlers, published_routes
from .secrets_api import SecretHandlers, secret_routes
from .services_api import STATUS as SERVICE_STATUS
from .services_api import ServiceHandlers, service_routes
from .settings_api import SettingsHandlers, settings_routes
from .spaces_api import SpaceHandlers, space_routes
from .update_request_api import UpdateRequestHandlers, update_request_routes


@dataclass(frozen=True)
class Feature:
    mixin: type  # the handler methods, a base class of WebApi
    routes: Callable[[Any], tuple[Any, ...]] | None = None  # ``routes(api._r)`` -> the feature's Route tuple
    status: dict[str, int] = field(default_factory=dict)  # wire code -> HTTP status this feature adds to STATUS


FEATURES: tuple[Feature, ...] = (
    Feature(mixin=ChangeHandlers),  # its routes are listed in api.py (inside the admin block)
    Feature(mixin=OrgHandlers),  # its routes are listed in api.py (inside the admin block)
    Feature(mixin=SecretHandlers, routes=secret_routes),
    Feature(mixin=SettingsHandlers, routes=settings_routes),
    Feature(mixin=PublishedHandlers, routes=published_routes),
    Feature(mixin=BackupHandlers, routes=backup_routes, status=BACKUP_STATUS),
    Feature(mixin=SpaceHandlers, routes=space_routes),
    Feature(mixin=AskPhoneHandlers, routes=ask_phone_routes, status=ASK_PHONE_STATUS),
    Feature(mixin=InviteStatusHandlers, routes=invite_status_routes, status=INVITE_STATUS),
    Feature(mixin=ServiceHandlers, routes=service_routes, status=SERVICE_STATUS),
    Feature(mixin=UpdateRequestHandlers, routes=update_request_routes),
    Feature(mixin=OidcHandlers, routes=oidc_routes, status=OIDC_STATUS),
)

FEATURE_MIXINS: tuple[type, ...] = tuple(f.mixin for f in FEATURES)


def feature_routes(r: Any) -> tuple[Any, ...]:
    """Every feature's routes, in list order."""
    return tuple(route for f in FEATURES if f.routes is not None for route in f.routes(r))


def feature_status() -> dict[str, int]:
    """The wire code -> HTTP status entries the features add."""
    out: dict[str, int] = {}
    for f in FEATURES:
        out.update(f.status)
    return out
