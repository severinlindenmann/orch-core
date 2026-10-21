"""Keep credentials opaque so incidental formatting cannot disclose them."""

from __future__ import annotations

import hashlib
import hmac

# This is the only module that may read the token file; file access is added later.


class NoToken(Exception):
    """No token is stored. Carries no message and no path: the host maps it to ``github.no_token``."""

    def __init__(self) -> None:
        super().__init__("no github token")


class Token:
    """Explicit revelation prevents accidental logging and serialization of credentials."""

    __slots__ = ("__value",)
    __hash__ = None

    def __init__(self, value: str) -> None:
        if type(value) is not str or not 1 <= len(value) <= 255 or any(not "!" <= c <= "~" for c in value):
            raise ValueError("invalid github token") from None
        self.__value = value

    def __repr__(self) -> str:
        return "<github token>"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Token):
            return NotImplemented
        return hmac.compare_digest(self.__value, other.__value)

    def __reduce__(self):
        raise TypeError("github tokens cannot be serialized") from None

    def __getstate__(self):
        raise TypeError("github tokens cannot be serialized") from None

    def __copy__(self):
        raise TypeError("github tokens cannot be copied") from None

    def __deepcopy__(self, memo):
        raise TypeError("github tokens cannot be copied") from None

    def reveal(self) -> str:
        """Only the HTTP client needs the plaintext to authenticate a request."""
        return self.__value

    def fingerprint(self) -> str:
        """Partition future cache entries by credential without storing the credential."""
        return hashlib.sha256(self.__value.encode("ascii")).hexdigest()
