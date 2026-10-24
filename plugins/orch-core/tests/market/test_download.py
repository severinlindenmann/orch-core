"""V3 G4 step 3 (design-g4 C3): ``orch.market.download``: the one checked fetcher for dependencies. A local TLS server
plays PyPI; the resolver is injected. Each refusal of design section 2 has a test."""

from __future__ import annotations

import datetime
import hashlib
import http.server
import json
import os
import ssl
import threading
import time

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from orch.addons import lockfile
from orch.addons.envtree import Budget
from orch.market import MarketError, download

BODY = b"hello wheel" * 100


def _cert(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pypi.org")])
    now = datetime.datetime.now(datetime.UTC)
    san = x509.SubjectAlternativeName([x509.DNSName("pypi.org"), x509.DNSName("files.pythonhosted.org")])
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1)).add_extension(san, critical=False).sign(key, hashes.SHA256())
    )  # fmt: skip
    cp, kp = tmp_path / "c.pem", tmp_path / "k.pem"
    cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    kp.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return cp, kp


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    hits: list[str] = []

    def log_message(self, *a):  # noqa: D102
        pass

    def do_GET(self):  # noqa: N802
        Handler.hits.append(self.headers.get("Host", "") + self.path)
        p = self.path
        if p == "/ok":
            return self.send(200, BODY)
        if p == "/redirect":
            self.send_response(302)
            self.send_header("Location", "https://127.0.0.1/x")
            self.send_header("Content-Length", "0")
            return self.end_headers()
        if p == "/missing":
            return self.send(404, b"no")
        if p == "/big":
            self.send_response(200)
            self.send_header("Content-Length", str(10 << 30))
            return self.end_headers()
        if p == "/lying-big":  # no length: more bytes than the cap
            self.send_response(200)
            self.send_header("Connection", "close")
            self.end_headers()
            for _ in range(200):
                self.wfile.write(b"x" * 1000)
            self.close_connection = True
            return None
        if p == "/short":  # promises more than it sends
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(b"x" * 10)
            self.close_connection = True
            return None
        if p == "/drip":
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            for _ in range(100):
                time.sleep(0.05)
                self.wfile.write(b"x")
                self.wfile.flush()
            return None
        if p == "/gzip":
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"zz")
            return None
        self.send(404, b"")

    def send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def server(tmp_path, monkeypatch):
    cp, kp = _cert(tmp_path)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    sc = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sc.load_cert_chain(cp, kp)
    srv.socket = sc.wrap_socket(srv.socket, server_side=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    Handler.hits = []
    calls = []

    def resolve(host, timeout):
        calls.append(host)
        return ["127.0.0.1"]

    def ctx():
        c = ssl.create_default_context()
        c.load_verify_locations(cp)
        return c

    monkeypatch.setattr(download, "_resolve", resolve)
    monkeypatch.setattr(download, "_check", lambda a: a)  # the test server is on loopback
    monkeypatch.setattr(download, "_context", ctx)
    monkeypatch.setattr(download, "_port", srv.server_address[1])
    yield calls
    srv.shutdown()
    srv.server_close()


PY = "python-requirements"


def dl(url, cap=1 << 20, secs=5.0, kind=PY):
    return download.fetch_bytes(url, kind, cap, time.monotonic() + secs)


def refused(url, code, reason=None, **kw):
    with pytest.raises(MarketError) as e:
        dl(url, **kw)
    assert (e.value.code, e.value.reason) == (code, reason), (url, e.value.code, e.value.reason)


def test_a_download_returns_the_body_with_one_lookup_and_the_name_as_host(server):
    assert dl("https://pypi.org/ok") == BODY
    assert server == ["pypi.org"] and len(Handler.hits) == 1
    assert Handler.hits[0].startswith("pypi.org:") and Handler.hits[0].endswith("/ok")  # Host = the name, not the IP


def test_a_host_is_compared_whole_and_the_url_must_be_plain_https(server):
    for url in (
        "https://pypi.org.evil.example/ok",
        "https://evilpypi.org/ok",
        "https://registry.npmjs.org/ok",  # the other ecosystem's host
        "http://pypi.org/ok",
        "https://pypi.org:8443/ok",
        "https://user@pypi.org/ok",
        "https://pypi.org/ok?x=1",
        "https://127.0.0.1/ok",
        "https://pypi.org/o k",
        "ftp://pypi.org/ok",
    ):
        refused(url, "market.env_failed", "download")
    assert server == []  # nothing was even looked up


def test_a_redirect_is_address_refused_and_is_not_followed(server):
    refused("https://pypi.org/redirect", "market.address_refused")
    assert not any("127.0.0.1" in h for h in Handler.hits)


@pytest.mark.slow
def test_status_cap_deadline_and_cut_short_bodies(server):
    refused("https://pypi.org/missing", "market.env_failed", "download")
    refused("https://pypi.org/big", "market.env_failed", "too_large")
    refused("https://pypi.org/lying-big", "market.env_failed", "too_large", cap=50_000)
    refused("https://pypi.org/short", "market.env_failed", "download")
    refused("https://pypi.org/gzip", "market.env_failed", "download")
    refused("https://pypi.org/drip", "market.env_failed", "download", secs=0.5)


def test_every_address_is_judged_and_a_rebinding_answer_refuses_all(server, monkeypatch):
    monkeypatch.setattr(download, "_check", __import__("orch.market.address", fromlist=["x"]).check_address)
    for answer in (["127.0.0.1"], ["93.184.216.34", "127.0.0.1"], ["10.0.0.5"], ["169.254.169.254"], ["::1"]):
        monkeypatch.setattr(download, "_resolve", lambda h, t, a=answer: a)
        refused("https://pypi.org/ok", "market.address_refused")
    assert Handler.hits == []  # nothing was sent anywhere


def test_a_certificate_for_another_name_is_refused(server, tmp_path, monkeypatch):
    other = ssl.create_default_context()  # system roots: the test CA is not trusted
    monkeypatch.setattr(download, "_context", lambda: other)
    refused("https://pypi.org/ok", "market.env_failed", "download")


# ---- the cache: verified before it gets its name, re-hashed on every use


@pytest.fixture
def cache(tmp_path):
    d = tmp_path / "dl"
    d.mkdir(mode=0o700)
    fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    yield download.Cache(fd, os.getuid()), d
    os.close(fd)


def src(url="https://pypi.org/ok", body=BODY, algo="sha256"):
    return download.Source(PY, url, algo, hashlib.new(algo, body).hexdigest(), "x-1.0-py3-none-any.whl")


def read(fd):
    with os.fdopen(fd, "rb") as f:
        return f.read()


def test_the_cache_verifies_before_naming_and_a_hit_is_hashed_again(server, cache):
    c, d = cache
    b = download.new_budget()
    s = src()
    assert read(c.fetch(s, time.monotonic() + 5, b)) == BODY
    assert [p.name for p in d.iterdir()] == [f"sha256-{s.digest}"] and b.bytes == download.MAX_TOTAL - len(BODY)
    n = len(Handler.hits)
    assert read(c.fetch(s, time.monotonic() + 5, b)) == BODY and len(Handler.hits) == n  # a hit: no download
    (d / f"sha256-{s.digest}").write_bytes(b"poison")  # same name, other bytes
    assert read(c.fetch(s, time.monotonic() + 5, b)) == BODY and len(Handler.hits) == n + 1  # fetched again


def test_a_hash_mismatch_leaves_nothing_in_the_cache(server, cache):
    c, d = cache
    bad = download.Source(PY, "https://pypi.org/ok", "sha256", "0" * 64, "x.whl")
    with pytest.raises(MarketError) as e:
        c.fetch(bad, time.monotonic() + 5, download.new_budget())
    assert (e.value.code, e.value.reason) == ("market.env_failed", "hash_mismatch")
    assert list(d.iterdir()) == []  # no .part file, no final name


def test_a_cache_entry_that_is_a_link_or_a_hardlink_is_not_used(server, cache, tmp_path):
    c, d = cache
    s = src()
    target = tmp_path / "elsewhere"
    target.write_bytes(BODY)  # the right bytes, but behind a symlink
    os.symlink(target, d / f"sha256-{s.digest}")
    assert read(c.fetch(s, time.monotonic() + 5, download.new_budget())) == BODY
    assert not (d / f"sha256-{s.digest}").is_symlink()


def test_the_budget_caps_the_total(server, cache):
    c, _ = cache
    b = Budget(bytes=len(BODY) + 10, entries=10)
    c.fetch(src(), time.monotonic() + 5, b)
    with pytest.raises(MarketError) as e:
        c.fetch(src(body=b"other"), time.monotonic() + 5, b)  # room is 10 bytes: the 1100-byte body is too large
    assert e.value.reason in ("too_large", "hash_mismatch")


# ---- which file of a project is downloaded

TAGS = {t: i for i, t in enumerate(__import__("packaging.tags", fromlist=["x"]).sys_tags())}


def row(name="idna", version="3.10", *hs):
    return lockfile.Row(name, version, tuple(hs))


def doc(*files):
    return {"files": [{"filename": f, "url": u, "hashes": {"sha256": h}} for f, u, h in files]}


H1, H2 = "a" * 64, "b" * 64
IDX = "https://pypi.org/simple/idna/"
U = "https://files.pythonhosted.org/packages/x/"


def pick(d, r):
    try:
        return download.choose_wheel(d, r, TAGS, IDX)
    except MarketError as e:
        return (e.code, e.reason)


def test_choose_wheel():
    w = doc(("idna-3.10-py3-none-any.whl", U + "idna.whl", H1), ("idna-3.10.tar.gz", U + "idna.tgz", H2))
    got = pick(w, row("idna", "3.10", H1, H2))
    assert (got.filename, got.url, got.digest, got.compiled) == (
        "idna-3.10-py3-none-any.whl",
        U + "idna.whl",
        H1,
        False,
    )
    # an sdist-only lock (or an index that only offers one the lock names)
    assert pick(w, row("idna", "3.10", H2)) == ("market.env_failed", "sdist_only")
    # nothing the lock names
    assert pick(w, row("idna", "3.10", "c" * 64)) == ("market.env_failed", "no_binary")
    # right hash, wrong version or name
    assert pick(w, row("idna", "3.11", H1)) == ("market.env_failed", "no_binary")
    assert pick(w, row("other", "3.10", H1)) == ("market.env_failed", "no_binary")
    # a wheel for another platform is not a candidate
    alien = doc(("idna-3.10-cp39-cp39-win_amd64.whl", U + "w.whl", H1))
    assert pick(alien, row("idna", "3.10", H1)) == ("market.env_failed", "no_binary")
    # compiled wheels are flagged, and the file URL may be relative
    py = __import__("sys").version_info
    tag = (
        f"cp{py.major}{py.minor}-abi3-" + next(t.platform for t in TAGS if t.platform != "any" and t.abi == "abi3")
        if any(t.abi == "abi3" for t in TAGS)
        else None
    )
    if tag:
        c = doc((f"idna-3.10-{tag}.whl", "../../x/w.whl", H1))
        assert pick(c, row("idna", "3.10", H1)).compiled is True
    assert pick({"files": "x"}, row("idna", "3.10", H1)) == ("market.env_failed", "download")
    assert pick({"files": [{"filename": 1}]}, row("idna", "3.10", H1)) == ("market.env_failed", "no_binary")


def test_a_file_url_on_another_host_is_refused_when_it_is_fetched(server, cache):
    c, _ = cache
    s = download.Source(PY, "https://evil.example/idna.whl", "sha256", H1, "idna.whl")
    with pytest.raises(MarketError) as e:
        c.fetch(s, time.monotonic() + 5, download.new_budget())
    assert (e.value.code, e.value.reason) == ("market.env_failed", "download") and server == []


def test_locate_python_asks_the_json_index_and_node_needs_no_request(server, monkeypatch):
    idx = json.dumps(doc(("idna-3.10-py3-none-any.whl", U + "idna.whl", H1))).encode()
    seen = []

    def fake(url, kind, cap, deadline, accept="*/*"):
        seen.append((url, kind, accept))
        return idx

    monkeypatch.setattr(download, "fetch_bytes", fake)
    s = download.locate(row("idna", "3.10", H1), PY, time.monotonic() + 5)
    assert s.url == U + "idna.whl" and seen == [("https://pypi.org/simple/idna/", PY, download.PEP691)]
    n = lockfile.Row(
        "ms", "2.1.3", ("ab" * 64,), key="node_modules/ms", resolved="https://registry.npmjs.org/ms/-/ms-2.1.3.tgz"
    )
    got = download.locate(n, "node-package-lock", time.monotonic() + 5)
    assert (got.url, got.algo, got.digest) == (n.resolved, "sha512", "ab" * 64) and len(seen) == 1
