// The one confirm dialog (loaded in <head>, before app.js): every action that asks first uses it. Progressive
// enhancement only: without JS each form posts as it is and the server checks it (a Dark start still needs the word
// typed into the .nojs-only fields; data-dialog forms carry ask=1, so the server shows its confirm page first).
//
// Declarative API on a <form>:
//   data-confirm-title   the question ("Stop the run?")              data-confirm-body   one or two sentences
//   data-confirm-ok      the primary's specific verb ("Stop the run"), never a bare OK
//   data-confirm-items   JSON [{icon, text, tone}] for "What will happen" (tone "care" = the amber row)
//   data-confirm-stays   what stays the person's ("Reopen, with a reason")
//   data-confirm-tone="care"   outward or hard to undo: the dialog gets the calm amber look
//   data-confirm-field="name" (+ data-confirm-field-label): a required reason asked in the dialog, written into the
//                         form's input of that name (shown in a .nojs-only block without JS)
//   data-confirm-build="start": the dialog is built from the form's choices (New ticket, the epic's approve form)
// Inputs carrying data-confirm-word="<word>" get that word only after the person confirms; the server still requires
// it, so a forged POST without it is refused as before. The first submit is cancelled (and stopped before any other
// listener sees it), the dialog resolves a Promise, and a confirmed form is sent again with requestSubmit(), so an
// interceptor further on (receipts, in-place swaps) handles the confirmed submit as any other.
(() => {
  const root = document.documentElement;
  if (root && root.classList) root.classList.add("confirm-js");  // CSS hides .nojs-only from the first paint

  const PATHS = {
    lock: "M5 11h14v9H5zM8 11V8a4 4 0 0 1 7.5-2",
    clock: "M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18M12 7v5l3 2",
    up: "M12 19V5M6 11l6-6 6 6",
    undo: "M9 14L4 9l5-5M4 9h10a6 6 0 0 1 0 12h-3",
    flag: "M5 21V4M5 4h11l-2 4 2 4H5",
    hand: "M8 12V6a1.5 1.5 0 0 1 3 0v5M11 11V4.5a1.5 1.5 0 0 1 3 0V11M14 11V6a1.5 1.5 0 0 1 3 0v7c0 4-2.5 7-6 7-2.5 0-4-1.5-5-3.5L4 13a1.5 1.5 0 0 1 2.6-1.3L8 14",
    pause: "M9 5v14M15 5v14",
    check: "M5 12l5 5 9-10",
    refresh: "M20 11a8 8 0 1 0-2 6M20 4v7h-7",
    x: "M6 6l12 12M18 6L6 18",
  };
  const make = (tag, cls, text) => {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text !== undefined) el.textContent = text;
    return el;
  };
  const icon = (name) => {
    const NS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS(NS, "path");
    path.setAttribute("d", PATHS[name] || PATHS.check);
    svg.append(path);
    return svg;
  };

  // confirmDialog(cfg) -> Promise<{ok, value}>. A native modal <dialog>: role dialog and aria-modal come with it,
  // the page behind is inert and focus stays inside. Cancel has focus first, a required reason field instead where
  // there is one; Esc and a click on the backdrop cancel; focus returns to the opener.
  let uid = 0;
  const confirmDialog = (cfg) => new Promise((resolve) => {
    const id = "confirm-" + (++uid);
    const d = make("dialog", "confirm-dialog" + (cfg.tone === "care" ? " is-care" : ""));
    d.setAttribute("aria-labelledby", id + "-t");
    const box = make("div", "cd-in");
    const title = make("h2", "", cfg.title);
    title.id = id + "-t";
    box.append(title);
    if (cfg.body) {
      const body = make("p", "cd-sum", cfg.body);
      body.id = id + "-d";
      d.setAttribute("aria-describedby", id + "-d");
      box.append(body);
    }
    const items = (cfg.items || []).filter((i) => i && i.text);
    if (items.length) {
      box.append(make("h3", "cd-head", cfg.itemsHead || "What will happen"));
      const ul = make("ul", "cd-items");
      items.forEach((i) => {
        const li = make("li", i.tone === "care" ? "is-care" : "");
        li.append(icon(i.icon), make("span", "", i.text));
        ul.append(li);
      });
      box.append(ul);
    }
    if (cfg.stays) {
      const p = make("p", "cd-stays");
      p.append(make("b", "", "What stays yours: "), cfg.stays);
      box.append(p);
    }
    let field = null;
    if (cfg.field) {
      const wrap = make("div", "cd-field field-col");
      const label = make("label", "", cfg.field.label || "Reason (required)");
      label.setAttribute("for", id + "-f");
      field = make("textarea");
      field.id = id + "-f";
      field.rows = 3;
      field.required = true;
      if (cfg.field.maxlength) field.maxLength = cfg.field.maxlength;
      wrap.append(label, field);
      box.append(wrap);
    }
    const acts = make("div", "cd-acts");
    const cancel = make("button", "btn", cfg.cancel || "Cancel");
    cancel.type = "button";
    const ok = make("button", "btn btn-primary", cfg.ok);
    ok.type = "button";
    acts.append(cancel, ok);
    box.append(acts);
    d.append(box);
    document.body.append(d);
    const opener = document.activeElement;
    let done = false;
    const close = (yes) => {
      if (done) return;
      done = true;
      const value = field ? field.value.trim() : "";
      if (d.open && d.close) d.close();
      d.remove();
      if (opener && opener.isConnected && opener.focus) opener.focus();
      resolve({ ok: yes, value });
    };
    const sync = () => { ok.disabled = Boolean(field && !field.value.trim()); };
    if (field) field.addEventListener("input", sync);
    sync();
    cancel.addEventListener("click", () => close(false));
    ok.addEventListener("click", () => { if (!ok.disabled) close(true); });
    d.addEventListener("cancel", (event) => { event.preventDefault(); close(false); });  // Esc
    d.addEventListener("click", (event) => { if (event.target === d) close(false); });  // the backdrop (no padding on d)
    d.showModal();
    (field || cancel).focus();
  });

  // ---------- The Dark / AI Factory start, built from the form's choices ----------
  const val = (form, name) => {
    const x = form.elements && form.elements[name];
    return x && typeof x.value === "string" ? x.value : "";
  };
  const ticked = (form, name) => {
    const x = form.elements && form.elements[name];
    return Boolean(x && x.checked && !x.disabled);
  };
  const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);
  const RELEASE = {
    none: { icon: "up", text: "Nothing is released: the run only builds and proves" },
    merge: { icon: "up", text: "Merges each child by itself with the recipe on this machine; nothing goes to production" },
    dev: { icon: "up", text: "Merges, then deploys dev by itself with the recipe on this machine; nothing goes to production" },
    prod: { icon: "up", tone: "care", text: "Releases to production by itself (merge, dev, then production) with the recipe on this machine, never before its release window opens" },
  };
  const startConfig = (form) => {
    const isNew = "newForm" in form.dataset;
    const start = isNew ? val(form, "mode") : val(form, "start");
    const limits = form.dataset.factoryConfirm || "";
    const epic = form.dataset.confirmEpic || "this epic";
    const again = "confirmReapprove" in form.dataset;
    if (start !== "dark" && start !== "factory") {
      if (isNew) return null;  // Ticket mode posts at once
      const on = ticked(form, "delegate");
      return {
        title: again ? "Re-approve the epic?" : "Approve the epic?",
        body: "You sign " + epic + ", exactly as shown above.",
        items: [on ? { icon: "hand", text: "Agents may approve up to " + val(form, "max_children") + " children they add, of size " + val(form, "max_size") + " or smaller" }
                   : { icon: "check", text: "No delegation: every child waits for your approval" }],
        stays: "Pausing the delegation, Request changes, and every verdict.",
        ok: again ? "Re-approve epic" : "Approve epic",
      };
    }
    const dark = start === "dark";
    const items = [dark ? { icon: "lock", text: "Sessions run without permission prompts: a command outside the Dark profile is denied and becomes a card" }
                        : { icon: "hand", text: "A command outside your grants waits for you on a card" }];
    if (limits) items.push({ icon: "clock", text: cap(limits) });
    let release = "none";
    let rollback = false;
    let close = false;
    if (dark) {
      release = (form.querySelector && form.querySelector("[data-release-choice]") && val(form, "release")) || "none";
      if (!RELEASE[release]) release = "none";
      rollback = release === "prod" && ticked(form, "rollback");
      close = ticked(form, "close");
      items.push(RELEASE[release]);
      if (release === "prod") items.push(rollback ? { icon: "undo", tone: "care", text: "Runs the recipe's rollback by itself if the production check fails" }
                                                  : { icon: "undo", text: "No rollback by itself if the production check fails" });
    } else items.push(RELEASE.none);
    items.push(close
      ? { icon: "flag", tone: "care", text: "Closes the epic by itself when everything is proven, in place of your verdict. It goes by what the agents wrote: nothing is executed or verified by the factory, coverage is text mentions only, and an epic that names no file is not closed by itself" + (release === "none" ? "; with no release nothing is deployed or run" : "") }
      : { icon: "flag", text: "You give the verdict" });
    const name = dark ? "Dark AI Factory" : "AI Factory";
    const what = isNew ? "creates the epic “" + (val(form, "title").trim() || "untitled") + "” and starts the " + name + " on it"
                       : "signs " + epic + ", exactly as shown above, and starts the " + name + " on it";
    return {
      title: dark ? "Start this Dark run?" : "Start this AI Factory run?",
      body: "This " + what + ".",
      items,
      stays: "Answering cards" + (dark ? ", larger children" : "") + (close ? "" : ", the verdict") + ", Stop the run" + (dark ? " and Reopen." : "."),
      ok: release === "prod" ? "Start and release to production" : dark ? "Start Dark run" : "Start AI Factory",
      tone: release === "prod" || rollback || close ? "care" : "",
    };
  };

  const configFromForm = (form, submitter) => {
    const ds = form.dataset;
    if (ds.confirmBuild === "start") return startConfig(form);
    if (!ds.confirmTitle) return null;
    let items = [];
    try { items = JSON.parse(ds.confirmItems || "[]"); } catch (e) { items = []; }
    let field = null;
    if (ds.confirmField) {
      const input = form.elements && form.elements[ds.confirmField];
      field = { name: ds.confirmField, label: ds.confirmFieldLabel, maxlength: input && input.maxLength > 0 ? input.maxLength : 0 };
    }
    const fallback = ((submitter && submitter.textContent) || "").replace(/…\s*$/, "").trim();
    return { title: ds.confirmTitle, body: ds.confirmBody || "", ok: ds.confirmOk || fallback || "Go ahead", items,
             stays: ds.confirmStays || "", tone: ds.confirmTone || "", field, cancel: ds.confirmCancel || "" };
  };

  const words = (form) => (form.querySelectorAll ? [...form.querySelectorAll("[data-confirm-word]")] : []);
  // What the person agreed to, written into the form just before it is sent again.
  const applyConfirmed = (form, cfg, result) => {
    words(form).forEach((input) => { input.value = input.dataset.confirmWord; });
    if (cfg.field) {
      let input = form.elements[cfg.field.name];
      if (!input) {
        input = make("input");
        input.type = "hidden";
        input.name = cfg.field.name;
        form.append(input);
      }
      input.value = result.value;
    }
    const ask = form.querySelector && form.querySelector('input[name="ask"]');
    if (ask) ask.value = "";  // the server's no-JS confirm page is not needed: the dialog asked
  };
  const clearWords = (form) => words(form).forEach((input) => { input.value = ""; });

  // A required field kept for the no-JS path is hidden with JS: it must not block the browser's own validation.
  const relax = (form) => {
    if (!form || !form.querySelectorAll || !form.matches || !form.matches("form[data-confirm-title], form[data-confirm-build]")) return;
    form.querySelectorAll(".nojs-only [required]").forEach((x) => { x.required = false; x.dataset.nojsRequired = "1"; });
  };
  document.addEventListener("click", (event) => {
    const b = event.target && event.target.closest && event.target.closest("button, input[type=submit]");
    if (b && b.form) relax(b.form);
  }, true);

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!form || !form.matches || !form.matches("form[data-confirm-title], form[data-confirm-build]")) return;
    if (form.dataset.confirmed) {  // the confirmed submit: on its way, as any other
      delete form.dataset.confirmed;
      return;
    }
    const submitter = event.submitter;
    const cfg = configFromForm(form, submitter);
    if (!cfg || !window.HTMLDialogElement) return;  // nothing to ask, or no <dialog>: the server checks it as without JS
    event.preventDefault();
    event.stopImmediatePropagation();
    clearWords(form);
    confirmDialog(cfg).then((result) => {
      if (!result.ok) return;
      applyConfirmed(form, cfg, result);
      form.dataset.confirmed = "1";
      if (form.requestSubmit) form.requestSubmit(submitter && submitter.form === form ? submitter : undefined);
      else { delete form.dataset.confirmed; form.submit(); }
    });
  }, true);
  // Back from the cache never finds a typed word already filled in.
  window.addEventListener("pageshow", () => {
    document.querySelectorAll("form[data-confirm-build], form[data-confirm-title]").forEach(clearWords);
  });

  window.orchConfirm = { confirmDialog, configFromForm, startConfig, applyConfirmed };
})();
