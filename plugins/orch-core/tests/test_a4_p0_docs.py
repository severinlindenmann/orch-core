from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_core_has_no_tix_code():
    import re
    banned = re.compile(r"\btix\b|tix\.severin|\bshr1\b|\bsharing\b|mirror", re.I)
    for p in (ROOT / "src" / "orch").rglob("*"):
        # static/vendor holds third-party libraries verbatim (Plot's minified source says "mirror").
        if p.suffix in (".py", ".html", ".css", ".js") and not {"__pycache__", "vendor"} & set(p.parts):
            text = p.read_text(encoding="utf-8")
            if p.name == "permits.py" and p.parent.name == "core":
                # git's push flag in the AI Factory's never-grantable list, not TIX code: only that exact token
                text = re.sub(r"--mirror\b", "", text)
            hit = banned.search(text)
            assert hit is None, f"{hit.group(0)!r} in {p.relative_to(ROOT)}"


def test_addons_guide_documents_the_new_api():
    text = (ROOT / "ADDONS.md").read_text(encoding="utf-8")
    for term in ("TicketIntent", "on_intent_result", "anchor", "ticket.sync", "accepts_file", "FileResult", "Reveal",
                 "always_on", "long_poll", "Keep syncing while Mission Control runs", "add_artifact", "QR(",
                 "remote_humans", "pairing_target", "remote_decision", "orch schema ticket", "orch wait"):
        assert term in text, term


def test_testing_kit_has_a_fake_remote():
    from orch.testing import FakeRemote
    from orch.remote.verify import RemoteResult
    fake = FakeRemote(RemoteResult("pending", "x"))
    assert fake({"decision_id": "d"}).status == "pending" and fake.seen == [{"decision_id": "d"}]
