// Runs before the body renders: html.js lets the CSS hide the phone menu from the first paint,
// and the menu toggle lives here too, so the menu never ends up hidden without a working toggle.
document.documentElement.classList.add("js");
(() => {
  // Phone menu (below 900 px): the <details> is open in the HTML so the menu works without JS. With JS, the html.js
  // class above hides the items until "Menu" opens them in the <dialog> drawer: the page behind is inert, Tab stays
  // in the drawer, Esc or a click outside closes it, and focus returns to Menu. Where <dialog> is missing, Menu adds
  // .expanded instead and the items open inline. The listeners are delegated, so a menu swapped in by app.js works.
  const narrow = window.matchMedia ? window.matchMedia("(max-width: 900px)") : { matches: false };
  const sync = (menu) => {
    const toggle = menu && menu.querySelector(":scope > summary");
    if (!toggle) return;
    const drawer = menu.parentElement && menu.parentElement.querySelector("dialog.menu-drawer");
    if (narrow.matches) toggle.setAttribute("aria-expanded", String(menu.classList.contains("expanded") || Boolean(drawer && drawer.open)));
    else toggle.removeAttribute("aria-expanded");
  };
  const focusables = (root) => Array.from(root.querySelectorAll("a[href], button:not([disabled]), input, select, textarea, summary"))
    .filter((el) => el.offsetParent !== null || el === document.activeElement);
  const openDrawer = (menu, toggle) => {
    const drawer = menu.parentElement.querySelector("dialog.menu-drawer");
    const body = menu.querySelector(":scope > .menu-body");
    if (!drawer || typeof drawer.showModal !== "function" || !body) {
      menu.classList.toggle("expanded");
      sync(menu);
      return;
    }
    drawer.append(body);
    drawer.showModal();
    sync(menu);
    const current = drawer.querySelector("[aria-current=page]") || focusables(drawer)[0];
    if (current) current.focus();
    drawer.addEventListener("close", () => {
      menu.append(body);
      sync(menu);
      if (toggle.isConnected) toggle.focus();
    }, { once: true });
  };
  document.addEventListener("click", (event) => {
    const target = event.target;
    if (!target || !target.closest) return;
    const toggle = target.closest("details.menu-more > summary");
    if (toggle) {
      if (!narrow.matches) return;
      event.preventDefault();
      openDrawer(toggle.parentElement, toggle);
      return;
    }
    const drawer = target.closest("dialog.menu-drawer");
    if (!drawer) return;
    if (target.closest(".drawer-close")) { drawer.close(); return; }
    if (target !== drawer) return;
    // A click whose target is the dialog itself is on its padding or on the backdrop: only outside its box closes it.
    const r = drawer.getBoundingClientRect();
    if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) drawer.close();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Tab") return;
    const drawer = document.querySelector("dialog.menu-drawer[open]");
    if (!drawer) return;
    const items = focusables(drawer);
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (event.shiftKey && (document.activeElement === first || !drawer.contains(document.activeElement))) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && (document.activeElement === last || !drawer.contains(document.activeElement))) { event.preventDefault(); first.focus(); }
  });
  const onResize = () => {
    const drawer = document.querySelector("dialog.menu-drawer[open]");
    if (drawer && !narrow.matches) drawer.close();
    document.querySelectorAll("details.menu-more").forEach(sync);
  };
  if (narrow.addEventListener) narrow.addEventListener("change", onResize);
  document.addEventListener("DOMContentLoaded", onResize);
})();
