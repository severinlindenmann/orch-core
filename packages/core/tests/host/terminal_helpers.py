from orch.host.client import Connection, Pin
from tests.host.human_helpers import ok
from tests.host.lifecycle_helpers import UID


def connect(served):
    return Connection(Pin(UID, served.wid, served.lab.run / served.wid / "host.sock"), client="test sign")


def by_id(c, rid):
    (item,) = ok(c.req("human.pending", {"request_id": rid}))["data"]["requests"]
    return item


class Shown:
    def __init__(self, answer=True):
        self.texts, self.plain, self.secrets = [], [], []
        self.answer = answer

    def confirm(self, text):
        self.texts.append(getattr(text, "details", "") or text)
        self.plain.append(str(text))
        return self.answer

    def secret(self, text):
        self.secrets.append(text)
