import json

import pytest

from orch import harness_update as hu
from orch import update

ORIGIN = {"origin": "http://testserver"}
CASK = "/opt/homebrew/Caskroom/claude-code@latest/2.0.1/claude"
NPM = "/usr/local/lib/node_modules/@openai/codex/bin/codex.js"


class Machine:
    """A fake PATH, symlinks and commands: {program: real path}, what `--version` says, what brew and npm know."""

    def __init__(self, monkeypatch, bins, versions, brew=None, npm=None):
        self.bins, self.versions, self.brew, self.npm, self.ran = bins, versions, brew or {}, npm or {}, []
        monkeypatch.setattr(hu, "harness_bins", lambda: {"claude": "claude", "codex": "codex", "copilot": "copilot"})
        monkeypatch.setattr(hu, "_which", lambda p: f"/bin/{p}" if p in self.bins or p in ("brew", "npm") else None)
        monkeypatch.setattr(hu, "_realpath", lambda p: self.bins.get(p.rpartition("/")[2], p))
        monkeypatch.setattr(hu, "_run", self.run)
        monkeypatch.setattr(hu, "_npm_latest", lambda pkg: self.npm.get(pkg))
        monkeypatch.setattr(update, "core_source", lambda: "no clone in tests")

    def run(self, argv, timeout=hu.CHECK_TIMEOUT, env=None):
        self.ran.append(argv)
        prog = argv[0].rpartition("/")[2]
        if argv[1:] == ["--version"]:
            return 0, f"{self.versions[prog]} (Claude Code)"
        if argv[1:3] == ["info", "--json=v2"]:
            assert env == {"HOMEBREW_NO_AUTO_UPDATE": "1"}
            return 0, json.dumps({"casks": [{"version": self.brew[argv[-1]]}]})
        if "upgrade" in argv or "install" in argv or argv[1:] == ["update"]:  # the update: --version now says latest
            self.versions = {k: "9.9.9" for k in self.versions}
            self.brew = {k: "9.9.9" for k in self.brew}
            self.npm = {k: "9.9.9" for k in self.npm}
            return 0, "done"
        raise AssertionError(argv)


@pytest.mark.parametrize("path,via,package,command", [
    (CASK, "brew cask", "claude-code@latest", ["/bin/brew", "upgrade", "--cask", "claude-code@latest"]),
    ("/opt/homebrew/Cellar/gemini-cli/1.0/libexec/lib/node_modules/@google/gemini-cli/dist/index.js",
     "brew", "gemini-cli", ["/bin/brew", "upgrade", "gemini-cli"]),
    (NPM, "npm", "@openai/codex", ["/bin/npm", "install", "-g", "@openai/codex@latest"]),
    ("/home/u/.local/share/claude/versions/2.0.1", "native", "@anthropic-ai/claude-code",
     ["/home/u/.local/share/claude/versions/2.0.1", "update"]),
    ("/home/u/bin/mystery", "unknown", None, None),
])
def test_how_a_harness_was_installed_decides_its_update_command(monkeypatch, path, via, package, command):
    monkeypatch.setattr(hu, "_which", lambda p: f"/bin/{p}")
    assert hu._how("x", path) == (via, package, command)


def test_a_brew_cask_behind_its_newest_version_offers_the_upgrade(monkeypatch):
    Machine(monkeypatch, {"claude": CASK}, {"claude": "2.0.1"}, brew={"claude-code@latest": "2.0.14"})
    h = hu.check("claude", "claude")
    assert h.has_update and h.summary == "Claude Code 2.0.1 → 2.0.14"
    assert h.line == "2.0.1 → 2.0.14 available (brew cask claude-code@latest): /bin/brew upgrade --cask claude-code@latest"


def test_up_to_date_and_unknown_installs_offer_nothing(monkeypatch):
    Machine(monkeypatch, {"codex": NPM, "copilot": "/home/u/bin/copilot"}, {"codex": "0.5.0", "copilot": "1.0.0"},
            npm={"@openai/codex": "0.5.0"})
    found = {h.name: h for h in hu.check_all()}
    assert set(found) == {"codex", "copilot"}  # claude is not on this PATH
    assert not found["codex"].has_update and found["codex"].line == "up to date (0.5.0, npm @openai/codex)"
    assert not found["copilot"].has_update and "cannot tell how it was installed" in found["copilot"].line


def test_offline_says_not_checked_rather_than_up_to_date(monkeypatch):
    Machine(monkeypatch, {"codex": NPM}, {"codex": "0.5.0"})
    (h,) = hu.check_all()
    assert not h.has_update and "not checked" in h.line


def test_orch_update_offers_the_harness_with_orch_and_applies_it(monkeypatch):
    m = Machine(monkeypatch, {"claude": CASK}, {"claude": "2.0.1"}, brew={"claude-code@latest": "2.0.14"})
    said = []
    update.run(check_only=False, force=True, ask=lambda q: said.append(q) or "y", review_text=str,
               confirm=lambda n: False, out=said.append)
    assert "Claude Code: 2.0.1 → 2.0.14 available" in " ".join(said)
    assert any("Claude Code 2.0.1 → 2.0.14" in s and "Update now?" in s for s in said)
    assert ["/bin/brew", "upgrade", "--cask", "claude-code@latest"] in m.ran
    assert said[-1] == "Claude Code updated 2.0.1 → 9.9.9. Restart its running sessions to use it."
    assert hu.last()[0][0].installed == "9.9.9"  # the About card sees the new version


def test_saying_no_runs_nothing(monkeypatch):
    m = Machine(monkeypatch, {"claude": CASK}, {"claude": "2.0.1"}, brew={"claude-code@latest": "2.0.14"})
    update.run(check_only=False, force=True, ask=lambda q: "n", review_text=str, confirm=lambda n: False,
               out=lambda s: None)
    assert not any("upgrade" in a for a in m.ran)


def test_a_failed_update_says_what_to_run(monkeypatch):
    Machine(monkeypatch, {"codex": NPM}, {"codex": "0.5.0"}, npm={"@openai/codex": "0.6.0"})
    monkeypatch.setattr(hu, "_run", lambda argv, timeout=0, env=None:
                        (0, "0.5.0") if argv[-1] == "--version" else (243, "npm ERR! EACCES"))
    (h,) = hu.check_all()
    assert hu.apply(h) == ("Codex: the update failed (npm ERR! EACCES); run it yourself: "
                           "/bin/npm install -g @openai/codex@latest")


@pytest.fixture
def client(ws):
    from fastapi.testclient import TestClient

    from orch.dashboard.app import create_app
    c = TestClient(create_app(ws, "tok"))
    assert c.get("/?token=tok").status_code == 200
    return c


def test_about_card_checks_then_asks_then_updates(monkeypatch, client):
    m = Machine(monkeypatch, {"claude": CASK}, {"claude": "2.0.1"}, brew={"claude-code@latest": "2.0.14"})
    assert "Not checked yet" in client.get("/workspace?tab=setup").text
    r = client.post("/workspace/harnesses/check", headers=ORIGIN, follow_redirects=False)
    assert "Claude+Code+2.0.1+%E2%86%92+2.0.14" in r.headers["location"]
    page = client.get("/workspace?tab=setup").text
    assert "Update Claude Code" in page and "Claude Code 2.0.14 available" in page
    ask = client.post("/workspace/harnesses/claude/update", data={"ask": "1"}, headers=ORIGIN)
    assert "brew upgrade --cask claude-code@latest" in ask.text and not any("upgrade" in a for a in m.ran)
    done = client.post("/workspace/harnesses/claude/update", headers=ORIGIN, follow_redirects=False)
    assert "updated" in done.headers["location"] and ["/bin/brew", "upgrade", "--cask", "claude-code@latest"] in m.ran
    assert "Update Claude Code" not in client.get("/workspace?tab=setup").text


def test_harness_routes_refuse_a_foreign_origin_and_unknown_names(monkeypatch, client):
    Machine(monkeypatch, {"claude": CASK}, {"claude": "2.0.1"}, brew={"claude-code@latest": "2.0.14"})
    evil = {"origin": "http://evil.example"}
    assert client.post("/workspace/harnesses/claude/update", headers=evil).status_code == 403
    assert client.post("/workspace/harnesses/check", headers=evil).status_code == 403
    r = client.post("/workspace/harnesses/rm/update", headers=ORIGIN, follow_redirects=False)
    assert "not+a+known+harness" in r.headers["location"]
