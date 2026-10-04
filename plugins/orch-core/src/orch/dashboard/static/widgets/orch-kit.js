/* orch-kit: the API a widget frame sees (format orch.widgets.v1, docs/widgets.md). Inlined into every frame
   document; talks to the host only through postMessage {orch: 1, kind, frame: <nonce>}. No network, no storage. */
(function () {
  "use strict";
  var root = document.documentElement;
  var meta = document.querySelector('meta[name="orch-frame"]');
  var nonce = meta ? meta.getAttribute("content") : "";
  var host = window.parent;

  function post(kind, extra) {
    if (host === window) return;  // opened on its own (a saved document): nothing to tell
    var msg = { orch: 1, kind: kind, frame: nonce };
    if (extra) for (var k in extra) msg[k] = extra[k];
    host.postMessage(msg, "*");  // the host's origin is not ours to know (opaque sandbox); the nonce binds it
  }

  // The kit is inlined before the data element (contract order), so the data is read on first use.
  var data, parsed = false;
  function readData() {
    if (!parsed) {
      var node = document.getElementById("orch-data");
      if (!node) return null;  // not in the document yet: try again on the next read
      parsed = true;
      try { data = JSON.parse(node.textContent); } catch (e) { data = null; }
    }
    return data;
  }

  var media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  var theme = {
    var: function (name) { return getComputedStyle(root).getPropertyValue(name).trim(); },
    get dark() {
      var t = root.getAttribute("data-theme");
      return t === "dark" || (t !== "light" && !!(media && media.matches));
    }
  };
  function themeChanged() { window.dispatchEvent(new CustomEvent("orch:theme", { detail: { dark: theme.dark } })); }
  if (media && media.addEventListener) media.addEventListener("change", themeChanged);

  var lastHeight = -1;
  function resize() {
    var h = Math.ceil(Math.max(document.body ? document.body.getBoundingClientRect().height : 0, 0));
    if (h !== lastHeight) { lastHeight = h; post("resize", { height: h }); }
  }

  window.addEventListener("message", function (event) {
    var m = event.data;
    if (event.source !== host || !m || m.orch !== 1 || m.frame !== nonce) return;
    if (m.kind === "theme" && typeof m.dark === "boolean") {
      root.setAttribute("data-theme", m.dark ? "dark" : "light");
      themeChanged();
    }
  });

  window.addEventListener("error", function (event) {
    post("error", { message: String((event && event.message) || "Script error").slice(0, 500) });
  });

  window.orch = {
    get data() { return readData(); },
    theme: theme,
    text: function (str) { post("text", { text: String(str).slice(0, 20000) }); },
    ready: function () { post("ready"); resize(); },
    resize: resize,
    open: function (ref) { post("open", { ref: String(ref).slice(0, 100) }); },
    error: function (msg) { post("error", { message: String(msg).slice(0, 500) }); }
  };

  function watch() {
    if (window.ResizeObserver) new ResizeObserver(resize).observe(document.body);
    resize();
  }
  if (document.body) watch(); else document.addEventListener("DOMContentLoaded", watch);
})();
