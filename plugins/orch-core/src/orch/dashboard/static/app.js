// Progressive enhancement only: every page works without this file.
(() => {
  const html = document.documentElement || { dataset: {} };
  let renderedAt = Date.now();  // when the page shown was rendered (reset by an in-place swap), for "Updated N min ago"
  const isEl = (el) => Boolean(el && el.closest);

  // Theme switch: without JS the buttons POST /theme (cookie + redirect); here it applies at once.
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!form.matches || !form.matches("form.theme-switch")) return;
    const value = event.submitter && event.submitter.value;
    if (!["light", "dark", "system"].includes(value)) return;
    event.preventDefault();
    html.dataset.theme = value;
    document.cookie = "orch_theme=" + value + "; Path=/; Max-Age=31536000; SameSite=Strict";
    form.querySelectorAll("button[name=theme]").forEach((b) => b.setAttribute("aria-pressed", String(b.value === value)));
  });

  // Workspace tabs: every panel is in the page and the server hides all but the chosen one. A #fragment inside a
  // hidden panel (an action's redirect to #phones, a #trust-x link) opens that panel instead.
  const openTabFor = (root) => {
    const hash = window.location && window.location.hash;
    if (!hash || hash.length < 2 || !root.querySelector || !document.getElementById) return;
    const target = document.getElementById(decodeURIComponent(hash.slice(1)));
    const panel = target && target.closest && target.closest("[data-tab-panel][hidden]");
    if (!panel) return;
    document.querySelectorAll("[data-tab-panel]").forEach((p) => { p.hidden = p !== panel; });
    document.querySelectorAll("a[data-tab]").forEach((a) => {
      const on = a.dataset.tab === panel.dataset.tabPanel;
      a.classList.toggle("on", on);
      if (on) a.setAttribute("aria-current", "true"); else a.removeAttribute("aria-current");
    });
    target.scrollIntoView();
  };
  openTabFor(document);
  window.addEventListener("hashchange", () => openTabFor(document));

  // A form marked data-busy (Refresh on an addon page): after 1 s its button says so; after 10 s it counts.
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!form || !form.matches || !form.matches("form[data-busy]") || event.defaultPrevented) return;
    const button = form.querySelector("button[type=submit]");
    if (!button) return;
    const started = Date.now();
    let stopped = false;
    window.addEventListener("pagehide", () => { stopped = true; }, { once: true });
    const tick = () => {
      if (stopped || !form.isConnected) return;  // the page moved on (or was swapped): stop counting
      const s = Math.round((Date.now() - started) / 1000);
      button.setAttribute("aria-busy", "true");
      button.textContent = s >= 10 ? "Still fetching… (" + s + " s)" : form.dataset.busy;
      setTimeout(tick, 1000);
    };
    setTimeout(tick, 1000);
  });

  // Filters marked data-autosubmit (Board): a select changed with the mouse or a finger applies at once. A keyboard
  // user steps through the options with the arrow keys, which fires change on every step (WCAG 3.2.2), so a change
  // made from the keyboard waits for the Filter button (or Enter); the button stays visible for that.
  const pointerSelects = new WeakSet();
  document.addEventListener("pointerdown", (event) => {
    const field = event.target && event.target.closest && event.target.closest("form[data-autosubmit] select");
    if (field) pointerSelects.add(field);
  });
  document.addEventListener("keydown", (event) => {
    const field = event.target;
    if (field && field.matches && field.matches("form[data-autosubmit] select")) pointerSelects.delete(field);
  });
  document.addEventListener("change", (event) => {
    const field = event.target;
    if (!field || !field.matches || !field.matches("select")) return;
    const form = field.closest("form[data-autosubmit]");
    if (form && form.requestSubmit && pointerSelects.has(field)) form.requestSubmit();
  });

  // ---------- Live search (D) ----------
  // A search box in a form marked data-live-search="<selectors>" filters as you type: LIVE_MS after the last key it
  // fetches the form's own GET URL and swaps only those regions from the answer (no reload), keeping the box itself:
  // focus, caret and anything typed meanwhile. A newer key aborts the older request; only the newest answer lands.
  // Enter and the form's button submit as before, and without JS the form is a plain GET. Typing never reaches the
  // page's keyboard shortcuts (they ignore text fields), and nothing is sent while an IME composes.
  const LIVE_MS = 200;
  let liveTimer = null;
  let liveAbort = null;
  let liveSeq = 0;
  const liveBox = (el) => Boolean(el && el.matches && el.matches("form[data-live-search] input[type=search]"));
  const liveRun = (input) => {
    const form = input.form;
    if (!form || !window.DOMParser) return;
    const selectors = String(form.dataset.liveSearch || "").split(",").map((x) => x.trim()).filter(Boolean);
    const url = new URL(form.getAttribute("action") || window.location.pathname, window.location.href);
    const params = new URLSearchParams();
    for (const [k, v] of new FormData(form)) if (v !== "") params.append(k, v);  // empty filters stay out of the URL
    url.search = params.toString();
    if (liveAbort) liveAbort.abort();
    liveAbort = window.AbortController ? new window.AbortController() : null;
    const seq = ++liveSeq;
    form.setAttribute("aria-busy", "true");
    fetch(url.toString(), { credentials: "same-origin", signal: liveAbort ? liveAbort.signal : undefined })
      .then((r) => (r.ok ? r.text() : Promise.reject(new Error("search failed"))))
      .then((text) => {
        if (seq !== liveSeq) return;  // a newer search is on its way
        const fresh = new window.DOMParser().parseFromString(text, "text/html");
        const focused = document.activeElement === input;
        const start = input.selectionStart;
        const end = input.selectionEnd;
        selectors.forEach((sel) => {
          const now = document.querySelector(sel);
          const next = fresh.querySelector(sel);
          if (!now || !next) return;
          if (now.contains(input)) {  // the box sits in the swapped region: keep this one, not its fresh copy
            const copy = next.querySelector("input[type=search][name=\"" + input.name + "\"]");
            if (copy) copy.replaceWith(input);
          }
          now.replaceWith(next);
        });
        if (focused && document.activeElement !== input) {
          input.focus();
          try { input.setSelectionRange(start, end); } catch (e) { /* type=search may refuse a range */ }
        }
        if (window.history && window.history.replaceState) window.history.replaceState(null, "", url.toString());
      })
      .catch(() => { /* aborted by a newer key, or offline: Enter still submits */ })
      .finally(() => { if (seq === liveSeq) form.removeAttribute("aria-busy"); });
  };
  document.addEventListener("input", (event) => {
    const input = event.target;
    if (!liveBox(input) || event.isComposing) return;
    window.clearTimeout(liveTimer);
    liveTimer = window.setTimeout(() => liveRun(input), LIVE_MS);
  });
  document.addEventListener("compositionend", (event) => {
    const input = event.target;
    if (!liveBox(input)) return;
    window.clearTimeout(liveTimer);
    liveTimer = window.setTimeout(() => liveRun(input), LIVE_MS);
  });
  document.addEventListener("submit", (event) => {
    if (event.target && event.target.matches && event.target.matches("form[data-live-search]")) {
      window.clearTimeout(liveTimer);  // Enter: the full page load wins over a pending live search
      liveSeq += 1;
    }
  });

  // Board Backlog fold (C): remember the choice per user (POST /board/backlog; without JS the fold still opens).
  const firstFold = document.querySelector("details[data-backlog-fold]");
  let foldOpen = firstFold ? firstFold.open : null;  // every swimlane's fold follows; one POST per change
  document.addEventListener("toggle", (event) => {
    const fold = event.target;
    if (!fold || !fold.matches || !fold.matches("details[data-backlog-fold]") || fold.open === foldOpen) return;
    foldOpen = fold.open;
    document.querySelectorAll("details[data-backlog-fold]").forEach((other) => { if (other !== fold) other.open = fold.open; });
    fetch("/board/backlog", { method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: "open=" + (fold.open ? "1" : "0") })
      .catch(() => {});
  }, true);

  // Board drag and drop: dragging a card into another lane posts the same /move the
  // keyboard move control on the ticket page uses, then shows the flash back on the board.
  let draggedId = null;
  document.addEventListener("dragstart", (event) => {
    const card = event.target.closest && event.target.closest("[data-ticket]");
    if (!card) return;
    draggedId = card.dataset.ticket;
    if (event.dataTransfer) {
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", draggedId);
    }
  });
  document.addEventListener("dragover", (event) => {
    const lane = event.target.closest && event.target.closest(".lane[data-status]");  // Needs you takes no drop
    if (!lane) return;
    event.preventDefault();
    lane.classList.add("drop");
  });
  document.addEventListener("dragleave", (event) => {
    const lane = event.target.closest && event.target.closest(".lane");
    if (lane) lane.classList.remove("drop");
  });
  document.addEventListener("drop", (event) => {
    const lane = event.target.closest && event.target.closest(".lane");
    if (!lane) return;
    event.preventDefault();
    lane.classList.remove("drop");
    const id = draggedId || (event.dataTransfer && event.dataTransfer.getData("text/plain"));
    const status = lane.dataset.status;
    draggedId = null;
    if (!id || !status) return;
    fetch("/t/" + id + "/move", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: "to=" + encodeURIComponent(status),
      credentials: "same-origin",
      redirect: "follow",
    }).then((response) => {
      if (!response.ok || response.url.includes("/move")) {
        window.location = "/board?err=" + encodeURIComponent("could not move the ticket");
        return;
      }
      window.location = "/board" + new URL(response.url).search;
    }).catch(() => window.location.reload());
  });

  // ---------- Receipts (design system Receipt): what happened, in place, never a toast ----------
  // A receipt replaces the card it belongs to and sits in an aria-live region. The live reload that follows the
  // write would drop it, so it is also kept for this tab (sessionStorage) and shown once more on the next page.
  const RECEIPT_KEY = "orch-receipts";
  const clock = () => new Date().toTimeString().slice(0, 5);
  const keepReceipt = (text) => {
    try {
      const all = JSON.parse(window.sessionStorage.getItem(RECEIPT_KEY) || "[]");
      all.push({ text, at: Date.now() });
      window.sessionStorage.setItem(RECEIPT_KEY, JSON.stringify(all.slice(-5)));
    } catch (e) { /* no storage: the receipt shows until the page changes */ }
  };
  const receiptEl = (text, kind) => {
    const p = document.createElement("p");
    p.className = "receipt" + (kind === "pending" ? " receipt-pending" : kind === "err" ? " receipt-err" : "");
    p.setAttribute("role", kind === "err" ? "alert" : "status");
    const mark = document.createElement("span");
    mark.className = "receipt-mark";
    mark.setAttribute("aria-hidden", "true");
    mark.textContent = kind === "pending" ? "○" : kind === "err" ? "✕" : "✓";
    const words = document.createElement("span");
    words.textContent = text;
    p.append(mark, " ", words);
    return p;
  };
  const showKeptReceipts = () => {
    const box = document.getElementById("receipts");
    if (!box) return;
    let all = [];
    try {
      all = JSON.parse(window.sessionStorage.getItem(RECEIPT_KEY) || "[]");
      window.sessionStorage.removeItem(RECEIPT_KEY);
    } catch (e) { return; }
    all.filter((r) => Date.now() - r.at < 120000).forEach((r) => box.append(receiptEl(r.text)));
  };
  const nextCard = (card) => {
    const all = [...document.querySelectorAll("[data-decision]")].filter((c) => !c.hidden);
    const i = all.indexOf(card);
    return all[i + 1] || all[i - 1] || null;
  };
  // The card collapses into its receipt; focus moves to the next decision (or stays on the receipt).
  const collapseInto = (card, text) => {
    const r = receiptEl(text + " · " + clock());
    r.setAttribute("tabindex", "-1");
    const next = nextCard(card);
    card.replaceWith(r);
    if (next) focusCard(next); else r.focus();
    keepReceipt(text + " · " + clock());
  };
  const encode = (form, submitter) => {
    const data = new FormData(form);
    if (submitter && submitter.name && !data.has(submitter.name)) data.append(submitter.name, submitter.value);
    return new URLSearchParams(data);
  };
  const post = (form, body) => fetch(form.action, {
    method: "POST", body, credentials: "same-origin", redirect: "follow",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
  });
  const outcome = (response) => {
    const url = new URL(response.url);
    return { ok: response.ok && !url.searchParams.get("err"), msg: url.searchParams.get("msg"), err: url.searchParams.get("err"), url };
  };

  // ---------- Careful tier: an in-page <dialog> (design system Dialog), never a browser popup ----------
  // For forms with data-dialog (release, trust, revoke, tidy, addon actions). Focus starts on the safe button, Esc
  // cancels, a click outside does nothing, focus returns to the opener. The form carries ask=1, so without JS the
  // server shows a confirm page first; confirming here clears it and posts the form.
  let dialogEl = null;
  const dialogOpen = () => Boolean(document.querySelector && document.querySelector("dialog[open]"));
  const makeDialog = () => {
    if (dialogEl) return dialogEl;
    dialogEl = document.createElement("dialog");
    dialogEl.className = "dialog-sheet overlay orch-dialog";
    dialogEl.setAttribute("aria-labelledby", "orch-dialog-title");
    const title = document.createElement("h2");
    title.id = "orch-dialog-title";
    const body = document.createElement("p");
    body.className = "orch-dialog-body";
    const row = document.createElement("div");
    row.className = "cluster dialog-actions";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "btn";
    const ok = document.createElement("button");
    ok.type = "button";
    row.append(cancel, ok);
    dialogEl.append(title, body, row);
    document.body.append(dialogEl);
    return dialogEl;
  };
  const openDialog = (form, opener) => {
    if (!window.HTMLDialogElement) return false;  // no <dialog>: the server's confirm page asks instead
    const d = makeDialog();
    const [cancel, ok] = d.querySelectorAll("button");
    d.querySelector("h2").textContent = form.dataset.dialog;
    const body = d.querySelector(".orch-dialog-body");
    body.textContent = form.dataset.dialogBody || "";
    body.hidden = !form.dataset.dialogBody;
    cancel.textContent = form.dataset.dialogCancel || "Cancel";
    ok.textContent = form.dataset.dialogConfirm || ((opener && opener.textContent) || "Confirm").replace(/…\s*$/, "").trim();
    ok.className = "btn " + ("dialogDanger" in form.dataset ? "btn-danger" : "btn-primary");
    const back = opener || form.querySelector("button[type=submit], button:not([type])");
    const close = (confirmed) => {
      d.close();
      if (!confirmed) {
        if (back && back.isConnected) back.focus();
        return;
      }
      const ask = form.querySelector('input[name="ask"]');
      if (ask) ask.value = "";
      form.dataset.dialogConfirmed = "1";
      if (form.requestSubmit) form.requestSubmit(opener && opener.form === form ? opener : undefined);
      else form.submit();
    };
    cancel.onclick = () => close(false);
    ok.onclick = () => close(true);
    d.oncancel = (event) => { event.preventDefault(); close(false); };  // Esc
    d.showModal();
    cancel.focus();
    return true;
  };
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!form.matches || !form.matches("form[data-dialog]")) return;
    if (form.dataset.dialogConfirmed) {
      delete form.dataset.dialogConfirmed;
      return;
    }
    if (openDialog(form, event.submitter)) event.preventDefault();
  });

  // ---------- Inline two-step confirm (design system "Inline confirm") ----------
  // For forms with data-inline-confirm: the first press arms the button in place (its label names what it binds,
  // e.g. "Confirm · plan ab1e…7f", and a Cancel appears), a second, separate press within 6 s submits. Esc, Cancel or
  // the timeout reverts, and focus stays on the button. No popup, no undo. A held key or a double click never
  // confirms: the confirming press must come after a key or pointer release that happened while armed, and at least
  // 400 ms after arming. A confirmed form inside a decision card (data-receipt) posts in the background and the card
  // collapses into its receipt. Without JS the form posts at once; the server still checks it (an approval carries
  // the hash of the text shown).
  const ARM_MS = 6000;
  const DOUBLE_CLICK_MS = 400;
  const reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const armed = new WeakMap();  // form -> {at, released, timer}
  const canConfirm = (state, now) => Boolean(state && state.released && now - state.at >= DOUBLE_CLICK_MS);
  const disarm = (form, refocus) => {
    const state = armed.get(form);
    const button = form.querySelector("button[data-armed-label]");
    if (state) clearTimeout(state.timer);
    armed.delete(form);
    delete form.dataset.armed;
    form.querySelectorAll(".ic-cancel, .ic-live").forEach((el) => el.remove());
    form.classList.remove("inline-confirm");
    if (!button) return;
    button.textContent = button.dataset.armedLabel;
    delete button.dataset.armedLabel;
    button.classList.remove("is-armed");
    button.style.minWidth = "";
    if (refocus) button.focus();
  };
  const arm = (form, button) => {
    document.querySelectorAll("form[data-armed]").forEach((other) => disarm(other, false));
    button.style.minWidth = button.offsetWidth + "px";  // nothing moves or shrinks when the label changes
    button.dataset.armedLabel = button.textContent;
    button.textContent = form.dataset.inlineConfirm;
    button.classList.add("is-armed");
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "btn btn-quiet ic-cancel";
    cancel.textContent = "Cancel";
    cancel.addEventListener("click", () => disarm(form, true));
    button.after(cancel);
    const live = document.createElement("p");
    live.className = "ic-live muted";
    live.setAttribute("aria-live", "polite");
    form.append(live);
    live.textContent = "Press again to confirm" + (reduced ? " within 6 s" : "") + ". Esc cancels.";
    form.classList.add("inline-confirm");
    form.dataset.armed = "1";
    const state = { at: Date.now(), released: false, timer: 0 };
    state.timer = setTimeout(() => disarm(form, document.activeElement === button), ARM_MS);
    armed.set(form, state);
    button.focus();
  };
  // An epic's approve form (data-charter-confirm): the confirm label says whether the delegation is on and its
  // limits, as chosen in the form; changing them while armed disarms, so the label pressed is what is signed.
  const charterLabel = (form) => {
    const f = form.elements;
    const on = f.delegate && f.delegate.checked;
    if (f.factory && f.factory.checked) {  // AI Factory: its own limits, whatever the delegation fields say
      form.dataset.inlineConfirm = form.dataset.charterConfirm + " · START AI FACTORY: " + form.dataset.factoryConfirm;
      return;
    }
    form.dataset.inlineConfirm = form.dataset.charterConfirm + (on
      ? ` · delegation on: up to ${f.max_children.value} children, size ≤ ${f.max_size.value}` : " · no delegation");
  };
  document.addEventListener("change", (event) => {
    const form = event.target.closest && event.target.closest("form[data-charter-confirm]");
    if (!form) return;
    if (armed.has(form)) disarm(form, false);
    charterLabel(form);
  });
  const armedFormOf = (target) => target && target.closest && target.closest("form[data-armed]");
  const confirmInPlace = (form, button) => {
    const card = form.closest("[data-decision]");
    if (!card || !form.dataset.receipt || !window.fetch || !window.URLSearchParams) return false;
    button.setAttribute("aria-busy", "true");
    post(form, encode(form, button)).then((response) => {
      const o = outcome(response);
      if (!o.ok) { window.location = o.url.href; return; }  // the server's message, on the page it chose
      disarm(form, false);
      collapseInto(card, form.dataset.receipt);
    }).catch(() => form.submit());
    return true;
  };
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!form.matches || !form.matches("form[data-inline-confirm]")) return;
    const state = armed.get(form);
    if (state) {
      if (!canConfirm(state, Date.now())) event.preventDefault();
      else {
        const button = form.querySelector("button[data-armed-label]");
        if (button) button.setAttribute("aria-busy", "true");
        if (button && confirmInPlace(form, button)) event.preventDefault();
      }
      return;
    }
    event.preventDefault();
    const button = event.submitter || form.querySelector("button[type=submit]");
    if (button) arm(form, button);
  });
  document.addEventListener("keydown", (event) => {
    const form = armedFormOf(event.target);
    if (!form) return;
    if (event.key === "Escape") disarm(form, true);
    else if (event.repeat) event.preventDefault();  // a held Enter or Space never reaches the button again
  }, true);
  const release = (event) => {
    const state = armed.get(armedFormOf(event.target));
    if (state) state.released = true;
  };
  document.addEventListener("keyup", release, true);
  document.addEventListener("pointerup", release, true);
  // Coming back with the browser's Back button must never find a button still armed.
  // A reload or Back can restore the delegation fields the human had set: the label must follow them, at start and on
  // every pageshow (the charter form also has autocomplete="off").
  const charterLabels = () => document.querySelectorAll("form[data-charter-confirm]").forEach(charterLabel);
  charterLabels();
  window.addEventListener("pageshow", () => {
    document.querySelectorAll("form[data-armed]").forEach((f) => disarm(f, false));
    charterLabels();
  });
  window.orchInlineConfirm = { canConfirm, DOUBLE_CLICK_MS };  // read by the unit test

  // ---------- Delayed send with Undo (answers and messages only; approvals and verdicts have none) ----------
  // A form with data-delayed-send is held for 5 s: the answered question (or the card, for a message) turns into
  // "○ Sending … in 5 s · Undo", and z or Undo brings it back with focus on the button that sent it. Undo acts only
  // after a key or pointer release seen while held, and a held key repeats into neither the form nor the receipt, so
  // holding Enter can never cycle through Undo into another option. Then it posts in the background (the form carries
  // the hash of what was shown, so a question or gate that changed meanwhile is refused) and becomes its receipt.
  // Leaving the page sends what is still held (fetch keepalive) and says so on the next page; a live refresh waits
  // until nothing is held. Without JS the form posts at once.
  const DELAY_MS = 5000;
  const HELD_IN = "form[data-delayed-send], .receipt-pending";
  const held = [];  // {form, body, box, receipt, timer, left, tick, submitter, released}
  const heldOf = (target) => {
    const r = isEl(target) && target.closest(".receipt-pending");
    return r ? held.find((h) => h.receipt === r) : null;
  };
  const undo = (item) => {
    const i = held.indexOf(item);
    if (i < 0) return false;
    held.splice(i, 1);
    clearTimeout(item.timer);
    clearInterval(item.tick);
    item.receipt.remove();
    item.box.hidden = false;
    const back = (item.submitter && item.submitter.isConnected && item.submitter)
      || item.form.querySelector("input:not([type=hidden]), button[type=submit], button") || item.box;
    back.focus();
    return true;
  };
  const undoLatest = () => (held.length ? undo(held[held.length - 1]) : false);
  const settle = (item) => {
    // The answered question goes; a card with nothing left to answer (or a message's card) goes with it.
    const card = item.box.closest && item.box.closest("[data-decision]");
    if (item.box !== card) item.box.remove();
    if (card && (item.box === card || !card.querySelector(".question"))) card.remove();
  };
  // The held receipt keeps its own words and Undo button (item.words, item.undoButton): looking them up by selector
  // once " · Undo" follows the words found nothing (#45), threw before the post and left "… in 1 s · Undo" standing.
  // Whatever goes wrong from here on is said in place, the form comes back, and nothing is ever dropped silently.
  const failHeld = (item, text) => {
    const r = receiptEl(text, "err");
    if (item.receipt.isConnected) item.receipt.replaceWith(r); else item.box.before(r);
    item.box.hidden = false;
  };
  const refusal = (o, response) => o.err || (response.status >= 400
    ? "the dashboard answered " + response.status + (response.status === 401 ? " (signed out: open the link orch serve printed)" : "")
    : "nothing changed");
  const sendHeld = (item) => {
    const i = held.indexOf(item);
    if (i < 0) return;
    held.splice(i, 1);
    clearInterval(item.tick);
    try {
      item.words.textContent = item.label + " now";
      item.undoButton.remove();
      post(item.form, item.body).then((response) => {
        const o = outcome(response);
        if (!o.ok) { failHeld(item, "Not recorded: " + refusal(o, response)); return; }
        const text = (o.msg ? o.msg.charAt(0).toUpperCase() + o.msg.slice(1) : "Sent") + " · " + item.ticket;
        item.receipt.replaceWith(receiptEl(text + " · " + clock()));
        settle(item);
        keepReceipt(text + " · " + clock());
      }).catch((e) => failHeld(item, "Not recorded: could not reach the dashboard (" + ((e && e.message) || e) + "); nothing changed"));
    } catch (e) {
      failHeld(item, "Not recorded: " + ((e && e.message) || e) + "; nothing changed");
    }
  };
  // Capture phase, before any button sees the key: a repeat inside a held form or its receipt does nothing, and a
  // release there is what lets Undo act.
  document.addEventListener("keydown", (event) => {
    if (event.repeat && isEl(event.target) && event.target.closest(HELD_IN)) event.preventDefault();
  }, true);
  const releaseHeld = (event) => {
    const item = heldOf(event.target);
    if (item) item.released = true;
  };
  document.addEventListener("keyup", releaseHeld, true);
  document.addEventListener("pointerup", releaseHeld, true);
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!form.matches || !form.matches("form[data-delayed-send]") || !window.fetch || !window.URLSearchParams) return;
    event.preventDefault();
    const box = form.closest(".question") || form.closest("[data-decision]") || form;
    const item = { form, body: encode(form, event.submitter), box, label: form.dataset.delayedSend, left: DELAY_MS / 1000,
                   submitter: event.submitter, released: false,
                   ticket: (form.closest("[data-ticket]") || { dataset: {} }).dataset.ticket || "" };
    item.receipt = receiptEl(item.label + " in " + item.left + " s", "pending");
    item.words = item.receipt.lastChild;  // the words span, before " · Undo" is appended
    const button = document.createElement("button");
    item.undoButton = button;
    button.type = "button";
    button.className = "btn btn-quiet";
    button.textContent = "Undo";
    button.setAttribute("aria-keyshortcuts", "z");
    button.addEventListener("click", () => { if (item.released) undo(item); });
    item.receipt.append(" · ", button);
    box.before(item.receipt);
    box.hidden = true;
    button.focus();
    item.tick = setInterval(() => {
      item.left -= 1;
      if (item.left > 0) item.words.textContent = item.label + " in " + item.left + " s";
    }, 1000);
    item.timer = setTimeout(() => sendHeld(item), DELAY_MS);
    held.push(item);
  });
  window.addEventListener("pagehide", () => {
    while (held.length) {
      const item = held.shift();
      clearTimeout(item.timer);
      clearInterval(item.tick);
      try {
        fetch(item.form.action, { method: "POST", body: item.body, credentials: "same-origin", keepalive: true,
                                  headers: { "Content-Type": "application/x-www-form-urlencoded" } }).catch(() => {});
        keepReceipt(item.label + " as you left the page: check " + (item.ticket || "the ticket") + " that it arrived");
      } catch (e) {
        keepReceipt(item.label + " was not sent: the page closed first");
      }
    }
  });
  window.orchDelayedSend = { DELAY_MS, held, HELD_IN };  // read by the unit test

  // ---------- Keyboard (design system ShortcutOverlay): moves and arms, never commits a gate ----------
  // j/k move between decisions, 1–9 focus an answer option, a arms the primary (then Enter confirms), c opens
  // "Request changes…"/"Send back…", o opens the ticket, z undoes a held send, g t/b/a go to Today/Board/Activity,
  // ? lists the keys, Ctrl+K or ⌘K opens the command palette. Nothing fires while typing in a field, inside a dialog,
  // or when the workspace turned shortcuts off (Workspace & addons).
  const SHORTCUTS = [
    ["Next / previous decision", ["j", "k"]], ["Answer with option", ["1", "–", "9"]], ["Approve (then confirm)", ["a", "↵"]],
    ["Request changes or send back", ["c"]], ["Open ticket", ["o"]], ["Undo a held answer", ["z"]],
    ["Go to Today / Board / Activity", ["g", "t", "/", "b", "/", "a"]], ["Command palette", ["⌘K"]], ["This list", ["?"]],
  ];
  // A field you type in; a checkbox, radio or button input is not one (j/k still move from a ticked override box).
  const NOT_TYPED = new Set(["checkbox", "radio", "button", "submit", "reset", "file", "range", "color"]);
  const typing = (el) => Boolean(el && ((el.tagName === "INPUT" && !NOT_TYPED.has(el.type)) || ["TEXTAREA", "SELECT"].includes(el.tagName)
                                        || el.isContentEditable));
  const shortcutsOn = () => html.dataset.shortcuts !== "off";
  const visibleCards = () => [...document.querySelectorAll("[data-decision]")].filter((c) => !c.hidden && c.getClientRects().length);
  const currentCard = () => (isEl(document.activeElement) ? document.activeElement.closest("[data-decision]") : null);
  const say = (text) => {
    let live = document.getElementById("orch-key-live");
    if (!live) {
      live = document.createElement("p");
      live.id = "orch-key-live";
      live.className = "sr-only";
      live.setAttribute("aria-live", "polite");
      document.body.append(live);
    }
    live.textContent = text;
  };
  function focusCard(card) {
    if (!card) return;
    document.querySelectorAll("[data-decision].is-selected").forEach((c) => c.classList.remove("is-selected"));
    card.classList.add("is-selected");
    card.focus({ preventScroll: true });
    card.scrollIntoView({ block: "nearest", behavior: reduced ? "auto" : "smooth" });
  }
  const armPrimary = (card) => {
    const button = card.querySelector("[data-key-primary]");
    if (!button) return;
    button.focus();
    if (button.form && button.form.matches("form[data-inline-confirm]") && !button.form.dataset.armed) button.click();
    else say(button.textContent.trim() + ": press Enter");
  };
  const openChanges = (card) => {
    const summary = card.querySelector("[data-key-changes]");
    if (!summary) return;
    const details = summary.closest("details");
    details.open = true;
    const input = details.querySelector("input:not([type=hidden]), textarea");
    (input || summary).focus();
  };
  let gAt = 0;
  document.addEventListener("keydown", (event) => {
    if (!shortcutsOn() || event.defaultPrevented) return;
    if ((event.metaKey || event.ctrlKey) && !event.altKey && (event.key === "k" || event.key === "K")) {
      if (dialogOpen()) return;
      event.preventDefault();
      openPalette();
      return;
    }
    if (event.metaKey || event.ctrlKey || event.altKey || typing(event.target) || dialogOpen()) return;
    const key = event.key;
    if (gAt && Date.now() - gAt < 1500) {
      gAt = 0;
      const to = { t: "/", b: "/board", a: "/activity" }[key];
      if (to) { event.preventDefault(); go(to); }
      return;
    }
    if (key === "g") { gAt = Date.now(); return; }
    if (key === "?") { event.preventDefault(); openShortcuts(); return; }
    if (key === "z") { if (!event.repeat && undoLatest()) event.preventDefault(); return; }
    const cards = visibleCards();
    if (key === "j" || key === "k") {
      const link = document.querySelector(key === "j" ? "[data-key-next]" : "[data-key-prev]");
      if (link && cards.length <= 1) { event.preventDefault(); link.click(); return; }
      if (!cards.length) return;
      event.preventDefault();
      const i = cards.indexOf(currentCard());
      focusCard(i < 0 ? cards[0] : cards[key === "j" ? Math.min(cards.length - 1, i + 1) : Math.max(0, i - 1)]);
      return;
    }
    const card = currentCard() || (cards.length === 1 ? cards[0] : null);
    if (!card) return;
    if (/^[1-9]$/.test(key)) {
      const option = card.querySelector('[data-option="' + key + '"]');
      if (!option) return;
      event.preventDefault();
      option.focus();
      if (option.type === "checkbox") option.click();
      else say("Option " + key + ": press Enter to send it");
    } else if (key === "a") { event.preventDefault(); armPrimary(card); }
    else if (key === "c") { event.preventDefault(); openChanges(card); }
    else if (key === "o") {
      const link = card.querySelector("[data-open]");
      if (link) { event.preventDefault(); go(link.getAttribute("href")); }
    }
  });

  // The key list (?): a Dialog with every shortcut, one Close button that has focus; Esc closes, focus returns.
  let keysEl = null;
  function openShortcuts() {
    if (!window.HTMLDialogElement) return;
    const opener = document.activeElement;
    if (!keysEl) {
      keysEl = document.createElement("dialog");
      keysEl.className = "dialog-sheet overlay shortcut-overlay";
      keysEl.setAttribute("aria-labelledby", "orch-keys-title");
      const title = document.createElement("h2");
      title.id = "orch-keys-title";
      title.textContent = "Keyboard shortcuts";
      const list = document.createElement("dl");
      list.className = "kbd-list";
      SHORTCUTS.forEach(([what, keys]) => {
        const dt = document.createElement("dt");
        dt.textContent = what;
        const dd = document.createElement("dd");
        keys.forEach((k) => {
          if (k === "/" || k === "–") { dd.append(" " + k + " "); return; }
          const kbd = document.createElement("kbd");
          kbd.textContent = k;
          dd.append(kbd, " ");
        });
        list.append(dt, dd);
      });
      const note = document.createElement("p");
      note.className = "muted";
      note.textContent = "Never fires while typing in a field. 1–9 and a only arm; you confirm. Turn them off in Workspace & addons.";
      const close = document.createElement("button");
      close.type = "button";
      close.className = "btn";
      close.textContent = "Close";
      close.addEventListener("click", () => keysEl.close());
      keysEl.append(title, list, note, close);
      keysEl.addEventListener("close", () => { if (keysEl.opener && keysEl.opener.isConnected) keysEl.opener.focus(); });
      document.body.append(keysEl);
    }
    keysEl.opener = opener;
    keysEl.showModal();
    keysEl.querySelector("button").focus();
  }

  // ---------- Command palette (Ctrl+K / ⌘K): go to a page, a decision or a ticket ----------
  // Matching understands synonyms (accept, ok, sign off → approve). A decision opens its card and arms its primary;
  // nothing is ever committed from here.
  const SYNONYMS = { accept: "approve", ok: "approve", okay: "approve", lgtm: "approve", signoff: "approve", sign: "approve",
                     reply: "answer", respond: "answer", question: "answer", close: "verdict", done: "verdict",
                     kanban: "board", agents: "activity", log: "activity", settings: "workspace", addons: "workspace",
                     new: "new", create: "new" };
  const STOP = new Set(["off", "the", "a", "an", "to", "go"]);
  const tokens = (q) => q.toLowerCase().split(/[\s·,]+/).filter((t) => t && !STOP.has(t)).map((t) => SYNONYMS[t] || t);
  const matchCommands = (query, commands) => {
    const want = tokens(query || "");
    if (!want.length) return commands.slice(0, 12);
    return commands.filter((c) => {
      const hay = (c.label + " " + (c.words || "")).toLowerCase();
      return want.every((t) => hay.includes(t) || (/^\d+$/.test(t) && new RegExp("-0*" + t + "\\b").test(hay)));
    }).slice(0, 12);
  };
  window.orchPalette = { matchCommands, tokens };  // read by the unit test
  const GO_TO = [
    { label: "Go to Today", words: "today decisions home", href: "/", group: "Go to", hint: "g t" },
    { label: "Go to Board", words: "board tickets", href: "/board", group: "Go to", hint: "g b" },
    { label: "Go to Activity", words: "activity agents", href: "/activity", group: "Go to", hint: "g a" },
    { label: "Go to Reports", words: "reports", href: "/reports", group: "Go to" },
    { label: "Groom the backlog one by one", words: "groom backlog requirements", href: "/groom", group: "Go to" },
    { label: "Go to Workspace & addons", words: "workspace addons settings setup", href: "/workspace", group: "Go to" },
    { label: "New ticket", words: "new create ticket", href: "/new", group: "Go to" },
  ];
  let paletteEl = null;
  let paletteData = null;
  const loadPalette = () => {
    if (paletteData) return Promise.resolve(paletteData);
    return fetch("/palette.json", { credentials: "same-origin" }).then((r) => (r.ok ? r.json() : { decisions: [], tickets: [] }))
      .catch(() => ({ decisions: [], tickets: [] }))
      .then((data) => {
        paletteData = [
          ...data.decisions.map((d) => ({ label: d.label, words: d.words, href: d.href, card: d.card, group: "Decisions" })),
          ...GO_TO,
          ...data.tickets.map((t) => ({ label: t.id + " " + t.title, words: t.status, href: "/t/" + t.id, group: "Tickets" })),
        ];
        return paletteData;
      });
  };
  const runCommand = (c) => {
    paletteEl.close();
    const card = c.card && document.getElementById(c.card);
    if (card) { focusCard(card); armPrimary(card); return; }
    go(c.href);
  };
  function openPalette() {
    if (!window.HTMLDialogElement || !window.fetch) return;
    const opener = document.activeElement;
    if (!paletteEl) {
      paletteEl = document.createElement("dialog");
      paletteEl.className = "overlay palette";
      paletteEl.setAttribute("aria-label", "Command palette");
      const input = document.createElement("input");
      input.type = "text";  // not "search": Esc must close the palette, not clear the field
      input.className = "palette-input";
      input.setAttribute("role", "combobox");
      input.setAttribute("aria-controls", "palette-list");
      input.setAttribute("aria-expanded", "true");
      input.setAttribute("aria-autocomplete", "list");
      input.setAttribute("aria-label", "Go to a page, a decision or a ticket");
      input.placeholder = "Go to a page, a decision or a ticket";
      const list = document.createElement("ul");
      list.id = "palette-list";
      list.className = "palette-list";
      list.setAttribute("role", "listbox");
      const foot = document.createElement("p");
      foot.className = "palette-foot muted";
      foot.textContent = "Synonyms: accept, ok, sign off → approve. Human-only actions still confirm on their card.";
      paletteEl.append(input, list, foot);
      document.body.append(paletteEl);
      let shown = [];
      let at = 0;
      const render = () => {
        list.textContent = "";
        let group = "";
        shown.forEach((c, i) => {
          if (c.group !== group) {
            group = c.group;
            const h = document.createElement("li");
            h.className = "palette-group muted";
            h.setAttribute("role", "presentation");
            h.textContent = group;
            list.append(h);
          }
          const li = document.createElement("li");
          li.id = "palette-opt-" + i;
          li.className = "palette-opt";
          li.setAttribute("role", "option");
          li.setAttribute("aria-selected", String(i === at));
          li.textContent = c.label;
          if (c.hint) { const k = document.createElement("kbd"); k.textContent = c.hint; li.append(" ", k); }
          li.addEventListener("click", () => runCommand(c));
          list.append(li);
        });
        input.setAttribute("aria-activedescendant", shown.length ? "palette-opt-" + at : "");
      };
      const update = () => loadPalette().then((all) => { shown = matchCommands(input.value, all); at = 0; render(); });
      input.addEventListener("input", update);
      input.addEventListener("keydown", (event) => {
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          if (!shown.length) return;
          at = (at + (event.key === "ArrowDown" ? 1 : shown.length - 1)) % shown.length;
          render();
          const sel = document.getElementById("palette-opt-" + at);
          if (sel && sel.scrollIntoView) sel.scrollIntoView({ block: "nearest" });
        } else if (event.key === "Enter" && shown[at]) {
          event.preventDefault();
          runCommand(shown[at]);
        }
      });
      paletteEl.addEventListener("close", () => { if (paletteEl.opener && paletteEl.opener.isConnected) paletteEl.opener.focus(); });
      paletteEl.update = update;
    }
    paletteEl.opener = opener;
    const input = paletteEl.querySelector("input");
    input.value = "";
    paletteData = null;  // decisions change: read them again each time it opens
    paletteEl.showModal();
    input.focus();
    paletteEl.update();
  }

  // ---------- Start agent: one shared panel on Today ----------
  // A card's "Start agent…" swaps that ticket's panel into the one panel (GET /t/<id>/agent/panel) instead of each
  // card carrying its own; without JS the link reloads Today with ?start=<id>.
  document.addEventListener("click", (event) => {
    const link = isEl(event.target) && event.target.closest("[data-start-panel]");
    if (!link || !window.fetch || event.metaKey || event.ctrlKey || event.shiftKey || event.button) return;
    const box = document.getElementById("start-agent");
    if (!box) return;
    event.preventDefault();
    fetch("/t/" + encodeURIComponent(link.dataset.startPanel) + "/agent/panel?next=/", { credentials: "same-origin" })
      .then((r) => (r.ok ? r.text() : Promise.reject(new Error("panel"))))
      .then((text) => {
        box.innerHTML = text;  // server-rendered and escaped by the same templates as the page
        document.querySelectorAll("[data-start-panel]").forEach((a) => a.removeAttribute("aria-current"));
        link.setAttribute("aria-current", "true");
        const section = box.querySelector("section") || box;
        section.setAttribute("tabindex", "-1");
        section.focus();
      })
      .catch(() => { window.location = link.href; });
  });

  // Copy fix: the Workspace page's "Copy fix" buttons carry the shell command in data-copy.
  document.addEventListener("click", (event) => {
    const button = event.target.closest && event.target.closest("[data-copy]");
    if (!button || !navigator.clipboard || !navigator.clipboard.writeText) return;
    navigator.clipboard.writeText(button.dataset.copy).then(() => {
      const original = button.textContent;
      button.textContent = "Copied";
      setTimeout(() => { button.textContent = original; }, 1500);
    }).catch(() => {});
  });

  // Start agent: picking another harness or mode swaps in that combination's prompt and command
  // (all rendered by the server into .sa-options), so previews and copy buttons stay truthful.
  document.addEventListener("change", (event) => {
    const form = event.target.closest && event.target.closest("form[data-agent-start]");
    if (!form) return;
    const harness = form.elements.harness && form.elements.harness.value;
    const mode = form.elements.mode && form.elements.mode.value;
    const option = [...form.querySelectorAll(".sa-options li")]
      .find((li) => li.dataset.harness === harness && li.dataset.mode === mode);
    if (!option) return;
    ["prompt", "command", "launcher"].forEach((what) => {
      const pre = form.querySelector('[data-preview="' + what + '"]');
      if (pre) pre.textContent = option.dataset[what];
      const button = form.querySelector('[data-copy-of="' + what + '"]');
      if (button) button.dataset.copy = option.dataset[what];
    });
  });

  // ---------- Per-page setup, run on load and after a partial page swap ----------
  const init = (root) => {
    // Story chapters (ticket page): on wide screens every chapter starts open; phones keep the server's choice (the
    // current chapter open, the rest folded). `?open=all` opens everything without JS.
    if (window.matchMedia && window.matchMedia("(min-width: 900px)").matches) {
      root.querySelectorAll("details[data-wide-open]").forEach((d) => { d.open = true; });
    }
    // Paste screenshots into the new-ticket form.
    const pasteArea = root.querySelector("[data-paste-target]");
    if (pasteArea && window.DataTransfer) {
      const input = document.getElementById(pasteArea.dataset.pasteTarget);
      pasteArea.addEventListener("paste", (event) => {
        const images = [...(event.clipboardData?.files || [])].filter((f) => f.type.startsWith("image/"));
        if (!images.length || !input) return;
        const all = new DataTransfer();
        [...input.files, ...images].forEach((f) => all.items.add(f));
        input.files = all.files;
        event.preventDefault();
        const note = document.getElementById("paste-count");
        if (note) note.textContent = `${all.files.length} image(s) attached`;
      });
    }
    // <details data-remember="name">: closed by default (server-rendered); this browser remembers
    // whether it was opened. Storage may be off (private mode, blocked): then it just stays closed.
    root.querySelectorAll("details[data-remember]").forEach((details) => {
      const key = "orch-details:" + details.dataset.remember;
      try {
        if (window.localStorage.getItem(key) === "open") details.open = true;
      } catch (e) { /* no storage: keep the default */ }
      details.addEventListener("toggle", () => {
        try {
          window.localStorage.setItem(key, details.open ? "open" : "closed");
        } catch (e) { /* no storage: nothing to remember */ }
      });
    });
    showKeptReceipts();
    // A palette link from another page (/#d-<card>) lands on the card with its primary focused, never armed.
    const target = window.location && window.location.hash.startsWith("#d-") && document.getElementById(window.location.hash.slice(1));
    if (target && target.matches("[data-decision]")) {
      focusCard(target);
      const primary = target.querySelector("[data-key-primary]");
      if (primary) primary.focus();
    }
  };
  if (document.body && document.body.querySelectorAll) init(document);

  // ---------- Navigation feel: prefetch on hover, swap the page in place ----------
  // Menu and ticket links fetch the next page when hovered or focused, and a click swaps <main> and the menu in
  // place (history keeps working), so the live stream below stays connected. Anything else, a modifier click, or a
  // failed fetch is a normal page load.
  const SWAPPABLE = /^\/(?:|board|activity|reports|workspace|groom|t\/[A-Za-z0-9_-]+|addons\/[a-z][a-z0-9-]*\/)$/;
  const swappable = (a) => {
    if (!a || !a.href || a.target || a.hasAttribute("download") || a.hasAttribute("data-no-swap")) return false;
    const url = new URL(a.href, window.location.href);
    return url.origin === window.location.origin && SWAPPABLE.test(url.pathname) && !(url.pathname === window.location.pathname && url.hash && url.search === window.location.search);
  };
  const cache = new Map();  // url -> {at, page}
  const fetchPage = (url) => {
    const hit = cache.get(url);
    if (hit && Date.now() - hit.at < 10000) return hit.page;
    const page = fetch(url, { credentials: "same-origin" }).then((r) => {
      const final = new URL(r.url);
      if (!r.ok || !SWAPPABLE.test(final.pathname) || !(r.headers.get("content-type") || "").includes("text/html")) throw new Error("not swappable");
      return r.text().then((text) => ({ url: r.url, text }));
    });
    cache.set(url, { at: Date.now(), page });
    page.catch(() => cache.delete(url));
    return page;
  };
  const swapIn = (page, push) => {
    const next = new DOMParser().parseFromString(page.text, "text/html");
    const main = next.querySelector("main.content");
    const menu = next.querySelector("nav.menu");
    const oldMain = document.querySelector("main.content");
    const oldMenu = document.querySelector("nav.menu");
    if (!main || !menu || !oldMain || !oldMenu) throw new Error("no page");
    oldMain.replaceWith(document.adoptNode(main));
    oldMenu.replaceWith(document.adoptNode(menu));
    document.title = next.title;
    ["version", "shortcuts", "density"].forEach((k) => { if (next.documentElement.dataset[k] !== undefined) html.dataset[k] = next.documentElement.dataset[k]; });
    document.body.classList.remove("stale");
    renderedAt = Date.now();
    dirty = false;
    if (push) history.pushState({ orch: true }, "", page.url);
    const hash = new URL(page.url).hash;
    if (!hash) window.scrollTo(0, 0);
    init(main);
    const heading = main.querySelector("h1");
    if (heading && !(hash && document.getElementById(hash.slice(1)))) {
      heading.setAttribute("tabindex", "-1");
      heading.focus({ preventScroll: true });
    }
    cache.clear();
  };
  function go(href) {
    const a = document.createElement("a");
    a.href = href;
    if (!window.DOMParser || !window.fetch || !swappable(a)) { window.location.href = href; return; }
    fetchPage(a.href).then((page) => swapIn(page, true)).catch(() => { window.location.href = href; });
  }
  let hoverTimer = 0;
  const prefetch = (event) => {
    const a = isEl(event.target) && event.target.closest("a[href]");
    if (!a || !window.fetch || !swappable(a)) return;
    clearTimeout(hoverTimer);
    hoverTimer = setTimeout(() => fetchPage(a.href).catch(() => {}), 65);
  };
  document.addEventListener("mouseover", prefetch);
  document.addEventListener("focusin", prefetch);
  document.addEventListener("click", (event) => {
    const a = isEl(event.target) && event.target.closest("a[href]");
    if (event.defaultPrevented || event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    if (!a || !window.DOMParser || !window.fetch || !window.history || !swappable(a)) return;
    event.preventDefault();
    fetchPage(a.href).then((page) => swapIn(page, true)).catch(() => { window.location.href = a.href; });
  });
  window.addEventListener("popstate", () => {
    if (!window.DOMParser || !window.fetch) return;
    fetchPage(window.location.href).then((page) => swapIn({ ...page, url: window.location.href }, false))
      .catch(() => window.location.reload());
  });

  // ---------- Live refresh when tickets change (server-sent events from /events) ----------
  // Only the visible tab keeps a stream open: browsers allow 6 connections per host, so with a stream in every tab
  // the 7th tab (and every page load once 6 were open) hung. A tab coming back compares the version in the new
  // stream's hello frame with the one its page was rendered with. While an answer is held for Undo, or the human
  // types, the page only says that something changed.
  let dirty = false;
  if (window.EventSource) {
    document.addEventListener("input", (event) => { if (event.target.closest("form")) dirty = true; });
    const refresh = () => {
      // Terminals keep themselves live over their own stream: a reload would drop Type mode and the reply being typed.
      if (document.querySelector("[data-term], [data-term-grid]")) return;
      const active = document.activeElement;
      // Never swap the page under someone who is reading a decision (a selected or focused card): say so instead.
      const reading = currentCard() || document.querySelector("[data-decision].is-selected");
      if (dirty || typing(active) || held.length || dialogOpen() || reading || document.querySelector("form[data-armed]")) {
        document.body.classList.add("stale");
      }
      else if (window.DOMParser && window.fetch && SWAPPABLE.test(window.location.pathname)) {
        cache.clear();
        fetchPage(window.location.href).then((page) => swapIn({ ...page, url: window.location.href }, false))
          .catch(() => window.location.reload());
      } else window.location.reload();
    };
    // Debounced: an agent's burst of writes (and every open tab) refreshes once, 1.5 s after the last change.
    let timer = null;
    let source = null;
    const open = () => {
      if (source) return;
      source = new EventSource("/events");
      source.addEventListener("open", () => { lostAt = 0; showAge(); });
      source.addEventListener("error", () => { if (!lostAt) lostAt = Date.now(); });
      source.addEventListener("hello", (event) => {
        const seen = html.dataset.version;
        if (seen && event.data && event.data !== seen) refresh();
      });
      source.addEventListener("change", () => {
        clearTimeout(timer);
        timer = setTimeout(refresh, 1500);
      });
    };
    const close = () => {
      if (!source) return;
      source.close();
      source = null;
    };
    // The stream gone for a minute (server stopped, laptop slept): say how old the page is, quietly; the data stays.
    let lostAt = 0;
    const showAge = () => {
      const line = document.querySelector(".page-age");
      if (!line) return;
      const gone = lostAt && Date.now() - lostAt > 60000;
      line.hidden = !gone;
      if (gone) {
        const n = Math.round((Date.now() - renderedAt) / 60000);
        line.querySelector(".page-age-n").textContent = n < 60 ? n + " min" : Math.round(n / 60) + " h";
      }
    };
    setInterval(showAge, 30000);
    document.addEventListener("visibilitychange", () => (document.hidden ? close() : open()));
    window.addEventListener("pagehide", close);
    if (!document.hidden) open();
  }
})();
