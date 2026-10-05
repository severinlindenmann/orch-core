"""R0 spike (throwaway): Playwright scenarios for the frame/shim. Results go to frame-results.json."""
import asyncio
import json
import time

from playwright.async_api import async_playwright


def frame_of(page, name="dash"):
    return next((f for f in page.frames if f.name == name), None)


async def log_of(page):
    return await page.evaluate("window.__log")


async def wait_kind(page, kind, since=0, timeout=15, n=1):
    t0 = time.time()
    while time.time() - t0 < timeout:
        log = await log_of(page)
        hits = [m for m in log[since:] if m["t"] == kind]
        if len(hits) >= n:
            return hits, len(log)
        await asyncio.sleep(0.03)
    return None, len(await log_of(page))


async def text_of(page):
    f = frame_of(page)
    try:
        return (await f.evaluate("document.body ? document.body.innerText : ''"))[:3000]
    except Exception as e:
        return "EVAL FAILED: " + str(e)[:100]


async def run(base, shell_app, ws, add_ticket, here):
    R = {}
    stats = shell_app.state.stats
    async with async_playwright() as p:
        for engine in ("chromium", "webkit"):
            try:
                b = await getattr(p, engine).launch()
            except Exception as e:
                R[engine] = {"launch_failed": str(e)[:300]}
                continue
            R[engine] = await scenarios(b, engine, base, shell_app, ws, add_ticket, here, stats, full=(engine == "chromium"))
            await b.close()
    (here / "frame-results.json").write_text(json.dumps(R, indent=1))
    print(json.dumps(R, indent=1))


async def scenarios(b, engine, base, shell_app, ws, add_ticket, here, stats, full):
    out = {}
    ctx = await b.new_context(viewport={"width": 1200, "height": 800})
    page = await ctx.new_page()
    errs = []
    page.on("console", lambda m: errs.append(("console." + m.type, m.text[:160])) if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: errs.append(("pageerror", str(e)[:160])))
    downloads, popups = [], []
    page.on("download", lambda d: downloads.append(d.suggested_filename))
    page.on("popup", lambda pp: popups.append(pp.url))

    def mark():
        return len(stats["paths"])

    # A. first load
    t0 = time.time(); m0 = mark()
    await page.goto(base + "/shell?path=/")
    hits, idx = await wait_kind(page, "rendered")
    out["A_first_load"] = {"wall_ms": round((time.time() - t0) * 1000), "shim_render": hits[0] if hits else None,
                           "bridge_requests": mark() - m0, "eventsource_opened": any(m["t"] == "sopen" for m in await log_of(page))}
    out["A_first_load"]["title"] = await frame_of(page).title()
    await page.screenshot(path=str(here / f"shot-{engine}-today.png"))

    # B. link click (delegated app.js soft navigation or the shim's own)
    t0 = time.time(); m0 = mark(); since = len(await log_of(page))
    await frame_of(page).locator("a", has_text="Board").first.click()
    hits, idx = await wait_kind(page, "rendered", since, 8)
    seen = [m["t"] for m in (await log_of(page))[since:] if m["t"] != "req"]
    out["B_click_link_board"] = {"wall_ms": round((time.time() - t0) * 1000), "messages_after_click": seen,
                                 "bridge_requests": mark() - m0, "frame_pathname": await frame_of(page).evaluate("location.pathname"),
                                 "parent_url_hash": await page.evaluate("location.hash"), "title": await frame_of(page).title()}
    if not hits:   # app.js soft-nav may have swapped in place without 'rendered'
        out["B_click_link_board"]["note"] = "no shim render: page swapped in place by the dashboard's own script or nothing happened"
    await page.screenshot(path=str(here / f"shot-{engine}-board.png"))

    # C. second navigation: a ticket page
    t0 = time.time(); m0 = mark(); since = len(await log_of(page))
    link = frame_of(page).locator("a[href^='/t/']").first
    href = await link.get_attribute("href")
    await link.click()
    await asyncio.sleep(0.8)
    out["C_click_ticket"] = {"href": href, "wall_ms_to_settle": round((time.time() - t0) * 1000), "bridge_requests": mark() - m0,
                             "messages": [m["t"] for m in (await log_of(page))[since:] if m["t"] != "req"],
                             "frame_pathname": await frame_of(page).evaluate("location.pathname"), "title": await frame_of(page).title()}

    # D. back button (the TIX shell's own Back and the browser's)
    since = len(await log_of(page)); t0 = time.time()
    await page.go_back()
    await asyncio.sleep(0.8)
    out["D_back"] = {"wall_ms_to_settle": round((time.time() - t0) * 1000), "frame_pathname": await frame_of(page).evaluate("location.pathname"),
                     "title": await frame_of(page).title(), "parent_hash": await page.evaluate("location.hash"),
                     "messages": [m["t"] for m in (await log_of(page))[since:] if m["t"] != "req"]}
    await page.go_forward()
    await asyncio.sleep(0.8)
    out["D_forward"] = {"frame_pathname": await frame_of(page).evaluate("location.pathname"), "title": await frame_of(page).title()}

    # E. form post: new ticket
    m0 = mark(); since = len(await log_of(page))
    try:
        await frame_of(page).locator("a", has_text="New ticket").first.click()
        await asyncio.sleep(0.8)
        out["E_new_page"] = {"frame_pathname": await frame_of(page).evaluate("location.pathname")}
        await frame_of(page).locator("input[name=title]").first.fill(f"Created through the frame ({engine})")
        await frame_of(page).locator("form[action='/new'] button[type=submit]").first.click()
        await asyncio.sleep(1.2)
        out["E_form_post"] = {"requests": stats["paths"][m0:], "frame_pathname": await frame_of(page).evaluate("location.pathname"),
                              "title": await frame_of(page).title(), "text_has_new_title": f"Created through the frame ({engine})" in await text_of(page), "ticket_on_disk": any(f"Created through the frame ({engine})" in str(p.read_text()) for p in ws.tickets_dir.rglob("*.md"))}
    except Exception as e:
        out["E_form_post"] = {"error": str(e)[:300], "requests": stats["paths"][m0:]}

    # E2. a human-only action must refuse for this agent process (expected)
    try:
        await frame_of(page).locator("a", has_text="Today").first.click()
        await asyncio.sleep(0.8)
        m0 = mark()
        await frame_of(page).locator("button", has_text="Accept, mark done").first.click()
        await asyncio.sleep(1.2)
        t = await frame_of(page).evaluate("document.body.innerText")
        lines = [l for l in t.splitlines() if any(w in l.lower() for w in ("human", "agent", "refus", "only", "cannot", "not allowed"))]
        out["E2_human_only_action"] = {"requests": stats["paths"][m0:], "frame_pathname": await frame_of(page).evaluate("location.pathname"), "lines_mentioning_refusal": lines[:5]}
    except Exception as e:
        out["E2_human_only_action"] = {"error": str(e)[:200]}

    # F. live update via the EventSource stand-in
    try:
        await frame_of(page).locator("a", has_text="Board").first.click()
        await asyncio.sleep(1.0)
        since = len(await log_of(page)); m0 = mark()
        t0 = time.time()
        await asyncio.sleep(4); idle = mark() - m0; m0 = mark()
        t0 = time.time()
        add_ticket(ws, f"Appeared while the page was open ({engine})")
        seen_after = None
        for _ in range(200):
            if f"Appeared while the page was open ({engine})" in await frame_of(page).evaluate("document.body.textContent"):
                seen_after = round((time.time() - t0) * 1000); break
            await asyncio.sleep(0.05)
        out["F_live_update"] = {"requests_during_4s_idle": idle, "appeared_after_ms": seen_after, "requests": stats["paths"][m0:], "stream_messages": [m["t"] for m in (await log_of(page))[since:] if m["t"].startswith("s")]}
    except Exception as e:
        out["F_live_update"] = {"error": str(e)[:200]}

    # G. terminals (fake tmux sessions, patched in the spike process; the real tmux server is never touched)
    try:
        since = len(await log_of(page)); m0 = mark()
        await page.evaluate("window.__go('/terminals')")
        await asyncio.sleep(2.5)
        out["G_terminals_grid"] = {"frame_pathname": await frame_of(page).evaluate("location.pathname"), "text": (await text_of(page))[:200].replace("\n", " | "),
                                   "requests": stats["paths"][m0:][:8], "stream_messages": [m["t"] for m in (await log_of(page))[since:] if m["t"] in ("sopen", "sclose")]}
        since = len(await log_of(page)); m0 = mark()
        await page.evaluate("window.__go('/terminals/orch-spike-1')")
        await asyncio.sleep(1.0)
        await frame_of(page).evaluate("""(() => { window.__lat = []; const re = /t=(\\d+\\.\\d+)/;
          new MutationObserver(() => { const m = re.exec(document.body.textContent); if (m) window.__lat.push(Date.now() / 1000 - parseFloat(m[1])); })
            .observe(document.body, { subtree: true, childList: true, characterData: true }); })()""")
        await asyncio.sleep(8)
        lat = sorted((await frame_of(page).evaluate("window.__lat")) or [])
        keys_before = len(shell_app.state.keys)
        try:
            await frame_of(page).locator("[data-mode=cli]").first.click()
            await page.keyboard.type("ab")
            await asyncio.sleep(0.5)
        except Exception:
            pass
        await asyncio.sleep(0.8)
        out["G_terminal_view"] = {"frame_pathname": await frame_of(page).evaluate("location.pathname"), "dom_updates_seen": len(lat),
                                  "update_latency_ms": ({"p50": round(lat[len(lat) // 2] * 1000), "p95": round(lat[int(len(lat) * .95)] * 1000), "max": round(lat[-1] * 1000)} if lat else None),
                                  "requests": stats["paths"][m0:][:12], "key_posts_reaching_host": len(shell_app.state.keys) - keys_before,
                                  "stream_messages": [m["t"] for m in (await log_of(page))[since:] if m["t"] in ("sopen", "sclose")]}
        r = await shell_app.state.client.post("/terminals/orch-spike-1/keys", json={"seq": ["a"]})
        out["G_keys_post_without_Origin_status"] = r.status_code
        await page.screenshot(path=str(here / f"shot-{engine}-terminal.png"))
    except Exception as e:
        out["G_terminals"] = {"error": str(e)[:300]}

    # H. direct probes of single features inside the shimmed dashboard frame
    f = frame_of(page)
    since = len(await log_of(page))
    probes = {}
    for name, js in {
        "theme_cookie_write": "(()=>{document.cookie='orch_theme=dark; Path=/'; return document.cookie})()",
        "localStorage": "(()=>{localStorage.setItem('a','b'); return localStorage.getItem('a')})()",
        "location.pathname/search": "location.pathname + location.search",
        "document.referrer": "document.referrer",
        "clipboard.writeText": "navigator.clipboard ? navigator.clipboard.writeText('x').then(()=>'ok', e=>'rejected '+e.name) : 'navigator.clipboard undefined'",
        "window.open": "String(window.open('https://example.com'))",
        "pushState to other path": "(()=>{history.pushState({a:1},'','/board?x=1'); return location.pathname+location.search})()",
        "xhr": "(()=>{try{new XMLHttpRequest();return 'constructed'}catch(e){return 'throws '+e.message}})()",
        "fetch external": "fetch('https://example.com/').then(()=>'ok',e=>'rejected '+e.message)",
        "dialog.showModal": "(()=>{const d=document.createElement('dialog');document.body.append(d);d.showModal();const o=d.open;d.close();return o})()",
        "download link click": "(()=>{const a=document.createElement('a');a.href='/static/app.css';a.download='x.css';document.body.append(a);a.click();return 'clicked'})()",
        "target=_blank click": "(()=>{const a=document.createElement('a');a.href='/board';a.target='_blank';document.body.append(a);a.click();return 'clicked'})()",
    }.items():
        try:
            probes[name] = await f.evaluate(js)
        except Exception as e:
            probes[name] = "EVAL THROWS " + str(e)[:140]
    await asyncio.sleep(0.6)
    probes["_downloads_seen_by_browser"] = downloads
    probes["_popups_seen_by_browser"] = popups
    probes["_shim_messages"] = [m for m in (await log_of(page))[since:] if m["t"] not in ("req",)]
    out["H_in_frame_probes"] = probes

    out["errors_console"] = errs[:40]
    out["bridge_requests_total"] = stats["relayed"]
    ms = sorted(stats["relay_server_ms"])
    out["relay_server_ms"] = {"p50": round(ms[len(ms) // 2], 1), "p95": round(ms[int(len(ms) * .95)], 1), "max": round(ms[-1], 1)} if ms else None
    rel = sorted(await page.evaluate("window.__relayMs"))
    out["relay_roundtrip_ms_in_browser"] = {"n": len(rel), "p50": round(rel[len(rel) // 2], 1), "p95": round(rel[int(len(rel) * .95)], 1)} if rel else None
    if not full:
        await ctx.close()
        return out

    await page.close()
    # I. raw platform behaviour of the sandbox policy (no shim)
    pg2 = await ctx.new_page()
    dl2, pop2 = [], []
    pg2.on("download", lambda d: dl2.append(d.suggested_filename))
    pg2.on("popup", lambda pp: pop2.append(pp.url))
    await pg2.goto(base + "/probe-shell")
    for _ in range(100):
        out_probe = await pg2.evaluate("window.__probe")
        if out_probe:
            break
        await asyncio.sleep(0.1)
    out["I_raw_probe"] = out_probe
    pf = next(fr for fr in pg2.frames if fr.name == "probe")
    try:
        await pf.evaluate("(()=>{const a=document.createElement('a');a.href='data:text/plain,hi';a.download='x.txt';document.body.append(a);a.click();const b=document.createElement('a');b.href='/shell';b.target='_blank';document.body.append(b);b.click()})()")
    except Exception as e:
        out["I_raw_probe_links_error"] = str(e)[:120]
    await asyncio.sleep(0.8)
    out["I_raw_downloads_seen"], out["I_raw_popups_seen"] = dl2, pop2

    # J. reload / location assignment inside the real frame (fresh shell each)
    for name, js in (("location.reload()", "location.reload()"), ("location.href = '/board'", "location.href='/board'"), ("location.hash = '#x'", "location.hash='#x'")):
        pg3 = await ctx.new_page()
        await pg3.goto(base + "/shell?path=/")
        await wait_kind(pg3, "rendered")
        n0 = len(stats["navs_to_frame_route"])
        fr = frame_of(pg3)
        before = len(await log_of(pg3))
        try:
            await fr.evaluate(js)
        except Exception as e:
            pass
        await asyncio.sleep(1.2)
        fr2 = frame_of(pg3)
        try:
            body_len = await fr2.evaluate("document.body ? document.body.innerText.length : -1")
            fpath = await fr2.evaluate("location.pathname")
        except Exception as e:
            body_len, fpath = "eval failed", "?"
        out.setdefault("J_navigation_escapes", {})[name] = {"frame_route_hits_after": len(stats["navs_to_frame_route"]) - n0,
                                                           "frame_pathname_after": fpath, "visible_text_len_after": body_len, "iframe_loads": sum(1 for m in await log_of(pg3) if m["t"] == "iframe-load"), "body_text_head": (await fr2.evaluate("document.body ? document.body.innerText.slice(0,60) : ''")) if isinstance(body_len, int) else "",
                                                           "shell_log_after": [m["t"] for m in (await log_of(pg3))[before:] if m["t"] != "req"]}
        await pg3.close()

    # K. phone width, menu drawer
    pg4 = await ctx.new_page()
    await pg4.set_viewport_size({"width": 390, "height": 844})
    await pg4.goto(base + "/shell?path=/")
    await wait_kind(pg4, "rendered")
    await pg4.screenshot(path=str(here / "shot-chromium-phone.png"))
    try:
        await frame_of(pg4).locator("summary", has_text="Menu").first.click()
        await asyncio.sleep(0.4)
        out["K_phone_menu_dialog_open"] = await frame_of(pg4).evaluate("!!document.querySelector('dialog.menu-drawer[open]')")
    except Exception as e:
        out["K_phone_menu_dialog_open"] = "error " + str(e)[:120]
    await ctx.close()
    return out
