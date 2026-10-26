"""#561 (Opus probes 5 and 8), since #565 through orch's reader: a diff the reader cannot produce travels devhost
reads.diff -> server /changes route -> 409 observe.unavailable "diff unavailable"; never a 200 with an empty, partial
or other-base diff. These damage the repository for real (no monkeypatch of the read), and the read starts no
process at all."""

import os
import subprocess

import pytest
from test_dashboard_ticket import headers, url


@pytest.mark.parametrize("damage", ["object", "packed-refs", "extension"])
def test_a_broken_repository_reaches_the_route_as_diff_unavailable(client, token, stub, rich, damage):
    env, _ids = rich
    repo = env.tmp / "code"
    ok = client.get(url(stub, "DEMO-0001/changes"), headers=headers(token))
    assert ok.status_code == 200 and ok.json()["files"], ok.text  # positive control: a real diff with files
    if damage == "object":  # the new blob b.txt ("new\n"): a missing object
        blob = subprocess.run(["git", "-C", str(repo), "rev-parse", "feat/one:b.txt"], capture_output=True,
                              text=True, check=True).stdout.strip()  # fmt: skip
        os.remove(repo / ".git" / "objects" / blob[:2] / blob[2:])
    elif damage == "packed-refs":
        (repo / ".git" / "packed-refs").write_text("not a packed-refs line\n")
    else:  # an extension the reader does not support
        subprocess.run(["git", "-C", str(repo), "config", "core.repositoryformatversion", "1"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "extensions.worktreeConfig", "true"], check=True)
    res = client.get(url(stub, "DEMO-0001/changes"), headers=headers(token))
    assert res.status_code == 409, res.text
    assert res.json()["error"]["code"] == "observe.unavailable"
    msg = res.json()["error"]["message"]
    # a repository the reader refuses as a whole has no identity either: the host never observed it, so no diff
    assert "diff unavailable" in msg or "has not observed" in msg, msg


def test_the_diff_route_starts_no_process(client, token, stub, rich, monkeypatch):
    """#565: the dev host's diff is orch's reader in-process; process creation is forbidden and the route answers."""

    def no(*a, **k):
        raise AssertionError("a process was started")

    monkeypatch.setattr(subprocess.Popen, "__init__", no)
    monkeypatch.setattr(os, "posix_spawn", no)
    res = client.get(url(stub, "DEMO-0001/changes"), headers=headers(token))
    assert res.status_code == 200 and res.json()["files"], res.text
