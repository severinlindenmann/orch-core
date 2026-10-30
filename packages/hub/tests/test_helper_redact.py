"""HUB-SV-03 piece e: what the helper lets out of the logs. Adversarial: nothing secret may survive."""

import pytest

from orch_hub.services import helper_backends as B
from orch_hub.services.helper_proto import HelperError
from orch_hub.services.redact import MAX_LINE, MAX_LINES, R, read_logs, redact

SECRETS = [
    "Authorization: Bearer abcDEF1234567890abcDEF1234567890",
    "GET /x Authorization: Basic dXNlcjpwYXNzd29yZA==",
    "Cookie: orch_session=abcdef; other=1",
    "Set-Cookie: sid=zzzzzzzzzz; HttpOnly",
    "login ok password=hunter2 user=ann",
    'body {"password": "hunter2", "totp": "123456"}',
    "api_key: sk-live-0123456789",
    "token=ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "passphrase = 'correct horse battery'",
    "connect https://user:s3cr3tpw@relay.example.com/path",
    "fetch https://relay.example.com/x?invite=SECRETVALUE&a=b",
    "sig=MEUCIQDabcdefghijklmnopqrstuvwxyz0123456789ABCDEF",
    "card_seq 3 cert eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJ4In0.c2ln",
    "key 0123456789abcdef0123456789abcdef0123456789abcdef",
    '{"proof":"AAAA","digest":"BBBB"}',
    "nonce: 00112233",
]
LEAKS = [
    "hunter2",
    "123456",
    "s3cr3tpw",
    "SECRETVALUE",
    "ghp_",
    "sk-live",
    "correct horse",
    "MEUCI",
    "eyJhbGci",
    "dXNlcjpw",
    "0123456789abcdef0123",
    "abcdef;",
]


def test_known_secret_shapes_are_gone():
    out = redact("\n".join(SECRETS))
    for leak in LEAKS:
        assert leak not in out, leak
    assert R in out


def test_pem_block_even_unterminated_or_split_over_lines():
    pem = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkq\nhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----"
    assert "MIIE" not in redact("before\n" + pem + "\nafter") and "after" in redact("before\n" + pem + "\nafter")
    assert "MIIE" not in redact("x\n-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkq\nmore")


def test_control_characters_ansi_and_bidi_removed():
    out = redact(
        "\x1b[31mred\x1b[0m ok\x07 \x1b]0;title\x07done "
        + chr(0x202E)
        + "evil"
        + chr(0x202C)
        + " \x00nul zero"
        + chr(0x200B)
        + "width"
    )
    assert out == "red ok done evil nul zerowidth"
    assert "\x1b" not in out


def test_html_stays_text_not_interpreted():
    assert redact("<script>alert(1)</script>") == "<script>alert(1)</script>"  # escaped by the console (textContent)


def test_line_and_total_caps_keep_the_newest():
    long = "x " * 400
    assert len(redact(long)) <= MAX_LINE + 1
    many = "\n".join(f"line {i}" for i in range(2000))
    out = redact(many).split("\n")
    assert len(out) == MAX_LINES and out[-1] == "line 1999"
    big = "\n".join("w" * 100 for _ in range(10_000))
    assert len(redact(big, max_bytes=1000).encode()) <= 1000


def test_ordinary_lines_survive():
    line = "2026-10-30T09:52:11+0000 orch-relay[123]: push sent to device ok (relay 0.1.0)"
    assert redact(line) == line


def test_read_logs_uses_the_fixed_argv_and_redacts():
    part = B.parse_config(
        {
            "v": 1,
            "host_id": "h",
            "parts": {"relay": {"label": "Relay", "kind": "systemd", "unit": "orch-relay.service"}},
        }
    ).parts["relay"]
    seen = []

    def ok(argv, t):
        seen.append((list(argv), t))
        return 0, "a password=hunter2\nb\n", ""

    assert read_logs(part, ok) == f"a password={R}\nb"
    assert (
        seen[0][0][-9:]
        == ["-u", "orch-relay.service", "--since", "-1h", "-n", "500", "-o", "short-iso", "--no-pager"][-9:]
    )
    assert seen[0][1] == B.LOG_TIMEOUT_S
    with pytest.raises(HelperError) as e:
        read_logs(part, lambda a, t: (1, "", "journalctl failed token=abc12345678"))
    assert e.value.code == "action_failed" and "abc12345678" not in e.value.detail

    def boom(a, t):
        raise OSError

    with pytest.raises(HelperError):
        read_logs(part, boom)
