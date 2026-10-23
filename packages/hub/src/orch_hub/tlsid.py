"""The hub's TLS identity: a P-256 key and a self-signed certificate. Trust is the SPKI pin on the card, not the
certificate, so the certificate is rebuilt from the stored key whenever needed (D12b: the key persists in the key store)."""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from .contract.canon import b64u


def spki_sha256(cert: x509.Certificate) -> str:
    der = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    h = hashes.Hash(hashes.SHA256())
    h.update(der)
    return b64u(h.finalize())


@dataclass(frozen=True)
class TlsIdentity:
    """The hub's TLS key and self-signed certificate; the card's ``tls_spki_sha256`` is :attr:`spki_sha256`."""

    key: ec.EllipticCurvePrivateKey
    cert: x509.Certificate

    @staticmethod
    def from_key(key: ec.EllipticCurvePrivateKey, name: str = "orch-hub") -> TlsIdentity:
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        now = datetime.datetime.now(datetime.UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .sign(key, hashes.SHA256())
        )
        return TlsIdentity(key, cert)

    @staticmethod
    def generate(name: str = "orch-hub") -> TlsIdentity:
        return TlsIdentity.from_key(ec.generate_private_key(ec.SECP256R1()), name)

    @property
    def spki_sha256(self) -> str:
        return spki_sha256(self.cert)
