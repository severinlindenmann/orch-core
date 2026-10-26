"""Fixtures for the fake hub: a fresh hub per test, and a host link for vector workspace A."""

from __future__ import annotations

import pytest

from .fake import FakeHub, HostLink


@pytest.fixture
def hub() -> FakeHub:
    return FakeHub()


@pytest.fixture
def link(hub: FakeHub) -> HostLink:
    return HostLink(hub)


@pytest.fixture
def joined(hub: FakeHub, link: HostLink) -> HostLink:
    link.join()
    return link
