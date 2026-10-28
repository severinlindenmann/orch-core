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
    workspaces: ["No workspaces yet", "Connect the first one with a one-time link. Ask your hub admin for a link, then open it in the workspace’s Mission Control (Settings › Hub › Connect)."],
    services: ["No services yet", "Ask your IT person to set up services on this hub, then check again."],
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
  const timer = setTimeout(dismiss, TOAST_DURATION);
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
    if (event.key === "Escape") { event.preventDefault(); dialog.remove(); }
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
