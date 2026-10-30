"""Codes the remote client shows, in plain words (R5 design section 6; hub contract 10.2c / 10.2d).

Every refusal the client raises is a :class:`ClientError` with a stable ``code`` (the contract's close reasons, the
hub's
``error`` / ``refused`` codes, or one of the client's own ``remote_client.*`` codes) and a ``message`` a person can
read.
The UI shows ``message``; the code goes behind "Technical details". Nothing here contains a key, a token or a name that
came from the hub."""

from __future__ import annotations

__all__ = ["PLAIN", "ClientError", "plain"]

PLAIN: dict[str, str] = {
    # hub: error / refused
    "malformed": "The connection check failed. Try again.",
    "no_session": "Sign in to the hub again.",
    "not_allowed": "Not available (off, offline, or not shared with you).",
    "unavailable": "The hub could not do that right now. Try again in a moment.",
    "remote.busy": "Too many connections at the moment. Try again in a moment.",
    "replay": "That code was already used. Make a new one.",
    # host / stream close reasons
    "remote.off": "Remote access is off at the workspace.",
    "remote.device": "This computer is no longer added.",
    "remote.no_code": "There is no open code at the workspace. Make a new one there.",
    "remote.version": "This computer and the workspace run different orch versions. Update orch on both.",
    "handshake": "Connection check failed.",
    "presence": "Not confirmed.",
    "idle": "The session ended because it was idle.",
    "expired": "The session has ended. Open it again.",
    "revoked": "Access was turned off at the workspace.",
    "closed": "The session was closed.",
    "too_large": "Too much data was sent.",
    # the client's own
    "remote_client.hub_key_changed": "The hub's key changed. Connect again on the office network.",
    "remote_client.no_pin": "Use the connect link from your hub admin.",
    "remote_client.hub_mismatch": "This is not the hub you connected to.",
    "remote_client.alpn": "The hub does not offer remote access.",
    "remote_client.no_hub": "Connect to your hub first.",
    "remote_client.not_added": "This computer is not added to that workspace.",
    "remote_client.bad_code": "That code does not look right. Make a new one at the workspace.",
    "remote_client.browser_code": "This code is for a browser. Make a code for a computer.",
    "remote_client.wrk_mismatch": "The code does not match this workspace's key. Make a new one.",
    "remote_client.fingerprint": "The workspace shows different words. Do not add this computer.",
    "remote_client.key_file_refused": "A key file cannot confirm anything here. Use Touch ID or a security key.",
    "remote_client.no_presence": "No Touch ID or security key is available on this computer.",
    "remote_client.fido2_no_uv": "This security key has no PIN or fingerprint. Set a PIN first.",
    "remote_client.fido2_algorithm": "This security key is not supported.",
    "remote_client.assertion": "The security key answered something unexpected. Nothing was sent.",
    "remote_client.cancelled": "Not confirmed.",
    "remote_client.not_confirmed": "Not confirmed.",
    "remote_client.no_digest": "This computer could not check what was asked. Nothing was confirmed.",
    "remote_client.protocol": "The hub sent something unexpected. The connection was closed.",
    "remote_client.timeout": "The hub or the workspace did not answer in time.",
    "remote_client.closed": "The connection was closed.",
    "remote_client.helper": "The Touch ID helper is not available.",
    "remote_client.state": "This computer's remote access data could not be read.",
    "remote_client.bearer": "Not allowed.",
}


class ClientError(Exception):
    """A refusal of the client, with its code and the plain text for it."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.message = plain(code)
        self.detail = detail  # for Technical details only; never a key, token or hub-supplied name


def plain(code: str) -> str:
    return PLAIN.get(code, "Something went wrong. Try again.")
