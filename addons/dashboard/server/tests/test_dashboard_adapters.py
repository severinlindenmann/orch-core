from orch_dashboard import adapters


def test_adapter_maps_real_data_only(stub):
    row = stub.call("ticket.query", {})["data"]["rows"][0]
    row["status"] = "in_progress"
    row["lines"] = ["ignored"]
    result = adapters.ticket(row).model_dump()
    assert result["status"] == "in-progress"
    assert result["head_seq"] == row["head_seq"]
    assert "lines" not in result and "progress" not in result
    kinds = ["question", "approve", "no_eligible", "acknowledge", "addon"]
    data = {
        "items": [
            {"kind": k, "key": "DEMO-0001", "uid": "u", "ref": "plan", "detail": "x", "who": ["p"]} for k in kinds
        ],
        "sub_cursor": "c",
    }
    result = adapters.today(data, stub.ws)
    assert [n.kind for n in result.needs_you] == ["question", "approval", "no_eligible", "acknowledge", "addon"]
    assert all(n.title is None and n.since is None for n in result.needs_you)
