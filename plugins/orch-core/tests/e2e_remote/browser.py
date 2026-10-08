"""What the paired device does in the browser: the TIX pages as a person uses them."""
from __future__ import annotations

import time

from playwright.sync_api import expect

import harness as H


def pair(stack, host, scope: str = "operate", link: str | None = None) -> str:
    """The whole pairing ceremony: the owner makes a link on the computer, the browser opens it and shows its
    fingerprint, the owner types the last group of what the device shows and approves in the Remote tab."""
    page = stack.page
    link = link or host.dash.offer(scope)
    page.goto(f"{stack.tix.url}/remote")            # a new hash alone would not reload the pairing page
    page.goto(link)
    page.locator("#pair-go").click()
    page.locator("#pair-fp").wait_for(state="visible", timeout=40_000)
    shown = page.locator("#pair-fp").inner_text().strip()
    mine = H.until(lambda: [p for p in host.dash.pending() if p[1] == shown], 30, what="the pending pairing on the computer")
    did = mine[0][0]
    r = host.dash.approve(did, shown, scope)
    assert r.status_code == 303, r.text
    expect(page.locator("#remote-pair-main h1")).to_have_text("Paired", timeout=40_000)
    return did


def open_remote(stack, host=None) -> None:
    """The Remote page, optionally with a workspace opened in the frame."""
    page = stack.page
    page.goto(f"{stack.tix.url}/remote" + (f"?space={host.space}" if host else ""))
    page.locator("#ws-loading").wait_for(state="hidden", timeout=30_000)


def card(stack, host):
    return stack.page.locator(f'.wrow[data-space="{host.space}"]')


def open_workspace(stack, host) -> None:
    page = stack.page
    if page.url.split("?")[0] != f"{stack.tix.url}/remote":
        open_remote(stack)
    expect(card(stack, host).locator(".pill").first).to_have_text("Online", timeout=40_000)
    card(stack, host).get_by_role("button", name=f"Open {host.name.title()}").click()


def frame_of(page, seconds: int = 40):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        frames = [f for f in page.frames if "/sandbox/dash" in f.url]
        if frames:
            return frames[-1]
        page.wait_for_timeout(100)
    raise AssertionError("no dashboard frame")


def frame_text(page) -> str:
    try:
        return frame_of(page, 2).evaluate("document.body ? document.body.innerText : ''")
    except Exception:  # noqa: BLE001 - the frame is being rebuilt
        return ""


CONSOLE: list[str] = []          # the browser's console, filled by the stack fixture; shown with a failure


def wait_frame_text(page, text: str, seconds: int = 60) -> None:
    try:
        H.until(lambda: text in frame_text(page), seconds, 0.3, what=f"the dashboard frame to show {text!r}")
    except AssertionError as e:
        snap = H.SNAPS / f"frame-{int(time.time())}.png"
        page.screenshot(path=str(snap))
        raise AssertionError(f"{e}\nnotice: {notice(page)!r}\nframe text: {frame_text(page)[:600]!r}\nimage: {snap}\nconsole: {CONSOLE[-12:]}") from None


RAW = """async ({space, method, path, headers, body, replay, timeoutMs}) => {
  const store = await import("/static/js/bridge-store.js");
  const { DeviceSession } = await import("/static/js/bridge-session.js");
  const { deviceId } = await import("/static/js/bridge-crypto.js");
  const { hexToBytes } = await import("/static/js/crypto.js");
  const { createMailbox } = await import("/static/js/remote-mailbox.js");
  const rec = await store.workspaceRecord(space);
  if (!rec?.hostPub) return { error: "not paired in this browser" };
  const dk = await store.deviceKey();
  const sess = new DeviceSession({ workspace: space, kWs: rec.kWs, keyVersion: rec.keyVersion,
    deviceId: await deviceId(hexToBytes(space), dk.pub), signKey: dk.privateKey, hostKey: await store.pinnedHostKey(space) });
  const mb = createMailbox(space);
  const args = { meta: { op: "http", method, path, headers: headers || {} },
                 data: body ? new TextEncoder().encode(body) : new Uint8Array(0), flags: 0 };
  const once = (sent, fresh) => new Promise(async (resolve) => {
    const out = {}, parts = [];
    const done = () => { clearTimeout(t); const n = parts.reduce((a, p) => a + p.length, 0), all = new Uint8Array(n);
      let o = 0; for (const p of parts) { all.set(p, o); o += p.length; } out.body = new TextDecoder().decode(all); resolve(out); };
    const t = setTimeout(() => { out.timeout = true; done(); }, timeoutMs || 40000);
    if (fresh) sess.pending.set(sent.id, { next: 0, stream: false, envelope: sent.envelope, args, lastAt: Date.now() });
    const listener = { fail: (e) => { out.error = String(e); done(); }, chunk: async (c) => {
      const r = await sess.receive(c.env, c.mailbox);
      if (r.result === "drop") { out.drop = r.why || "drop"; return; }
      if (r.refusal) { out.refusal = String(r.meta.refusal); out.message = r.message || null; return done(); }
      if (out.status === undefined) { out.status = r.meta.status; out.page = r.meta.page === true; out.headers = r.meta.headers; }
      if (r.data?.length) parts.push(r.data);
      if (r.last) done();
    } };
    await mb.cancel(sent.id);
    await mb.post(sent.id, sent.envelope, false, listener).catch((e) => { out.error = String(e); done(); });
  });
  const sent = await sess.request(args);
  const first = await once(sent, false);
  let second = null;
  if (replay) second = await once(sent, true);          // the same bytes again, as a retry after a lost answer
  mb.close();
  return replay ? { first, second } : first;
}"""


def raw_request(stack, host, method: str, path: str, body: str | None = None, headers: dict | None = None,
                replay: bool = False, timeout_ms: int = 40_000) -> dict:
    """One request to the workspace from this browser's own device key, outside the frame, answered with what the host
    sent: {status, page, headers, body} or {refusal, message}. With replay, the same sealed bytes are posted twice."""
    return stack.page.evaluate(RAW, {"space": host.space, "method": method, "path": path, "headers": headers or {},
                                     "body": body, "replay": replay, "timeoutMs": timeout_ms})


def notice(page) -> str:
    return page.locator("#remote-notice").inner_text() if page.locator("#remote-notice").is_visible() else ""
