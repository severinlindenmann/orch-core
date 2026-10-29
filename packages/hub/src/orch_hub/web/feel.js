// Shared presentation helpers. No workspace strings are interpreted as markup.
import { h } from "./ui.js";

export const PROGRESS_DELAY = 120;
export const TOAST_DURATION = 4000;
export function skeletonPlan(kind) {
  if (kind === "home") return { grid: "home-grid", sections: [4, 2, 2] };
  if (kind === "workspaces") return { grid: "work-grid", sections: [3, 3] };
  if (kind === "services") return { grid: "", sections: [3] };
  return { grid: "", sections: [4] };
}
export function emptyWords(kind) {
  return ({
    home: ["Nothing needs you", "All approvals are done. New ones show up here by themselves."],
    workspaces: ["No workspaces yet", "Press “Connect a workspace”, copy the link and open it on the workspace's own dashboard, under Settings › Hub."],
    "workspaces-member": ["No workspaces yet", "Ask your hub admin for a link, then open it on the workspace's own dashboard, under Settings › Hub."],
    account: ["No phones or browsers yet", "To add a phone, open the phone app and choose Pair a phone."],
    services: ["No services yet", "Ask your IT person to set up services on this hub, then check again."],
    handoffs: ["No hand-offs yet", "Send a hand-off from a workspace to see its progress here."],
    mcp: ["No tool servers yet", "Add one below to offer it to your workspaces."],
    skills: ["No company skills yet", "Write one below to offer it to your workspaces."],
    spaces: ["No shared spaces yet", "A shared space is a place where several workspaces keep documents together. Press “New shared space” to make one."],
    "published-links": ["No shared files yet", "When someone in a workspace shares a file by link, it shows up here."],
    "published-apps": ["No apps yet", "Apps and pages that people publish from a workspace show up here."],
    "addon-requests": ["No requests", "When a workspace is refused an add-on, the request shows up here."],
    search: ["Nothing found", "Try another name or a shorter search."],
  })[kind] ?? ["Nothing here yet", "Check again later."];
}
export function emptyState(kind) {
  const [title, next] = emptyWords(kind);
  return h("div", { class: "empty" }, h("b", { text: title }), h("div", { class: "small", text: next }));
}
export function skeleton(kind = "page") {
  const plan = skeletonPlan(kind);
  const bar = (cls = "") => h("div", { class: "sk " + cls });
  if (kind === "workspace") return h("div", { class: "skeleton", role: "status", "aria-label": "Loading", "aria-busy": "true" },
    h("div", { "aria-hidden": "true" }, bar("sk-title"), bar("sk-lead"), bar("sk-lead"), bar("sk-button")));
  const cards = plan.sections.map((count) => h("section", { class: "card" }, bar("sk-heading"),
    Array.from({ length: count }, () => h("div", { class: kind === "services" ? "svc-row" : "list-row row" },
      bar("sk-icon"), h("div", { class: "grow" }, bar(), bar("sk-short")), bar("sk-button")))));
  const body = kind === "home" ? [cards[0], h("div", { class: "home-side" }, cards.slice(1))] : cards;
  return h("div", { class: "skeleton", role: "status", "aria-label": "Loading", "aria-busy": "true" },
    h("div", { "aria-hidden": "true" }, bar("sk-title"), bar("sk-lead"), h("div", { class: plan.grid }, body)));
}

// Reference counting keeps overlapping reads from hiding each other's progress.
export function createLoadingController(show, hide, schedule = setTimeout, cancel = clearTimeout) {
  let count = 0, timer;
  return () => {
    if (++count === 1) timer = schedule(show, PROGRESS_DELAY);
    let finished = false;
    return () => {
      if (finished) return;
      finished = true;
      if (--count === 0) { cancel(timer); hide(); }
    };
  };
}
let beginProgress;
export function loading() {
  if (!beginProgress) {
    const bar = h("div", { id: "pbar", hidden: true, role: "progressbar", "aria-label": "Loading" });
    document.body.append(bar);
    beginProgress = createLoadingController(() => { bar.hidden = false; }, () => { bar.hidden = true; });
  }
  return beginProgress();
}
export function enter(content) {
  content.classList.remove("enter");
  void content.offsetWidth;
  content.classList.add("enter");
}
let dismissToast;
export function toast(msg, { undo } = {}) {
  dismissToast?.();
  let region = document.getElementById("toast-region");
  if (!region) {
    region = h("div", { id: "toast-region", "aria-live": "polite", "aria-atomic": "true" });
    document.body.append(region);
  }
  const el = h("div", { class: "toast" }, h("span", { text: msg }));
  const dismiss = () => { clearTimeout(timer); el.remove(); };
  if (typeof undo === "function") el.append(h("button", { text: "Undo", onclick: () => { dismiss(); undo(); } }));
  region.append(el);
  const timer = setTimeout(dismiss, typeof undo === "function" ? UNDO_DURATION : TOAST_DURATION);
  dismissToast = dismiss;
  return dismiss;
}

export function focusIndex(index, length, backwards) {
  if (!length) return -1;
  return (index + (backwards ? -1 : 1) + length) % length;
}
export function trapDialog(dialog, restore = document.activeElement) {
  const controls = () => [...dialog.querySelectorAll('button, a[href], input, select, textarea, [tabindex]')]
    .filter((el) => !el.disabled && el.tabIndex >= 0 && !el.closest('[hidden], [inert]') && el.getClientRects().length);
  dialog.tabIndex = -1;
  const heading = dialog.querySelector("h1, h2, b");
  if (heading && !dialog.hasAttribute("aria-label") && !dialog.hasAttribute("aria-labelledby")) {
    heading.id ||= "feel-dialog-title";
    dialog.setAttribute("aria-labelledby", heading.id);
  }
  const focus = () => (controls()[0] ?? dialog).focus();
  const key = (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      const cancel = [...dialog.querySelectorAll("button")].find((button) =>
        !button.disabled && (button.hasAttribute("data-cancel") || button.textContent.trim() === "Cancel"));
      if (cancel) cancel.click();
      else if (dialog.dispatchEvent(new Event("cancel", { cancelable: true }))) {
        if (typeof dialog.close === "function") dialog.close();
        else dialog.remove();
      }
    }
    if (event.key === "Tab") {
      event.preventDefault();
      const items = controls(), index = items.indexOf(document.activeElement);
      (items[index < 0 ? (event.shiftKey ? items.length - 1 : 0) : focusIndex(index, items.length, event.shiftKey)] ?? dialog).focus();
    }
  };
  const contain = (event) => { if (!dialog.contains(event.target)) focus(); };
  dialog.addEventListener("keydown", key);
  document.addEventListener("focusin", contain);
  focus();
  return () => {
    dialog.removeEventListener("keydown", key);
    document.removeEventListener("focusin", contain);
    if (restore?.isConnected) restore.focus();
  };
}
// Observe the shared dialog surface without changing any signing/approval handlers.
export function watchDialogs() {
  let current, release;
  const observer = new MutationObserver(() => {
    const next = document.querySelector('[role="dialog"]');
    if (next === current) return;
    release?.();
    current = next;
    release = next ? trapDialog(next) : null;
  });
  observer.observe(document.body, { childList: true, subtree: true });
  return () => { observer.disconnect(); release?.(); };
}

// Preserve editing state across a refresh of the same view, without retaining old handlers.
export function replaceContent(container, nodes, preserve = true) {
  const selector = "input, select, textarea, button, a[href], summary, details, [tabindex]";
  const keyed = () => {
    const counts = new Map();
    return new Map([...container.querySelectorAll(selector)].map((el) => {
      const base = [el.tagName, el.id, el.getAttribute("name"), el.getAttribute("type"),
        el.getAttribute("href"), el.tagName === "BUTTON" ? el.textContent : ""].join("|");
      const n = counts.get(base) ?? 0;
      counts.set(base, n + 1);
      return [base + "|" + n, el];
    }));
  };
  const previous = preserve ? keyed() : new Map();
  const focused = document.activeElement;
  container.replaceChildren(...nodes);
  for (const [key, el] of keyed()) {
    const old = previous.get(key);
    if (!old) continue;
    if (["INPUT", "SELECT", "TEXTAREA"].includes(el.tagName)) {
      el.value = old.value;
      if (el.tagName === "INPUT") el.checked = old.checked;
    }
    if (el.tagName === "DETAILS") el.open = old.open;
    if (old === focused) {
      el.focus();
      if (typeof old.selectionStart === "number") el.setSelectionRange?.(old.selectionStart, old.selectionEnd, old.selectionDirection);
    }
  }
}
// Pure interaction rules, shared by the driver and node tests.
export const UNDO_DURATION = 5000;
export const retryDelay = (attempt) => Math.min(15000, 2000 * 2 ** Math.max(0, attempt));
export const itemKey = (item) => JSON.stringify([item.workspace_id, item.item_id ?? item.key]);
export function searchEntries({ admin = false, workspaces = [], items = [], people = [], shared = [] }) {
  const pages = [["Home", "home"], ["Workspaces", "workspaces"], ...(admin ? [["Services", "services"], ["Admin", "admin"]] : [])];
  return [
    ...pages.map(([label, page]) => ({ group: "Pages", label, hash: "#/" + page })),
    ...workspaces.map((w) => ({ group: "Workspaces", label: w.name || w.id, hash: "#/w/" + encodeURIComponent(w.id) })),
    ...items.map((it) => ({ group: "Tickets and items", label: [it.key, it.title ?? it.kind].filter(Boolean).join(" · "), hash: "#/w/" + encodeURIComponent(it.workspace_id) + "/item/" + encodeURIComponent(it.key || it.item_id) })),
    ...shared.map((it) => ({ group: "Shared tickets", label: [it.key, it.title].filter(Boolean).join(" · ") + (it.shared_by ? " · shared by " + it.shared_by : ""), hash: "#/w/" + encodeURIComponent(it.workspace_id) })),
    ...(admin ? people.map((p) => ({ group: "People", label: p.name || p.user_id, hash: "#/admin" })) : []),
    ...(admin ? [{ group: "Actions", label: "Connect a workspace", hash: "#/admin", action: "connect" }] : []),
  ];
}
export function filterSearch(entries, query) {
  const words = query.trim().toLocaleLowerCase().split(/\s+/);
  return entries.filter((e) => words.every((word) => e.label.toLocaleLowerCase().includes(word)));
}
export function shortcut(key, { typing = false, meta = false, ctrl = false, alt = false, prefix = false, admin = false } = {}) {
  if (typing || alt) return null;
  if (meta || ctrl) return key.toLowerCase() === "k" ? "search" : null;
  if (key === "/") return "search";
  if (key === "?") return "shortcuts";
  if (prefix) return ({ h: "home", w: "workspaces", ...(admin ? { s: "services", a: "admin" } : {}) })[key] ?? null;
  return key === "g" ? "prefix" : null;
}
export async function busyButton(button, action) {
  if (button.disabled || button.getAttribute("aria-busy") === "true") return;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  try { return await action(); }
  finally { button.removeAttribute("aria-busy"); if (button.arm) button.arm(); else button.disabled = false; }
}
export function slideOut(row) {
  if (row.classList.contains("row-done")) return;
  row.classList.add("row-done");
  setTimeout(() => {
    const rect = row.getBoundingClientRect();
    row.remove();
    if (rect.bottom < 0) window.scrollBy(0, -rect.height);
  }, 220);
}

// Retain matching nodes (and their focus/listeners) while updating read-only lists.
export function reconcileRows(target, source) {
  const old = [...target.children];
  const keyed = new Map(old.filter((el) => el.dataset.liveKey).map((el) => [el.dataset.liveKey, el]));
  const used = new Set();
  [...source.children].forEach((fresh, index) => {
    const key = fresh.dataset.liveKey;
    const existing = key ? keyed.get(key) : old[index];
    if (existing && existing.tagName === fresh.tagName && !used.has(existing) && (!existing.dataset.liveKey || key)) {
      used.add(existing);
      for (const attr of fresh.attributes) if (attr.name !== "class" || !existing.classList.contains("row-new")) existing.setAttribute(attr.name, attr.value);
      const oldText = [...existing.childNodes].filter((n) => n.nodeType === 3);
      const newText = [...fresh.childNodes].filter((n) => n.nodeType === 3);
      if (oldText.length === newText.length) oldText.forEach((n, i) => { if (n.textContent !== newText[i].textContent) n.textContent = newText[i].textContent; });
      if (existing.children.length || fresh.children.length) reconcileRows(existing, fresh);
      else if (existing.textContent !== fresh.textContent) existing.textContent = fresh.textContent;
    } else {
      target.append(fresh);
      for (const row of fresh.matches("article[data-live-key]") ? [fresh] : fresh.querySelectorAll("article[data-live-key]")) {
        row.classList.add("row-new");
        row.append(h("span", { class: "new-mark", text: "New" }));
        setTimeout(() => { row.classList.remove("row-new"); row.querySelector(".new-mark")?.remove(); }, 2000);
      }
    }
  });
  for (const el of old) if (!used.has(el)) {
    // A focused row remains until focus leaves, so polling never steals focus.
    if (el.contains(document.activeElement)) el.addEventListener("focusout", () => { queueMicrotask(() => { if (!el.contains(document.activeElement)) slideOut(el); }); }, { once: true });
    else slideOut(el);
  }
}

export function shouldPoll({ now, due, visible, active, busy }) {
  return visible && active && !busy && now >= due;
}
