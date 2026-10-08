"""What the paired device does in the browser: the TIX pages as a person uses them."""
from __future__ import annotations

import time

from playwright.sync_api import expect

import e2e_harness as H


def pair(stack, host, scope: str = "operate", link: str | None = None, register: bool = False) -> str:
    """The whole pairing ceremony: the owner makes a link on the computer, the browser opens it and shows its
    fingerprint, the owner types the last group of what the device shows and approves in the Remote tab. With
    `register` the browser also makes its platform credential (Face ID or device unlock) on the button's click."""
    page = stack.page
    link = link or host.dash.offer(scope)
    page.goto(f"{stack.tix.url}/remote")            # a new hash alone would not reload the pairing page
    page.goto(link)
    page.locator("#pair-go").click()
    try:
        page.locator("#pair-fp").wait_for(state="visible", timeout=40_000)
    except Exception:  # noqa: BLE001 - say what the page said
        raise AssertionError("no fingerprint after pairing was asked for; the page says: "
                             f"{page.locator('#pair-state').inner_text()!r}; pending on the computer: "
                             f"{host.dash.pending()}; log: {host.text()[-400:]!r}") from None
    shown = page.locator("#pair-fp").inner_text().strip()
    if register:   # the platform credential (the virtual authenticator answers): made on the person's click, before approval
        page.locator("#pair-cred").click()
        expect(page.locator("#pair-cred")).to_have_count(0, timeout=40_000)
        # the note says what the credential is for until it is made, then nothing; a failure puts its sentence there
        expect(page.locator("#pair-cred-note")).to_have_text("", timeout=40_000)
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


# -- a phone: a second browser context with its own keys, its own TIX session and a virtual platform authenticator --

COUNT_SHEETS = """(() => { if (window.top !== window.self) return; window.__sheets = 0;     // the TIX page only, never the frame
  new MutationObserver((muts) => { for (const m of muts) for (const n of m.addedNodes) if (n.id === 'unlock-sheet') window.__sheets++; })
    .observe(document, { childList: true, subtree: true }); })();"""


TAP_REQUESTS = """(() => { if (window.top !== window.self) return; const MC = window.MessageChannel;
  window.__reqs = [];                                       // what the frame asked its host for: [time, method, path]
  window.MessageChannel = function () { const c = new MC();
    c.port1.addEventListener('message', (e) => { const d = e.data || {};
      if (d.t === 'req' || d.t === 'sopen') { let b = ''; try { b = d.body ? new TextDecoder().decode(d.body) : ''; } catch (err) { b = '?'; }
        window.__reqs.push([Math.round(performance.now()), d.method || 'GET', String(d.path).slice(0, 100), b.slice(0, 140)]); } });
    c.port1.start(); return c; }; })();"""


def frame_requests(phone) -> list:
    """Every request the dashboard frame has made on this page so far: [ms, method, path]."""
    return phone.page.evaluate("window.__reqs || []")


def new_phone(stack):
    """-> a Stack whose page is a new browser context (a phone). Its WebAuthn is a CDP virtual platform authenticator
    that answers at once with user verification, so Face ID is simulated and the TIX app's own code runs unchanged.
    `phone.extra["auth"]` is the authenticator id, `phone.cdp` the CDP session (see set_user_verified)."""
    import dataclasses
    ctx = stack.extra["browser"].new_context(viewport={"width": 390, "height": 844})
    ctx.add_init_script(COUNT_SHEETS)
    ctx.add_init_script(TAP_REQUESTS)
    page = ctx.new_page()
    page.on("console", lambda m: CONSOLE.append(f"phone {m.type}: {m.text}"[:300]))
    page.goto(f"{stack.tix.url}/login")
    page.locator("#passphrase").fill(stack.tix.sim.passphrase)
    page.get_by_role("button", name="Log in").click()
    page.wait_for_url(f"{stack.tix.url}/", timeout=30_000)
    cdp = ctx.new_cdp_session(page)
    cdp.send("WebAuthn.enable")
    auth = cdp.send("WebAuthn.addVirtualAuthenticator", {"options": {
        "protocol": "ctap2", "transport": "internal", "hasResidentKey": True, "hasUserVerification": True,
        "isUserVerified": True, "automaticPresenceSimulation": True}})["authenticatorId"]
    return dataclasses.replace(stack, page=page, context=ctx, cdp=cdp, extra={**stack.extra, "auth": auth})


def set_user_verified(phone, ok: bool) -> None:
    """The person fails (or passes) Face ID: with False the authenticator cannot verify the user, so a confirmation fails."""
    phone.cdp.send("WebAuthn.setUserVerified", {"authenticatorId": phone.extra["auth"], "isUserVerified": ok})


def sheets_shown(phone) -> int:
    """How many unlock sheets this page has drawn so far."""
    return phone.page.evaluate("window.__sheets || 0")


def sheet_text(phone, timeout: int = 60_000) -> str:
    phone.page.locator("#unlock-sheet").wait_for(state="visible", timeout=timeout)
    return phone.page.locator("#unlock-text").inner_text()


def confirm_sheet(phone) -> None:
    """Read to the end, wait out the sheet's own delay, press Confirm (a real click), wait for the sheet to go."""
    page = phone.page
    page.wait_for_timeout(700)
    page.evaluate("() => { const t = document.getElementById('unlock-text'); t.scrollTop = t.scrollHeight; }")
    go = page.locator("#unlock-go")
    expect(go).to_be_enabled(timeout=10_000)
    go.click()
    expect(page.locator("#unlock-sheet")).to_have_count(0, timeout=30_000)


def cancel_sheet(phone) -> None:
    phone.page.locator("#unlock-cancel").click()
    expect(phone.page.locator("#unlock-sheet")).to_have_count(0, timeout=10_000)
