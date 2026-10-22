"""Hub channels (V3-D7, spec §7, protocol.md §6.2): sign-in sessions, channel registration with proof of possession,
channel certificates, listing and revocation. Public API: :class:`Channels` and :class:`ChannelError`."""

from .service import ChannelError, Channels, registration_object

__all__ = ["ChannelError", "Channels", "registration_object"]
