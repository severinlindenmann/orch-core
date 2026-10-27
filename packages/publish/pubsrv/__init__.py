"""pubsrv: the orch v2 publish server (design: ~/orch-dev/state/design-publish-addon.md, section 2.5)."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("orch-publish")
except PackageNotFoundError:  # pragma: no cover - running from a bare checkout
    __version__ = "0.0.0"
