"""Builds ``contracts/hub/vectors_link_ws.json``: the link transports of contract R6 V0 (protocol.md §10.10, §10.9;
spec ``orch-v2-hub-remote.md`` §8, D92; orch-core #1594).

Test code, deterministic, no keys. Every expected value is written here by hand (frame bytes from RFC 6455 §5.7 and
§1.3 where the RFC has one, the rest from the frame layout of RFC 6455 §5.2), never computed by the code under test;
the only helpers are a 4-byte XOR mask, SHA-1 for the accept key and SHA-256 to name long payloads.
Consumers: the hub replays every case against ``orch_hub.contract.linkws`` / ``orch_hub.contract.where``
(``packages/hub/tests/test_link_ws_vectors.py``, which also rebuilds this file byte for byte); orch-core replays every
case it implements (``packages/core/tests/hub/test_link_ws_vectors.py``) and names the hub-only ones. Both fail on an
unknown check and on a missing required case.

``cd packages/hub && uv run python ../../contracts/hub/make_vectors_link_ws.py`` rewrites the file.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

RFC_KEY = "dGhlIHNhbXBsZSBub25jZQ=="  # RFC 6455 §1.3
RFC_ACCEPT = "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="  # RFC 6455 §1.3
RFC_MASK = "37fa213d"  # RFC 6455 §5.7
HELLO = "48656c6c6f"
MASKED_HELLO = "7f9f4d5158"  # "Hello" under RFC_MASK (§5.7)
MAX_FRAME = 262144
CASES: list[dict[str, Any]] = []


def case(name: str, check: str, args: dict[str, Any], expect: Any, note: str = "") -> None:
    c = {"name": name, "check": check, "args": args, "expect": expect}
    if note:
        c["note"] = note
    CASES.append(c)


def bad(**k: Any) -> dict[str, Any]:
    return {"error": k.pop("error", "malformed"), **k}


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# --- addresses ----------------------------------------------------------------------------------------------------


def addr_ok(scheme: str, host: str, port: int, sni: str | None, host_header: str | None) -> dict[str, Any]:
    return {"ok": {"scheme": scheme, "host": host, "port": port, "sni": sni, "host_header": host_header}}


def address_cases() -> None:
    def a(name: str, text: Any, expect: dict[str, Any], note: str = "") -> None:
        case("addr_" + name, "address", {"text": text}, expect, note)

    a("direct_ipv4", "100.64.1.2:8105", addr_ok("tcp", "100.64.1.2", 8105, None, None))
    a("direct_name", "hub.example.com:443", addr_ok("tcp", "hub.example.com", 443, "hub.example.com", None))
    a(
        "direct_name_case_kept",
        "Hub.Example.COM:8105",
        addr_ok("tcp", "Hub.Example.COM", 8105, "hub.example.com", None),
        "host as written; SNI in lower case",
    )
    a("direct_ipv6", "[::1]:8105", addr_ok("tcp", "::1", 8105, None, None))
    a("direct_single_label", "localhost:8105", addr_ok("tcp", "localhost", 8105, "localhost", None))
    a("direct_port_max", "h:65535", addr_ok("tcp", "h", 65535, "h", None))
    a(
        "wss_default_port",
        "wss://hub-link.example.com",
        addr_ok("wss", "hub-link.example.com", 443, "hub-link.example.com", "hub-link.example.com"),
    )
    a(
        "wss_explicit_default_port",
        "wss://hub-link.example.com:443",
        addr_ok("wss", "hub-link.example.com", 443, "hub-link.example.com", "hub-link.example.com"),
        "the Host header leaves out the default port",
    )
    a(
        "wss_other_port",
        "wss://hub-link.example.com:8443",
        addr_ok("wss", "hub-link.example.com", 8443, "hub-link.example.com", "hub-link.example.com:8443"),
    )
    a(
        "ws_ipv4_port",
        "ws://10.0.0.5:8106",
        addr_ok("ws", "10.0.0.5", 8106, None, "10.0.0.5:8106"),
        "an IP literal gets no SNI",
    )
    a("ws_default_port", "ws://10.0.0.5", addr_ok("ws", "10.0.0.5", 80, None, "10.0.0.5"))
    a("ws_name_port_80", "ws://hub.lan:80", addr_ok("ws", "hub.lan", 80, "hub.lan", "hub.lan"))
    a("wss_ipv6_port", "wss://[2001:db8::1]:8443", addr_ok("wss", "2001:db8::1", 8443, None, "[2001:db8::1]:8443"))
    a(
        "wss_ipv4_mapped_v6",
        "wss://[::ffff:192.0.2.1]",
        addr_ok("wss", "::ffff:192.0.2.1", 443, None, "[::ffff:192.0.2.1]"),
    )
    a(
        "wss_punycode",
        "wss://xn--bcher-kva.example",
        addr_ok("wss", "xn--bcher-kva.example", 443, "xn--bcher-kva.example", "xn--bcher-kva.example"),
        "an internationalised name only in its xn-- form",
    )
    name253 = ".".join(["a" * 63, "b" * 63, "c" * 63, "d" * 61])
    assert len(name253) == 253
    a("wss_host_253", "wss://" + name253, addr_ok("wss", name253, 443, name253, name253))
    a(
        "wss_label_63",
        "wss://" + "a" * 63 + ".example",
        addr_ok("wss", "a" * 63 + ".example", 443, "a" * 63 + ".example", "a" * 63 + ".example"),
    )

    for nm, t, note in (
        ("not_a_string", 8105, ""), ("null", None, ""), ("empty", "", ""),
        ("direct_no_port", "10.0.0.1", "direct needs a port"), ("direct_empty_port", "10.0.0.1:", ""),
        ("direct_empty_host", ":8105", ""), ("direct_port_zero", "10.0.0.1:0", ""),
        ("direct_port_too_big", "10.0.0.1:65536", ""), ("direct_port_leading_zero", "10.0.0.1:08105", ""),
        ("direct_two_ports", "h:1:2", ""), ("direct_path", "h:1/x", ""), ("direct_userinfo", "u@h:1", ""),
        ("wss_port_zero", "wss://h:0", ""), ("wss_port_too_big", "wss://h:65536", ""),
        ("wss_port_leading_zero", "wss://h:0443", ""), ("wss_empty_port", "wss://h:", ""),
        ("ws_two_ports", "ws://h:80:80", ""),
        ("scheme_http", "http://h:80", "only ws and wss"), ("scheme_https", "https://h", ""),
        ("scheme_tcp", "tcp://h:1", ""), ("scheme_tls", "tls://h:1", ""), ("scheme_wss_unix", "wss+unix://x", ""),
        ("scheme_upper", "WSS://h", "the scheme is lower case"), ("scheme_mixed_case", "Wss://h", ""),
        ("scheme_one_slash", "ws:/h", ""), ("scheme_no_slash", "ws:h:80", ""), ("scheme_three_slashes", "wss:///h", ""),
        ("scheme_only", "wss://", ""), ("scheme_empty_host_port", "wss://:443", ""),
        ("path_slash", "wss://h/", "the path is fixed (/orch/link/1); none in the address"),
        ("path_link", "wss://h/orch/link/1", ""), ("path_after_port", "wss://h:443/x", ""),
        ("query", "wss://h?x=1", ""), ("query_empty", "wss://h:443?", ""),
        ("fragment", "wss://h#f", ""), ("fragment_empty", "wss://h#", ""),
        ("userinfo_user", "wss://user@h", "user info is refused, never stripped"),
        ("userinfo_user_password", "wss://user:pw@h:443", ""), ("userinfo_empty", "wss://@h", ""),
        ("percent_escape", "wss://h%2Eexample.com", ""), ("percent_only", "wss://%68", ""),
        ("leading_space", " wss://h", ""), ("trailing_space", "wss://h ", ""), ("tab", "wss://h\t", ""),
        ("newline", "wss://h\n", ""), ("space_in_host", "wss:// h", ""), ("backslash", "wss://h\\x", ""),
        ("comma", "wss://h,i", ""), ("semicolon", "wss://h;x", ""),
        ("idn_unicode", "wss://bücher.example", "IDN only as xn--"),
        ("idn_unicode_direct", "bücher.example:443", ""), ("fullwidth_letter", "wss://ｈub.example", ""),
        ("ideographic_full_stop", "wss://hub.example。com", ""),
        ("label_leading_dash", "wss://-h.example", ""), ("label_trailing_dash", "wss://h-.example", ""),
        ("empty_label", "wss://h..example", ""), ("leading_dot", "wss://.h", ""), ("trailing_dot", "wss://h.", ""),
        ("underscore", "wss://h_x.example", ""), ("label_64", "wss://" + "a" * 64 + ".example", ""),
        ("host_254", "wss://" + "a" * 63 + "." + "b" * 63 + "." + "c" * 63 + "." + "d" * 62, ""),
        ("ipv4_octet_too_big", "wss://999.1.1.1", ""),
        ("ipv4_three_parts", "wss://1.2.3", "a numeric last label must be a whole dotted-quad address"),
        ("ipv4_two_parts", "wss://127.1", ""), ("ipv4_leading_zero", "wss://010.0.0.1", ""),
        ("ipv4_hex_part", "wss://0x7f.0.0.1", ""), ("ipv4_integer", "wss://2130706433", ""),
        ("ipv4_five_parts", "1.2.3.4.5:1", ""),
        ("ipv6_unbracketed", "wss://::1", ""), ("ipv6_unclosed", "wss://[::1", ""), ("ipv6_unopened", "wss://::1]", ""),
        ("ipv6_zone", "wss://[fe80::1%25en0]", ""), ("ipv6_two_gaps", "wss://[1::2::3]", ""),
        ("ipv6_bad_digit", "wss://[g::1]", ""), ("ipv6_empty", "wss://[]", ""),
        ("ipv6_trailing_junk", "wss://[::1]x", ""),
        ("too_long", "wss://" + "a" * 295, "over 300 characters"),
    ):  # fmt: skip
        a(nm, t, bad(), note)


def connect_cases() -> None:
    def c(name: str, ep: Any, expect: dict[str, Any], note: str = "") -> None:
        case("connect_" + name, "connect_endpoint", {"endpoint": ep}, expect, note)

    c("local", "local", {"ok": "local"})
    c("direct", "100.64.1.2:8105", {"ok": "100.64.1.2:8105"})
    c("wss", "wss://hub-link.example.com", {"ok": "wss://hub-link.example.com"})
    c("ws_port", "ws://10.0.0.5:8106", {"ok": "ws://10.0.0.5:8106"})
    c("local_upper", "LOCAL", bad())
    c("proxy_url", "http://proxy.example:3128", bad(), "a connect link never carries a proxy")
    c("wss_path", "wss://hub-link.example.com/orch/link/1", bad())
    c("wss_tilde", "wss://h~x", bad())
    c("empty", "", bad())


def where_cases() -> None:
    def w(name: str, ans: Any, expect: dict[str, Any], note: str = "") -> None:
        case("where_" + name, "hub_where", {"answer": ans}, expect, note)

    def e(kind: str, link: str) -> dict[str, str]:
        return {"kind": kind, "link": link}

    ok = {"addresses": [e("private", "100.64.1.2:8105"), e("public", "wss://hub-link.example.com")], "name": "Demo hub"}
    w("ws_mixed_ok", ok, {"ok": ok})
    ok2 = {"addresses": [e("office", "ws://192.168.1.20:8106")], "name": "H"}
    w("ws_plain_ok", ok2, {"ok": ok2})
    ok3 = {"addresses": [e("public", "hub.example.com:443"), e("public", "wss://hub.example.com")], "name": "H"}
    w("direct_and_wss_same_host_port_ok", ok3, {"ok": ok3}, "different transports are different addresses")
    w(
        "ws_dup_default_port",
        {"addresses": [e("public", "wss://h.example"), e("public", "wss://h.example:443")], "name": "H"},
        bad(),
        "same scheme, host and port",
    )
    w(
        "ws_dup_host_case",
        {"addresses": [e("public", "wss://H.example"), e("office", "wss://h.example")], "name": "H"},
        bad(),
    )
    w(
        "direct_dup_host_case",
        {"addresses": [e("public", "H.example:1"), e("office", "h.example:1")], "name": "H"},
        bad(),
    )
    w("ws_path", {"addresses": [e("public", "wss://h.example/orch/link/1")], "name": "H"}, bad())
    w("ws_userinfo", {"addresses": [e("public", "wss://u@h.example")], "name": "H"}, bad())
    w(
        "proxy_as_link",
        {"addresses": [e("public", "http://proxy.example:3128")], "name": "H"},
        bad(),
        "the hub never sends proxy settings",
    )
    w(
        "proxy_key_in_entry",
        {"addresses": [{"kind": "public", "link": "wss://h.example", "proxy": "http://p:3128"}], "name": "H"},
        bad(),
    )
    w(
        "proxy_key_in_answer",
        {"addresses": [e("public", "wss://h.example")], "name": "H", "proxy": "http://p:3128"},
        bad(),
    )
    w(
        "transport_key_in_entry",
        {"addresses": [{"kind": "public", "link": "h.example:1", "transport": "ws"}], "name": "H"},
        bad(),
    )


# --- frames -------------------------------------------------------------------------------------------------------


def xor(data: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % 4] for i, b in enumerate(data))


def enc_ok(h: str) -> dict[str, Any]:
    return {"ok": {"hex": h}}


def frame_cases() -> None:
    def enc(name: str, opcode: int, payload: str, expect: dict[str, Any], *, mask: str | None = None,
            fin: bool = True, fill: dict[str, Any] | None = None, note: str = "") -> None:  # fmt: skip
        args: dict[str, Any] = {"opcode": opcode, "payload_hex": payload, "mask_hex": mask, "fin": fin}
        if fill:
            args["fill"] = fill
        case("enc_" + name, "frame_encode", args, expect, note)

    enc("binary_empty", 2, "", enc_ok("8200"))
    enc("binary_small", 2, "0102030405", enc_ok("82050102030405"))
    enc(
        "binary_masked_rfc",
        2,
        HELLO,
        enc_ok("8285" + RFC_MASK + MASKED_HELLO),
        mask=RFC_MASK,
        note="RFC 6455 §5.7 masked 'Hello' with the binary opcode",
    )
    enc("binary_125", 2, "00" * 125, enc_ok("827d" + "00" * 125), note="largest 7-bit length")
    enc("binary_126", 2, "00" * 126, enc_ok("827e007e" + "00" * 126), note="smallest 16-bit length")
    enc(
        "binary_65535",
        2,
        "",
        {"ok": {"head_hex": "827effff", "sha256": sha(bytes.fromhex("827effff") + b"\0" * 65535)}},
        fill={"byte": "00", "n": 65535},
        note="largest 16-bit length",
    )
    h64 = "827f0000000000010000"
    enc(
        "binary_65536",
        2,
        "",
        {"ok": {"head_hex": h64, "sha256": sha(bytes.fromhex(h64) + b"\0" * 65536)}},
        fill={"byte": "00", "n": 65536},
        note="smallest 64-bit length",
    )
    k = "01020304"
    p = bytes(range(256))
    enc("binary_256_masked", 2, p.hex(), enc_ok("82fe0100" + k + xor(p, bytes.fromhex(k)).hex()), mask=k)
    enc("ping_rfc", 9, HELLO, enc_ok("8905" + HELLO), note="RFC 6455 §5.7 unmasked ping")
    enc(
        "pong_masked_rfc",
        10,
        HELLO,
        enc_ok("8a85" + RFC_MASK + MASKED_HELLO),
        mask=RFC_MASK,
        note="RFC 6455 §5.7 masked pong",
    )
    enc("close_1000", 8, "03e8", enc_ok("880203e8"))
    enc("close_1000_masked", 8, "03e8", enc_ok("8882" + "00000000" + "03e8"), mask="00000000")
    enc("text_refused", 1, HELLO, bad(error="refused"), note="a sender never sends text")
    enc("ping_126_refused", 9, "00" * 126, bad(error="refused"))
    enc("ping_unfinished_refused", 9, "", bad(error="refused"), fin=False)
    enc("mask_3_bytes_refused", 2, "00", bad(error="refused"), mask="010203")

    def dec(name: str, sender: str, h: str, expect: Any, *, fill: dict[str, Any] | None = None, note: str = "") -> None:
        args: dict[str, Any] = {"sender": sender, "hex": h}
        if fill:
            args["fill"] = fill
        case("dec_" + name, "frame_decode", args, expect, note)

    def ok(fin: bool, op: int, payload: str, used: int) -> dict[str, Any]:
        return {"ok": {"fin": fin, "opcode": op, "payload_hex": payload, "used": used}}

    def ws(code: int, reason: str) -> dict[str, Any]:
        return {"error": "ws", "close": code, "reason": reason}

    inc = {"ok": "incomplete"}
    dec("server_binary", "server", "82050102030405", ok(True, 2, "0102030405", 7))
    dec("client_binary_masked_rfc", "client", "8285" + RFC_MASK + MASKED_HELLO, ok(True, 2, HELLO, 11))
    dec(
        "server_masked_refused",
        "server",
        "8285" + RFC_MASK + MASKED_HELLO,
        ws(1002, "mask"),
        note="a server frame is never masked",
    )
    dec("client_unmasked_refused", "client", "82050102030405", ws(1002, "mask"), note="a client frame is always masked")
    dec("text_rfc_unmasked", "server", "8105" + HELLO, ws(1003, "text"), note="RFC 6455 §5.7 text 'Hello'")
    dec("text_rfc_masked", "client", "8185" + RFC_MASK + MASKED_HELLO, ws(1003, "text"))
    dec(
        "compressed_rfc7692",
        "server",
        "c107f248cdc9c90700",
        ws(1002, "rsv"),
        note="RFC 7692 §7.2.3.1 compressed 'Hello': RSV1 (no extension is ever negotiated)",
    )
    dec("rsv1_binary", "server", "c2050102030405", ws(1002, "rsv"))
    dec("rsv2", "server", "a20100", ws(1002, "rsv"))
    dec("rsv3", "server", "920100", ws(1002, "rsv"))
    dec("opcode_3", "server", "8300", ws(1002, "opcode"))
    dec("opcode_b", "server", "8b00", ws(1002, "opcode"))
    dec("ping_rfc", "server", "8905" + HELLO, ok(True, 9, HELLO, 7))
    dec("pong_masked_rfc", "client", "8a85" + RFC_MASK + MASKED_HELLO, ok(True, 10, HELLO, 11))
    dec("ping_fragmented", "server", "0900", ws(1002, "control"))
    dec("ping_126_from_header", "server", "897e", ws(1002, "control"), note="decided from the first two bytes")
    dec("close_127_from_header", "server", "887f", ws(1002, "control"))
    dec("close_empty", "server", "8800", ok(True, 8, "", 2))
    dec("close_1000", "server", "880203e8", ok(True, 8, "03e8", 4))
    dec("close_1000_reason", "server", "880503e8627965", ok(True, 8, "03e8627965", 7))
    dec(
        "close_1000_masked",
        "client",
        "8882" + "0a0b0c0d" + xor(bytes.fromhex("03e8"), bytes.fromhex("0a0b0c0d")).hex(),
        ok(True, 8, "03e8", 8),
    )
    dec("close_one_byte", "server", "880103", ws(1002, "close_code"))
    for code_name, code_hex, good in (
        ("1001", "03e9", True), ("1014", "03f6", True), ("3000", "0bb8", True), ("4999", "1387", True),
        ("999", "03e7", False), ("1004", "03ec", False), ("1005", "03ed", False), ("1006", "03ee", False),
        ("1015", "03f7", False), ("1016", "03f8", False), ("2999", "0bb7", False), ("5000", "1388", False),
    ):  # fmt: skip
        dec(
            "close_code_" + code_name,
            "server",
            "8802" + code_hex,
            ok(True, 8, code_hex, 4) if good else ws(1002, "close_code"),
        )
    dec("close_reason_not_utf8", "server", "880403e8c328", ws(1007, "close_reason"))
    dec(
        "len16_short_form",
        "server",
        "827e0005" + "00" * 5,
        ws(1002, "length"),
        note="a length that fits 7 bits in the 16-bit form",
    )
    dec(
        "len64_short_form",
        "server",
        "827f000000000000ffff",
        ws(1002, "length"),
        note="a length that fits 16 bits in the 64-bit form; decided before any payload",
    )
    dec("len64_top_bit", "server", "827f8000000000000000", ws(1002, "length"))
    dec(
        "oversize_from_header",
        "server",
        "827f0000000000040001",
        ws(1009, "too_big"),
        note="256 KiB + 1: refused from the header alone, no payload sent",
    )
    dec("oversize_from_header_masked", "client", "82ff0000000000040001", ws(1009, "too_big"))
    dec("oversize_huge", "server", "827f7fffffffffffffff", ws(1009, "too_big"))
    big = b"\0" * MAX_FRAME
    dec(
        "max_frame_ok",
        "server",
        "827f0000000000040000",
        {
            "ok": {
                "fin": True,
                "opcode": 2,
                "payload_len": MAX_FRAME,
                "payload_sha256": sha(big),
                "used": 10 + MAX_FRAME,
            }
        },
        fill={"byte": "00", "n": MAX_FRAME},
        note="exactly 256 KiB",
    )
    p256 = bytes(range(256))
    dec("len16_256", "server", "827e0100" + p256.hex(), ok(True, 2, p256.hex(), 260))
    dec("incomplete_one_byte", "server", "82", inc)
    dec("incomplete_len16", "server", "827e01", inc)
    dec("incomplete_len64", "server", "827f00000000", inc)
    dec("incomplete_payload", "server", "820501", inc)
    dec("incomplete_mask", "client", "8285" + RFC_MASK[:4], inc)
    dec("incomplete_empty", "server", "", inc)
    dec("only_first_frame", "server", "8201aa8200", ok(True, 2, "aa", 3), note="the rest stays in the buffer")
    dec("binary_unfinished", "server", "0202aabb", ok(False, 2, "aabb", 4))
    dec("continuation_final", "server", "8001cc", ok(True, 0, "cc", 3))


def stream_cases() -> None:
    def st(name: str, sender: str, chunks: list[str], expect: Any, note: str = "") -> None:
        case("stream_" + name, "frame_stream", {"sender": sender, "chunks_hex": chunks}, expect, note)

    def ok(data: str, answers: list[list[Any]] | None = None, ended: bool = False, closed: int | None = None) -> Any:
        return {"ok": {"data_hex": data, "answers": answers or [], "ended": ended, "closed": closed}}

    def ws(code: int, reason: str) -> dict[str, Any]:
        return {"error": "ws", "close": code, "reason": reason}

    st("two_frames", "server", ["8202aabb", "8201cc"], ok("aabbcc"), "frame boundaries carry no meaning")
    st("split_reads", "server", ["82", "02aa", "bb"], ok("aabb"))
    st("fragmented", "server", ["0202aabb", "8001cc"], ok("aabbcc"), "intermediaries may re-fragment")
    st("fragmented_three", "server", ["0201aa", "0001bb", "8001cc"], ok("aabbcc"))
    st("ping_inside_fragmented", "server", ["0201aa", "890101", "8001bb"], ok("aabb", [[10, "01"]]))
    st("continuation_without_start", "server", ["8001aa"], ws(1002, "continuation"))
    st("binary_inside_fragmented", "server", ["0201aa", "8201bb"], ws(1002, "interleaved"))
    st("pong_ignored", "server", ["8a00", "8201aa"], ok("aa"), "a pong is liveness only")
    st(
        "ping_answered",
        "server",
        ["8905" + HELLO],
        ok("", [[10, HELLO]]),
        "a ping alone yields no link bytes (wait_readable stays False)",
    )
    st("ping_empty_answered", "server", ["8900"], ok("", [[10, ""]]))
    st(
        "close_ends",
        "server",
        ["8201aa", "880203e8", "8201bb"],
        ok("aa", [[8, "03e8"]], True, 1000),
        "bytes after the close are ignored; the answer is a close 1000",
    )
    st("close_empty", "server", ["8800"], ok("", [[8, "03e8"]], True, None))
    st("close_1001_answered_1000", "server", ["880203e9"], ok("", [[8, "03e8"]], True, 1001))
    st("text_after_data", "server", ["8201aa", "8100"], ws(1003, "text"))
    st("compressed", "server", ["c107f248cdc9c90700"], ws(1002, "rsv"))
    st("oversize", "server", ["827f0000000000040001"], ws(1009, "too_big"))
    st("client_masked", "client", ["8285" + RFC_MASK + MASKED_HELLO, "8a80" + "01020304"], ok(HELLO))
    st("client_unmasked_in_stream", "client", ["8285" + RFC_MASK + MASKED_HELLO, "8200"], ws(1002, "mask"))


# --- the upgrade --------------------------------------------------------------------------------------------------

REQ = (
    "GET /orch/link/1 HTTP/1.1\r\nHost: hub-link.example.com\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
    f"Sec-WebSocket-Key: {RFC_KEY}\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: orch-link.1\r\n\r\n"
)
RESP = (
    "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
    f"Sec-WebSocket-Accept: {RFC_ACCEPT}\r\nSec-WebSocket-Protocol: orch-link.1\r\n\r\n"
)


def handshake_cases() -> None:
    zero_key = base64.b64encode(b"\0" * 16).decode()
    case("accept_rfc", "accept_key", {"key": RFC_KEY}, {"ok": RFC_ACCEPT}, "RFC 6455 §1.3")
    zero_accept = base64.b64encode(
        hashlib.sha1((zero_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
    ).decode()
    case("accept_zero_key", "accept_key", {"key": zero_key}, {"ok": zero_accept})

    def breq(name: str, address: str, head: str) -> None:
        case("build_request_" + name, "build_request", {"address": address, "key": RFC_KEY}, {"ok": head})

    breq("wss_default", "wss://hub-link.example.com", REQ)
    breq("wss_explicit_443", "wss://hub-link.example.com:443", REQ)
    breq("ws_ip_port", "ws://10.0.0.5:8106", REQ.replace("hub-link.example.com", "10.0.0.5:8106"))
    breq("wss_v6_port", "wss://[2001:db8::1]:8443", REQ.replace("hub-link.example.com", "[2001:db8::1]:8443"))
    case("build_response_rfc", "build_response", {"key": RFC_KEY}, {"ok": RESP})
    for status, text, extra in ((400, "Bad Request", ""), (403, "Forbidden", ""), (404, "Not Found", ""),
                                (426, "Upgrade Required", "Sec-WebSocket-Version: 13\r\n")):  # fmt: skip
        case(
            f"build_refusal_{status}",
            "build_refusal",
            {"status": status},
            {"ok": f"HTTP/1.1 {status} {text}\r\n{extra}Content-Length: 0\r\nConnection: close\r\n\r\n"},
        )

    def rq(name: str, head: str, expect: Any, note: str = "") -> None:
        case("req_" + name, "check_request", {"head": head}, expect, note)

    def ref(status: int, reason: str) -> dict[str, Any]:
        return {"error": "refused", "status": status, "reason": reason}

    def sub(old: str, new: str) -> str:
        assert old in REQ
        return REQ.replace(old, new)

    def add(line: str) -> str:
        return REQ[:-2] + line + "\r\n\r\n"

    rq("ok", REQ, {"ok": RFC_KEY})
    rq(
        "ok_front_headers",
        sub("Connection: Upgrade", "Connection: keep-alive, Upgrade")[:-2]
        + "X-Forwarded-For: 203.0.113.9\r\nX-Forwarded-Proto: https\r\nX-Real-IP: 203.0.113.9\r\n\r\n",
        {"ok": RFC_KEY},
        "forwarding headers are ignored and never used as the peer address",
    )
    rq("ok_upgrade_case", sub("Upgrade: websocket", "Upgrade: WebSocket"), {"ok": RFC_KEY})
    rq("ok_header_name_case", sub("Sec-WebSocket-Key", "sec-websocket-key"), {"ok": RFC_KEY})
    rq("ok_content_length_zero", add("Content-Length: 0"), {"ok": RFC_KEY})
    rq("ok_cookie_ignored", add("Cookie: a=b"), {"ok": RFC_KEY})
    pad = 8192 - len(REQ) - len("X-Pad: \r\n")
    rq("ok_exactly_8192", add("X-Pad: " + "a" * pad), {"ok": RFC_KEY}, "the head limit counts CRLF CRLF")
    rq("too_large", add("X-Pad: " + "a" * (pad + 1)), ref(400, "too_large"))
    for nm, line in (("path_other_version", "GET /orch/link/2 HTTP/1.1"), ("path_root", "GET / HTTP/1.1"),
                     ("path_trailing_slash", "GET /orch/link/1/ HTTP/1.1"),
                     ("path_query", "GET /orch/link/1?x=1 HTTP/1.1"), ("path_card", "GET /api/v1/hub/card HTTP/1.1"),
                     ("path_absolute_form", "GET http://hub-link.example.com/orch/link/1 HTTP/1.1"),
                     ("method_post", "POST /orch/link/1 HTTP/1.1"), ("method_head", "HEAD /orch/link/1 HTTP/1.1"),
                     ("method_lower", "get /orch/link/1 HTTP/1.1"),
                     ("double_space", "GET  /orch/link/1 HTTP/1.1")):  # fmt: skip
        rq(nm, sub("GET /orch/link/1 HTTP/1.1", line), ref(404, "not_found"))
    rq("http10", sub("HTTP/1.1", "HTTP/1.0"), ref(400, "version"))
    rq("origin", add("Origin: https://evil.example"), ref(403, "origin"), "a browser never reaches the link")
    rq("origin_null", add("Origin: null"), ref(403, "origin"))
    rq("origin_empty", add("Origin:"), ref(403, "origin"))
    rq("origin_lower", add("origin: https://evil.example"), ref(403, "origin"))
    rq("origin_old_draft", add("Sec-WebSocket-Origin: https://evil.example"), ref(403, "origin"))
    rq("no_host", sub("Host: hub-link.example.com\r\n", ""), ref(400, "host"))
    rq("dup_host", add("Host: other.example"), ref(400, "malformed"))
    rq("no_upgrade", sub("Upgrade: websocket\r\n", ""), ref(400, "upgrade"))
    rq("upgrade_h2c", sub("Upgrade: websocket", "Upgrade: h2c"), ref(400, "upgrade"))
    rq("connection_keep_alive", sub("Connection: Upgrade", "Connection: keep-alive"), ref(400, "upgrade"))
    rq("version_8", sub("Sec-WebSocket-Version: 13", "Sec-WebSocket-Version: 8"), ref(426, "version"))
    rq("version_missing", sub("Sec-WebSocket-Version: 13\r\n", ""), ref(426, "version"))
    rq("key_missing", sub(f"Sec-WebSocket-Key: {RFC_KEY}\r\n", ""), ref(400, "key"))
    rq("key_15_bytes", sub(RFC_KEY, base64.b64encode(b"x" * 15).decode()), ref(400, "key"))
    rq(
        "key_non_canonical",
        sub(RFC_KEY, "dGhlIHNhbXBsZSBub25jZR=="),
        ref(400, "key"),
        "padding bits set: not the canonical form",
    )
    rq("key_not_base64", sub(RFC_KEY, "!GhlIHNhbXBsZSBub25jZQ=="), ref(400, "key"))
    rq(
        "extensions",
        add("Sec-WebSocket-Extensions: permessage-deflate; client_max_window_bits"),
        ref(400, "extensions"),
        "no extension is ever negotiated",
    )
    rq("extensions_empty", add("Sec-WebSocket-Extensions:"), ref(400, "extensions"))
    rq("subprotocol_missing", sub("Sec-WebSocket-Protocol: orch-link.1\r\n", ""), ref(400, "subprotocol"))
    rq("subprotocol_other", sub("orch-link.1", "orch-link.2"), ref(400, "subprotocol"))
    rq("subprotocol_list", sub("orch-link.1", "orch-link.1, chat"), ref(400, "subprotocol"))
    rq("subprotocol_dup", add("Sec-WebSocket-Protocol: orch-link.1"), ref(400, "malformed"))
    rq("body_chunked", add("Transfer-Encoding: chunked"), ref(400, "body"))
    rq("body_length", add("Content-Length: 5"), ref(400, "body"))
    rq("bare_lf", sub("Host: hub-link.example.com\r\n", "Host: hub-link.example.com\n"), ref(400, "malformed"))
    rq("folded_line", sub("Upgrade: websocket\r\n", "Upgrade:\r\n websocket\r\n"), ref(400, "malformed"))
    rq("space_before_colon", sub("Upgrade: websocket", "Upgrade : websocket"), ref(400, "malformed"))
    rq("non_ascii", sub("hub-link.example.com", "hüb.example.com"), ref(400, "malformed"))
    rq("no_final_crlf", REQ[:-2], ref(400, "malformed"))

    def rs(name: str, head: str, expect: Any, note: str = "") -> None:
        case("resp_" + name, "check_response", {"head": head, "key": RFC_KEY}, expect, note)

    def fail(reason: str) -> dict[str, Any]:
        return {"error": "failed", "reason": reason}

    def rsub(old: str, new: str) -> str:
        assert old in RESP
        return RESP.replace(old, new)

    def radd(line: str) -> str:
        return RESP[:-2] + line + "\r\n\r\n"

    rs("ok", RESP, {"ok": None})
    rs("ok_extra_headers", radd("Date: Fri, 30 Oct 2026 10:00:00 GMT\r\nServer: front"), {"ok": None})
    rs("ok_no_reason_phrase", rsub("101 Switching Protocols", "101"), {"ok": None})
    rs("ok_connection_list", rsub("Connection: Upgrade", "Connection: keep-alive, upgrade"), {"ok": None})
    for st in (301, 302, 307, 308):
        rs(
            f"redirect_{st}",
            f"HTTP/1.1 {st} Moved\r\nLocation: wss://elsewhere.example/orch/link/1\r\n\r\n",
            fail("redirect"),
            "never followed",
        )
    for st in (200, 400, 401, 403, 404, 407, 426, 502):
        rs(f"status_{st}", f"HTTP/1.1 {st} No\r\nContent-Length: 0\r\n\r\n", fail("status"))
    rs("http10", rsub("HTTP/1.1", "HTTP/1.0"), fail("malformed"))
    rs("status_two_digits", rsub("101 Switching Protocols", "10 x"), fail("malformed"))
    rs("upgrade_missing", rsub("Upgrade: websocket\r\n", ""), fail("upgrade"))
    rs("connection_missing", rsub("Connection: Upgrade\r\n", ""), fail("upgrade"))
    rs("accept_wrong", rsub(RFC_ACCEPT, zero_accept), fail("accept"), "the accept of another key")
    rs("accept_missing", rsub(f"Sec-WebSocket-Accept: {RFC_ACCEPT}\r\n", ""), fail("accept"))
    rs("accept_dup", radd(f"Sec-WebSocket-Accept: {RFC_ACCEPT}"), fail("malformed"))
    rs("extensions", radd("Sec-WebSocket-Extensions: permessage-deflate"), fail("extensions"))
    rs("subprotocol_missing", rsub("Sec-WebSocket-Protocol: orch-link.1\r\n", ""), fail("subprotocol"))
    rs("subprotocol_other", rsub("orch-link.1", "chat"), fail("subprotocol"))
    rs("too_large", radd("X-Pad: " + "a" * 8192), fail("too_large"))
    rs("bare_lf", rsub("Upgrade: websocket\r\n", "Upgrade: websocket\n"), fail("malformed"))


REQUIRED = [
    "addr_wss_default_port", "addr_ws_ipv4_port", "addr_direct_ipv4", "addr_userinfo_user", "addr_fragment",
    "addr_path_link", "addr_scheme_http", "addr_scheme_upper", "addr_idn_unicode", "addr_ipv4_integer",
    "addr_ipv6_zone", "addr_percent_escape", "addr_too_long",
    "connect_wss", "connect_proxy_url",
    "where_ws_mixed_ok", "where_proxy_key_in_entry", "where_proxy_key_in_answer", "where_proxy_as_link",
    "where_ws_dup_default_port",
    "enc_binary_masked_rfc", "enc_binary_65536", "enc_text_refused",
    "dec_server_masked_refused", "dec_client_unmasked_refused", "dec_text_rfc_unmasked", "dec_compressed_rfc7692",
    "dec_oversize_from_header", "dec_max_frame_ok", "dec_len64_short_form", "dec_ping_fragmented",
    "dec_close_code_1005", "dec_close_reason_not_utf8",
    "stream_fragmented", "stream_ping_answered", "stream_continuation_without_start", "stream_close_ends",
    "accept_rfc", "build_request_wss_default", "build_response_rfc",
    "req_ok", "req_origin", "req_origin_null", "req_extensions", "req_subprotocol_missing", "req_path_query",
    "req_ok_front_headers", "req_too_large",
    "resp_ok", "resp_redirect_302", "resp_extensions", "resp_subprotocol_missing", "resp_accept_wrong",
]  # fmt: skip


def build() -> dict[str, Any]:
    CASES.clear()
    address_cases()
    connect_cases()
    where_cases()
    frame_cases()
    stream_cases()
    handshake_cases()
    names = [c["name"] for c in CASES]
    assert len(names) == len(set(names)), "duplicate case name"
    assert set(REQUIRED) <= set(names)
    return {
        "contract": "orch.hub-link-ws/0",
        "note": "R6 V0 (#1594): link address grammar, the WebSocket profile of the link and its upgrade. No keys. "
        "The hub replays every case; orch-core every case except check_request, build_response and build_refusal.",
        "constants": {
            "path": "/orch/link/1",
            "subprotocol": "orch-link.1",
            "ws_version": 13,
            "ws_guid": "258EAFA5-E914-47DA-95CA-C5AB0DC85B11",
            "max_address": 300,
            "default_ports": {"ws": 80, "wss": 443},
            "max_frame_payload": MAX_FRAME,
            "max_send_chunk": 65536,
            "max_control_payload": 125,
            "max_head": 8192,
            "ping_after_s": 25,
            "idle_close_s": 75,
            "close_wait_s": 1,
        },
        "cases": CASES[:],
        "required": REQUIRED,
    }


if __name__ == "__main__":
    out = Path(__file__).with_name("vectors_link_ws.json")
    out.write_text(json.dumps(build(), indent=1, ensure_ascii=True) + "\n")
    print(f"wrote {out} ({len(build()['cases'])} cases)")
