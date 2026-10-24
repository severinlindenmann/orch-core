"""orch add-on: one JSON-RPC request on stdin, one response on stdout.
The host sends only the ticket's key, type, status and this add-on's own fields. The add-on holds no key; it
*proposes*, the host checks the proposal against the grant and signs it itself."""
import json
import sys


def propose(params):
    ticket = params["ticket"]
    fields = ticket["fields"]
    if ticket["type"] != "feature" or ticket["status"] != "done":
        return {}  # nothing to propose
    if fields.get("line") or fields.get("skip"):
        return {}
    return {"set": {"line": "Shipped " + ticket["key"] + "."}}


request = json.loads(sys.stdin.readline())
if request["method"] == "propose":
    reply = {"jsonrpc": "2.0", "id": request["id"], "result": propose(request["params"])}
else:
    reply = {"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": "no such method"}}
sys.stdout.write(json.dumps(reply) + "\n")
