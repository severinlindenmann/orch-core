def test_smoke(ws, cli):
    r = cli("new", "First ticket")
    print(r.out, r.err)
    assert r.code == 0, r.err
    r = cli("claim", "DEMO-0001")
    print(r.out, r.err)
    assert r.code == 0
    r = cli("show")
    print(r.out, r.err)
    assert r.code == 0
