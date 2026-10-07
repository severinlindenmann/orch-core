// File drop zone for <input type=file data-dropzone> (New ticket, a ticket's Add files). Progressive enhancement:
// without JS the native file input works as it is. With JS: drop files on the zone, press Choose files, or paste a
// screenshot into the textarea named by data-paste-from (it becomes screenshot-<timestamp>.png). Every file is listed
// with its size, a thumbnail for an image and a Remove button; the input's files are set through a DataTransfer, so
// the form posts exactly what the list shows. A file over data-max-bytes (the server's per-file artifact limit) is
// refused here with a plain message; the server checks it again. Thumbnails are data: URLs (the page's CSP allows
// data: images, not blob:), read only for images up to THUMB_MAX.
(() => {
  const MB = 1048576;
  const THUMB_MAX = 10 * MB;
  const size = (n) => (n >= MB ? (n / MB).toFixed(n >= 10 * MB ? 0 : 1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB");
  const EXT = { "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp" };
  const screenshotName = (type, at, n) => {
    const d = new Date(at);
    const p = (x) => String(x).padStart(2, "0");
    const stamp = d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) + "-" + p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds());
    return "screenshot-" + stamp + (n ? "-" + (n + 1) : "") + "." + (EXT[type] || "png");
  };
  const make = (tag, cls, text) => {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text !== undefined) el.textContent = text;
    return el;
  };

  const enhance = (input) => {
    if (input.dataset.dropzoneOn || !window.DataTransfer) return null;
    input.dataset.dropzoneOn = "1";
    const max = Number(input.dataset.maxBytes) || 0;
    const paste = input.dataset.pasteFrom && document.getElementById(input.dataset.pasteFrom);
    let files = [...(input.files || [])];
    const thumbs = new WeakMap();  // file -> data: URL

    const zone = make("div", "dropzone");
    const lead = make("p", "dz-lead", paste ? "Drop files here, choose files, or paste a screenshot with Ctrl/Cmd+V into the Ask box"
                                             : "Drop files here or choose files");
    const choose = make("button", "btn", "Choose files");
    choose.type = "button";
    const limit = make("p", "dz-limit muted", max ? "Up to " + size(max) + " per file." : "");
    const list = make("ul", "dz-list plain");
    const msg = make("p", "dz-msg");
    msg.setAttribute("role", "status");
    msg.setAttribute("aria-live", "polite");
    zone.append(lead, choose, limit, list, msg);
    input.classList.add("dz-input");
    input.tabIndex = -1;
    input.setAttribute("aria-hidden", "true");
    input.after(zone);

    const say = (text, bad) => { msg.textContent = text; msg.className = "dz-msg" + (bad ? " is-bad" : ""); };
    // the live line always ends on the total the form will send, never on the size of the last step
    const total = () => (files.length ? files.length + (files.length === 1 ? " file" : " files") + " attached." : "No files attached.");
    const render = () => {
      list.textContent = "";
      files.forEach((f, i) => {
        const li = make("li", "dz-item");
        const isImage = /^image\//.test(f.type) && f.size <= THUMB_MAX;
        const pic = make(isImage ? "img" : "span", "dz-thumb");
        if (isImage) {
          pic.alt = "";
          if (thumbs.has(f)) pic.src = thumbs.get(f);
          else if (window.FileReader) {
            const r = new window.FileReader();
            r.onload = () => { thumbs.set(f, r.result); pic.src = r.result; };
            r.readAsDataURL(f);
          }
        } else pic.textContent = (f.name.split(".").pop() || "file").slice(0, 4).toUpperCase();
        const remove = make("button", "btn btn-quiet dz-remove", "Remove");
        remove.type = "button";
        remove.setAttribute("aria-label", "Remove " + f.name);
        remove.addEventListener("click", () => {
          files.splice(i, 1);
          sync();
          say(f.name + " removed. " + total());
          const next = list.querySelectorAll(".dz-remove")[Math.min(i, files.length - 1)];
          (next || choose).focus();
        });
        li.append(pic, make("span", "dz-name", f.name), make("span", "dz-size muted", size(f.size)), remove);
        list.append(li);
      });
    };
    const sync = () => {
      const dt = new window.DataTransfer();
      files.forEach((f) => dt.items.add(f));
      input.files = dt.files;
      render();
    };
    const add = (incoming, note) => {
      const refused = [];
      [...incoming].forEach((f) => {
        if (max && f.size > max) refused.push(f.name + " (" + size(f.size) + ")");
        else files.push(f);
      });
      sync();
      if (refused.length) say("Not added, over the limit of " + size(max) + " per file: " + refused.join(", ") + ". " + total(), true);
      else if (incoming.length) say((note ? note + " " : "") + total());
    };

    choose.addEventListener("click", () => input.click());
    // the native picker replaces the input's files with the new pick: keep what was listed and add it
    input.addEventListener("change", () => {
      const picked = [...input.files].filter((f) => !files.includes(f));
      add(picked);
    });
    zone.addEventListener("dragover", (event) => { event.preventDefault(); zone.classList.add("is-over"); });
    zone.addEventListener("dragleave", () => zone.classList.remove("is-over"));
    zone.addEventListener("drop", (event) => {
      event.preventDefault();
      zone.classList.remove("is-over");
      const dropped = event.dataTransfer ? event.dataTransfer.files : [];
      if (dropped.length) add(dropped);
    });
    if (paste) {
      paste.addEventListener("paste", (event) => {
        const images = [...((event.clipboardData && event.clipboardData.files) || [])].filter((f) => /^image\//.test(f.type));
        if (!images.length) return;  // text pastes as text
        event.preventDefault();
        const at = Date.now();
        const named = images.map((f, n) => new window.File([f], screenshotName(f.type, at, n), { type: f.type }));
        add(named, "Pasted " + named.map((f) => f.name).join(", ") + ".");
      });
    }
    render();
    return { files: () => files, zone };
  };

  const scan = (root) => root.querySelectorAll && root.querySelectorAll("input[type=file][data-dropzone]").forEach(enhance);
  if (document.body) scan(document);
  else document.addEventListener("DOMContentLoaded", () => scan(document));
  // a page swapped in place (app.js) brings new inputs
  if (window.MutationObserver) {
    new window.MutationObserver((records) => records.forEach((r) => r.addedNodes.forEach((n) => scan(n))))
      .observe(document.documentElement, { childList: true, subtree: true });
  }
  window.orchFiles = { enhance, size, screenshotName };
})();
