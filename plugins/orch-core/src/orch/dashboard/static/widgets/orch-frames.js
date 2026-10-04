/* Host side of agent-HTML widgets (docs/widgets.md, "The frame"). Loaded by the dashboard with script-src 'self'.
   Finds <div class="w-frame" data-doc-url data-nonce data-min-height>, and when it scrolls near the viewport loads
   <iframe sandbox="allow-scripts" src=doc-url>. The document carries its own CSP header and the same nonce in
   <meta name="orch-frame">. Messages are {orch: 1, kind, frame: nonce}; one only counts when it comes from that
   frame's window with that nonce. The frame's text alternative goes into the chrome's text slot (.w-alt .w-text,
   the Show text toggle; .w-frame-text inside the placeholder where there is no chrome) and bubbles as the "orch:widget-text" event (detail.text) for the chrome around it. */
(function () {
  "use strict";
  if (window.orchFrames) return;  // loaded twice (a page that draws several tickets' widgets): one host is enough
  var READY_MS = 3000;
  var MAX_HEIGHT = 4000;
  var TICKET = /^[A-Z][A-Z0-9]*-\d+$/;
  var ANCHOR = /^#w-[a-z0-9-]{1,40}$/;
  var html = document.documentElement;
  var media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  var live = [];  // {el, iframe, nonce, timer, text, ready}

  function isDark() {
    var t = html.getAttribute("data-theme");
    return t === "dark" || (t !== "light" && !!(media && media.matches));
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text) n.textContent = text;
    return n;
  }

  function parts(box) {
    if (!box._w) {
      var status = el("p", "w-frame-status", "Loading…");
      status.setAttribute("role", "status");
      // The chrome's text slot (<details class="w-alt"><pre class="w-text">) when the frame stands in one: the
      // Show text toggle and the frame's posted text share it. Elsewhere the placeholder keeps its own.
      var fig = box.closest ? box.closest("figure.w") : null;
      var slot = fig ? fig.querySelector(".w-alt .w-text") : null;
      var text = slot || box.querySelector(".w-frame-text") || el("div", "w-frame-text");
      if (!slot) text.hidden = true;
      var bar = el("div", "w-frame-ctl");
      var button = el("button", "w-frame-btn btn btn-quiet");
      button.type = "button";
      bar.appendChild(button);
      box.appendChild(status);
      box.appendChild(bar);
      if (!text.parentNode) box.appendChild(text);
      box._w = { status: status, text: text, details: slot ? slot.closest("details") : null, button: button,
                 rec: null, lastText: "" };
      button.addEventListener("click", function () {
        if (box._w.rec) stop(box, "Stopped. Showing the text alternative."); else start(box);
      });
    }
    return box._w;
  }

  function setState(box, state, message) {
    var w = parts(box);
    box.setAttribute("data-state", state);
    w.status.textContent = message || "";
    w.status.hidden = !message;
    w.button.textContent = w.rec ? "Stop" : state === "idle" ? "Run" : "Run again";
    var showText = state === "text" || state === "error";
    if (w.details) {  // opened for the reader when the frame stops; closed again only if we opened it
      if (showText && !w.details.open) { w.details.open = true; w.opened = true; }
      else if (state === "ready" && w.opened) { w.details.open = false; w.opened = false; }
    }
    else w.text.hidden = !showText;
    if (showText && !w.text.textContent) w.text.textContent = w.lastText || "No text alternative given.";
  }

  function start(box) {
    var w = parts(box);
    var url = box.getAttribute("data-doc-url");
    var nonce = box.getAttribute("data-nonce");
    if (!url || !nonce || w.rec) return;
    var iframe = document.createElement("iframe");
    iframe.setAttribute("sandbox", "allow-scripts");
    iframe.setAttribute("referrerpolicy", "no-referrer");
    iframe.setAttribute("title", box.getAttribute("data-title") || "Widget");
    iframe.className = "w-frame-iframe";
    var min = parseInt(box.getAttribute("data-min-height"), 10) || 160;
    iframe.style.height = min + "px";
    iframe.src = url;
    var rec = { box: box, iframe: iframe, nonce: nonce, min: min, ready: false };
    rec.timer = setTimeout(function () {
      if (!rec.ready) stop(box, "The widget did not start within 3 s. Showing the text alternative.");
    }, READY_MS);
    // The document loads once. A second load is the frame navigating itself (a link, a form, location=…; the sandbox
    // cannot forbid that without navigate-to): whatever it loaded is not the widget, so the frame goes.
    rec.loads = 0;
    iframe.addEventListener("load", function () {
      rec.loads += 1;
      if (rec.loads > 1) stop(box, "The widget tried to leave the page. Showing the text alternative.", "error");
      else send(rec, "theme", { dark: isDark() });
    });
    w.rec = rec;
    live.push(rec);
    box.insertBefore(iframe, w.status);
    setState(box, "loading", "Loading…");
  }

  function stop(box, message, state) {
    var w = parts(box);
    var rec = w.rec;
    if (rec) {
      clearTimeout(rec.timer);
      rec.iframe.remove();
      live = live.filter(function (r) { return r !== rec; });
      w.rec = null;
    }
    setState(box, state || "text", message);
  }

  function send(rec, kind, extra) {
    if (!rec.iframe.contentWindow) return;
    var msg = { orch: 1, kind: kind, frame: rec.nonce };
    for (var k in extra) msg[k] = extra[k];
    rec.iframe.contentWindow.postMessage(msg, "*");  // opaque sandbox origin: "*" is the only target
  }

  window.addEventListener("message", function (event) {
    var m = event.data;
    if (!m || m.orch !== 1 || typeof m.kind !== "string") return;
    var rec = null;
    for (var i = 0; i < live.length; i++) if (live[i].iframe.contentWindow === event.source) rec = live[i];
    if (!rec || m.frame !== rec.nonce) return;
    var box = rec.box, w = parts(box);
    if (m.kind === "ready") {
      rec.ready = true;
      clearTimeout(rec.timer);
      setState(box, "ready", "");
    } else if (m.kind === "resize") {
      var h = Math.min(MAX_HEIGHT, Math.max(rec.min, Math.ceil(Number(m.height) || 0)));
      rec.iframe.style.height = h + "px";
    } else if (m.kind === "text") {
      w.lastText = String(m.text || "").slice(0, 20000);
      w.text.textContent = w.lastText;
      box.dispatchEvent(new CustomEvent("orch:widget-text", { bubbles: true, detail: { text: w.lastText } }));
    } else if (m.kind === "error") {
      stop(box, "The widget reported an error: " + String(m.message || "").slice(0, 500), "error");
    } else if (m.kind === "open") {
      var ref = String(m.ref || "");
      if (TICKET.test(ref)) window.location.assign("/t/" + ref);
      else if (ANCHOR.test(ref)) window.location.hash = ref;
    }
  });

  function themeAll() {
    var dark = isDark();
    live.forEach(function (rec) { send(rec, "theme", { dark: dark }); });
  }
  new MutationObserver(themeAll).observe(html, { attributes: true, attributeFilter: ["data-theme"] });
  if (media && media.addEventListener) media.addEventListener("change", themeAll);

  function init(scope) {
    var boxes = (scope || document).querySelectorAll(".w-frame[data-doc-url]:not([data-state])");
    var io = window.IntersectionObserver ? new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) { io.unobserve(e.target); start(e.target); }
      });
    }, { rootMargin: "200px" }) : null;
    Array.prototype.forEach.call(boxes, function (box) {
      setState(box, "idle", "");
      if (io) io.observe(box); else start(box);
    });
  }
  window.orchFrames = { init: init };  // for pages that swap content in place
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", function () { init(); });
  else init();
})();
