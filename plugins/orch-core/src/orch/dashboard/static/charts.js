// Draws <figure class="chart" data-chart='{json}'> with the vendored Chart.js. The numbers are already in the
// figure's <details> table, so nothing here is needed to read them. Chart.js itself is fetched (from the same
// origin, data-lib) only when a page has a chart. Colours come from the theme's CSS tokens and are read again
// when the theme changes; animation is off under prefers-reduced-motion.
(() => {
  const reduced = () => Boolean(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  const live = new Map();  // figure -> Chart
  let loading = null;

  const css = (name, fallback) => {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback || "";
  };

  const load = (src) => {
    if (window.Chart) return Promise.resolve();
    if (!loading) {
      loading = new Promise((resolve, reject) => {
        const s = document.createElement("script");
        s.src = src;
        s.onload = resolve;
        s.onerror = () => { loading = null; reject(new Error("chart library")); };
        document.head.append(s);
      });
    }
    return loading;
  };

  const build = (fig, spec) => {
    const plot = fig.querySelector(".chart-plot");
    plot.textContent = "";
    const canvas = document.createElement("canvas");
    plot.append(canvas);
    const horizontal = Boolean(spec.horizontal) && spec.kind === "bar";
    plot.style.height = horizontal ? Math.max(200, spec.labels.length * 30 + 48) + "px" : "";
    const linear = spec.x === "linear";
    const ink = css("--muted"), grid = css("--line");
    const whole = spec.series.every((s) => s.values.every((v) => Number.isInteger(v)));
    const stacked = Boolean(spec.stacked);
    const sets = spec.series.map((s, i) => {
      const colour = css("--" + s.token, css("--muted"));
      const base = { label: s.name, borderColor: colour, backgroundColor: colour,
        data: linear ? s.values.map((y, k) => ({ x: spec.labels[k], y })) : s.values };
      if (spec.kind === "line") {
        return Object.assign(base, { borderWidth: 2, tension: 0.2, pointRadius: spec.labels.length > 40 ? 0 : 3 });
      }
      return Object.assign(base, { borderRadius: 4, maxBarThickness: 46, stack: s.stack || (stacked ? "all" : "s" + i) });
    });
    const axis = (extra) => Object.assign({ ticks: { color: ink, font: { size: 11 } }, grid: { color: grid }, border: { color: grid } }, extra);
    const cat = axis({ grid: { display: false }, stacked });
    const val = axis({ beginAtZero: true, stacked, border: { display: false },
      ticks: { color: ink, font: { size: 11 }, precision: whole ? 0 : undefined } });
    const tip = {
      backgroundColor: css("--surface2", "#fff"), titleColor: css("--text"), bodyColor: css("--text"),
      borderColor: css("--line2"), borderWidth: 1, padding: 10,
      callbacks: { label: (c) => " " + c.dataset.label + ": " + (horizontal ? c.parsed.x : c.parsed.y) + (spec.unit ? " " + spec.unit : "") },
    };
    return new window.Chart(canvas, {
      type: spec.kind === "line" ? "line" : "bar",
      data: { labels: linear ? undefined : spec.labels, datasets: sets },
      options: {
        responsive: true, maintainAspectRatio: false, animation: reduced() ? false : { duration: 250 },
        indexAxis: horizontal ? "y" : "x", interaction: { mode: "index", intersect: false },
        plugins: { legend: { display: false }, tooltip: tip },
        scales: horizontal ? { x: val, y: cat } : { x: linear ? axis({ type: "linear" }) : cat, y: val },
      },
    });
  };

  const draw = (fig) => {
    let spec;
    try { spec = JSON.parse(fig.dataset.chart); } catch (e) { return; }
    const old = live.get(fig);
    if (old) old.destroy();
    live.set(fig, build(fig, spec));
    fig.dataset.drawn = "1";
  };

  const start = (root) => {
    const figs = Array.from((root || document).querySelectorAll("figure.chart[data-chart]:not([data-drawn])"));
    if (!figs.length) return;
    figs.forEach((f) => { f.dataset.drawn = "0"; });
    load(figs[0].dataset.lib).then(() => figs.forEach(draw)).catch(() => figs.forEach((f) => {
      const plot = f.querySelector(".chart-plot");
      if (plot) plot.textContent = "The chart did not load. The numbers are below.";
      const d = f.querySelector("details");
      if (d) d.open = true;
    }));
  };

  const redraw = () => { if (window.Chart) Array.from(live.keys()).forEach((f) => { if (f.isConnected) draw(f); else live.delete(f); }); };

  const watch = () => {
    start(document);
    // pages are swapped in place by app.js: draw the charts of whatever arrives
    new MutationObserver(() => start(document)).observe(document.body, { childList: true, subtree: true });
    new MutationObserver(redraw).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    try { window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", redraw); } catch (e) { /* old browser */ }
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", watch); else watch();
})();
