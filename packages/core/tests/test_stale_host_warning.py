"""B5b: the CLI says so when the host that answered runs another release than the CLI."""

from orch import hostclient
from orch.host.release import code_digest


def test_matching_release_is_silent():
    hostclient._SEEN.update(connected=True, release=code_digest())
    assert hostclient.stale_host_warning() is None


def test_older_or_unreporting_host_warns_with_the_fix():
    for theirs in ("abcdef012345", None):
        hostclient._SEEN.update(connected=True, release=theirs)
        w = hostclient.stale_host_warning()
        assert w and w.startswith("the orch host runs an older release (") and f"than this CLI ({code_digest()})" in w
        assert w.endswith("run `orch host enable` (it restarts the host on the new release)") and "\n" not in w


def test_no_host_answered_no_warning():
    hostclient.forget_host()
    assert hostclient.stale_host_warning() is None
