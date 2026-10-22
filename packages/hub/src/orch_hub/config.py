"""``HubConfig``: the one frozen set of runtime switches of a hub process (orch-hub#30).

Built once, in ``serve`` (or a CLI command), from the hub card and the command-line flags; every service takes
``config.dev`` (or the field it needs) as a REQUIRED argument. There is no service default for the dev flag anywhere in
``orch_hub``: a construction that forgets it fails with a ``TypeError`` instead of running in dev mode (guard test:
``tests/test_hubconfig_guard.py``). The flag comes from the card the hub holds, never from a flag a caller can drop.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


def _https_origin(o: str) -> bool:
    u = urlsplit(o)
    return (
        u.scheme == "https"
        and bool(u.hostname)
        and not (u.path or u.query or u.fragment or u.username)
        and o == f"https://{u.netloc}"
    )


@dataclass(frozen=True)
class HubConfig:
    dev: bool  # the held card's dev marker: True accepts dev certificates and cards, False refuses them (spec §6)
    listen: str  # link listener, loopback host:port
    web_listen: str | None = None  # web API listener (loopback, the edge fronts it); None = no web API
    relay_url: str | None = None  # --relay-url, else the card's
    trusted_peers: frozenset[str] = frozenset()  # edge addresses that may state the client address (X-Forwarded-For)
    behind_edge: bool = False
    allowed_origins: frozenset[str] = frozenset()
    #: the hub's public API origin, signed into ``hub.api_origin`` (protocol §6.4). One of ``allowed_origins`` (the same
    #: configured value family, never a request's); None with exactly one allowed origin means that one
    api_origin: str | None = None

    def resolved_api_origin(self) -> str | None:
        """The origin ``GET /hub/card`` signs: ``api_origin``, else the single allowed origin, else None (the route then
        answers ``unavailable``; the loopback dev server learns its address only after it binds, see ``serve``)."""
        if self.api_origin is not None:
            return self.api_origin
        return next(iter(self.allowed_origins)) if len(self.allowed_origins) == 1 else None

    def __post_init__(self) -> None:
        if type(self.dev) is not bool:
            raise ValueError("HubConfig.dev must be a bool")
        if self.behind_edge and not self.trusted_peers:
            raise ValueError("behind_edge needs --trusted-peer (else every request has the edge as its source)")
        if self.behind_edge and not self.allowed_origins:
            raise ValueError("behind_edge needs --web-origin (the exact origin the edge serves the console from)")
        if self.api_origin is not None and self.api_origin not in self.allowed_origins:
            raise ValueError(
                "--api-origin must be one of the --web-origin values (one configured origin, signed as is)"
            )
        if self.behind_edge and self.resolved_api_origin() is None:
            raise ValueError("behind_edge with several --web-origin values needs --api-origin (the one the phone uses)")
        if self.behind_edge and not all(_https_origin(o) for o in self.allowed_origins):
            # the cookie is Secure and the Host check derives from these (design-a8 A8-4): exactly https://<console host>
            raise ValueError("behind_edge needs --web-origin to be exactly https://<host>[:port] (no path, no http)")
        if (self.behind_edge or self.trusted_peers or self.allowed_origins) and self.web_listen is None:
            raise ValueError("--trusted-peer / --behind-edge / --web-origin need --web-listen")

    @classmethod
    def from_card(cls, card: Mapping[str, Any], **flags: Any) -> HubConfig:
        """``card`` is the signed card object (``keys.card()``); its ``o.dev`` is the dev flag (fail closed: anything
        but a real bool is refused). ``relay_url`` falls back to the card's."""
        o = card["o"]
        dev = o.get("dev")
        if type(dev) is not bool:
            raise ValueError("the hub card carries no dev marker")
        if flags.get("relay_url") is None:
            flags["relay_url"] = o.get("relay_url")
        return cls(dev=dev, **flags)


def dev_of(keys: Any) -> bool:
    """The dev flag of the card ``keys`` holds, for the operator CLI commands that build a single service."""
    return HubConfig.from_card(keys.card(), listen="127.0.0.1:0").dev
