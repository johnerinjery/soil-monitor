/* Soil & environment monitor: static dashboard.
 * Reads only the JSON snapshot written by the scheduled job (latest/series/raw/daily/events.json).
 * No keys, no database calls, no build step. All text from data goes through textContent. */
(function () {
  "use strict";

  // ------------------------------------------------------------------ helpers
  const $ = (s, el) => (el || document).querySelector(s);
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const svgNS = "http://www.w3.org/2000/svg";
  const num = (v) => typeof v === "number" && isFinite(v);
  const fmt = (v, nd, unit) => (num(v) ? v.toFixed(nd) + (unit || "") : "–");
  const clampNull = (a) => a.map((v) => (num(v) ? v : null));
  const safe = (fn) => { try { return fn(); } catch (e) { return undefined; } };
  const store = {
    get: (k) => safe(() => localStorage.getItem(k)),
    set: (k, v) => safe(() => localStorage.setItem(k, v)),
  };

  const S = { L: null, ser: null, raw: null, daily: null, ev: null, demo: false, base: "data/", rangeDays: 7, tz: "UTC", fmtTz: null, zoom: null, plots: [] };

  // ------------------------------------------------------------------ time (stored UTC, shown in the site's timezone)
  function setTz(tz) {
    S.tz = tz || "UTC";
    S.fmtTz = new Intl.DateTimeFormat("sv-SE", { timeZone: S.tz, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
    S.fmtNice = new Intl.DateTimeFormat("en-GB", { timeZone: S.tz, weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  }
  const loc = (e) => S.fmtTz.format(e * 1000);          // "2026-10-21 14:30:00", plotted as plain wall-clock time
  const nice = (e) => (num(e) ? S.fmtNice.format(e * 1000) : "–");
  function ago(sec) {
    if (!num(sec)) return "–";
    const m = Math.round(sec / 60);
    if (m < 2) return "just now";
    if (m < 90) return m + " min ago";
    const h = sec / 3600;
    if (h < 36) return h.toFixed(h < 10 ? 1 : 0) + " h ago";
    return Math.round(h / 24) + " d ago";
  }
  const dur = (h) => (!num(h) ? "–" : h < 48 ? Math.round(h) + " h" : (h / 24).toFixed(1) + " d");

  // ------------------------------------------------------------------ theme and palette
  const cssv = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  function pal() {
    return { page: cssv("--page"), surface: cssv("--surface"), ink: cssv("--ink"), ink2: cssv("--ink-2"), muted: cssv("--muted"), grid: cssv("--grid"), axis: cssv("--axis"),
      s1: cssv("--s1"), s2: cssv("--s2"), s3: cssv("--s3"), s7: cssv("--s7"), seqLo: cssv("--seq-lo"), seqHi: cssv("--seq-hi"),
      good: cssv("--good"), warn: cssv("--warn"), serious: cssv("--serious"), crit: cssv("--crit") };
  }
  const withAlpha = (hex, a) => { const h = hex.replace("#", ""); const n = parseInt(h.length === 3 ? h.replace(/./g, "$&$&") : h, 16); return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`; };
  function applyTheme(t) {
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t); else document.documentElement.removeAttribute("data-theme");
  }
  function currentIsDark() {
    const t = document.documentElement.getAttribute("data-theme");
    return t ? t === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  }

  // ------------------------------------------------------------------ icons (shape + label, never colour alone)
  function icon(kind) {
    const s = document.createElementNS(svgNS, "svg");
    s.setAttribute("viewBox", "0 0 16 16"); s.setAttribute("class", "ico"); s.setAttribute("aria-hidden", "true");
    const p = pal();
    const add = (tag, attrs) => { const n = document.createElementNS(svgNS, tag); for (const k in attrs) n.setAttribute(k, attrs[k]); s.appendChild(n); return n; };
    if (kind === "ok") { add("circle", { cx: 8, cy: 8, r: 7, fill: p.good }); add("path", { d: "M4.5 8.3l2.4 2.4 4.6-5", fill: "none", stroke: "#fff", "stroke-width": 1.8, "stroke-linecap": "round", "stroke-linejoin": "round" }); }
    else if (kind === "info") { add("circle", { cx: 8, cy: 8, r: 6.4, fill: "none", stroke: p.ink2, "stroke-width": 1.6 }); add("rect", { x: 7.2, y: 7, width: 1.6, height: 4.6, fill: p.ink2 }); add("circle", { cx: 8, cy: 4.8, r: 1, fill: p.ink2 }); }
    else if (kind === "warn") { add("path", { d: "M8 1.5L15 14H1z", fill: p.warn, stroke: p.ink, "stroke-width": 0.8, "stroke-linejoin": "round" }); add("rect", { x: 7.3, y: 6, width: 1.4, height: 4, fill: "#0b0b0b" }); add("circle", { cx: 8, cy: 11.9, r: 0.9, fill: "#0b0b0b" }); }
    else { add("path", { d: "M5 1h6l4 4v6l-4 4H5l-4-4V5z", fill: p.crit }); add("path", { d: "M5.5 5.5l5 5M10.5 5.5l-5 5", stroke: "#fff", "stroke-width": 1.8, "stroke-linecap": "round" }); }
    return s;
  }
  const SEV_LABEL = { ok: "OK", info: "Note", warn: "Warning", crit: "Critical" };

  // ------------------------------------------------------------------ data loading
  async function getJSON(base, name) {
    const r = await fetch(base + name + "?v=" + Date.now(), { cache: "no-store" });
    if (!r.ok) throw new Error(name + " " + r.status);
    return r.json();
  }
  async function loadAll() {
    let base = "data/";
    let L;
    try { L = await getJSON(base, "latest.json"); }
    catch (e) { base = "data/sample/"; S.demo = true; L = await getJSON(base, "latest.json"); }
    const [ser, raw, daily, ev] = await Promise.all(["series.json", "raw.json", "daily.json", "events.json"].map((n) => getJSON(base, n)));
    Object.assign(S, { L, ser, raw, daily, ev, base });
    if (L.source === "simulated") S.demo = true;
  }

  // ------------------------------------------------------------------ range handling
  function tEnd() { return S.L.data_through ? Date.parse(S.L.data_through) / 1000 : Date.now() / 1000; }
  function range() {
    const t1 = tEnd();
    const first = S.ser.t.length ? S.ser.t[0] : t1 - 7 * 86400;
    const t0 = S.rangeDays ? t1 - S.rangeDays * 86400 : first;
    return [t0, t1 + 600];
  }
  function bounds(t, t0, t1) {
    let lo = 0, hi = t.length;
    while (lo < hi) { const m = (lo + hi) >> 1; if (t[m] < t0) lo = m + 1; else hi = m; }
    const a = lo; hi = t.length;
    while (lo < hi) { const m = (lo + hi) >> 1; if (t[m] <= t1) lo = m + 1; else hi = m; }
    return [a, lo];
  }
  function slice(d, keys, t0, t1) {
    const [a, b] = bounds(d.t, t0, t1);
    const out = { t: d.t.slice(a, b), x: d.t.slice(a, b).map(loc) };
    keys.forEach((k) => { out[k] = (d[k] || []).length ? d[k].slice(a, b) : new Array(b - a).fill(null); });
    return out;
  }
  // break the line where the node was silent for more than gapS seconds
  function gapify(o, keys, gapS) {
    const out = { t: [], x: [] }; keys.forEach((k) => (out[k] = []));
    for (let i = 0; i < o.t.length; i++) {
      if (i > 0 && o.t[i] - o.t[i - 1] > gapS) { out.t.push(o.t[i]); out.x.push(o.x[i]); keys.forEach((k) => out[k].push(null)); }
      out.t.push(o.t[i]); out.x.push(o.x[i]); keys.forEach((k) => out[k].push(o[k][i]));
    }
    return out;
  }
  // keep bars <= 24 px thick regardless of how many categories there are
  const barW = (P, n) => Math.min(0.6, 24 / Math.max(40, ((P.plot.clientWidth || 800) - 70) / Math.max(1, n)));
  const hasData = (arrs) => arrs.some((a) => a.some(num));

  // ------------------------------------------------------------------ plotly layout
  function baseLayout(p, opts) {
    const [t0, t1] = range();
    const o = opts || {};
    const ax = (extra) => Object.assign({ gridcolor: p.grid, gridwidth: 1, zeroline: false, linecolor: p.axis, linewidth: 1, tickfont: { color: p.ink2, size: 11 }, ticks: "", automargin: true, showline: false }, extra);
    return {
      paper_bgcolor: p.surface, plot_bgcolor: p.surface, font: { family: "system-ui, -apple-system, 'Segoe UI', sans-serif", color: p.ink2, size: 12 },
      margin: { l: 52, r: o.right || 18, t: 8, b: o.noX ? 8 : 30 }, height: o.height || 220,
      xaxis: ax(o.xaxis || { range: S.zoom || [loc(t0), loc(t1)], type: "date", showspikes: true, spikemode: "across", spikethickness: 1, spikecolor: p.muted, spikedash: "solid", spikesnap: "cursor", tickformat: "%d %b\n%H:%M" }),
      yaxis: ax(Object.assign({ title: { text: o.ytitle || "", font: { size: 11.5, color: p.ink2 }, standoff: 6 } }, o.yaxis || {})),
      hovermode: o.hovermode || "x unified", dragmode: o.dragmode || "zoom",
      hoverlabel: { bgcolor: p.surface, bordercolor: p.axis, font: { color: p.ink, size: 12, family: "system-ui, sans-serif" } },
      legend: { orientation: "h", x: 0, y: 1.14, font: { color: p.ink2, size: 12 }, itemsizing: "constant", bgcolor: "rgba(0,0,0,0)" },
      showlegend: o.legend !== false, shapes: o.shapes || [], annotations: o.annotations || [],
    };
  }
  const CFG = { displaylogo: false, responsive: true, displayModeBar: "hover", modeBarButtonsToRemove: ["select2d", "lasso2d", "autoScale2d", "toggleSpikelines"] };
  const line = (color, extra) => Object.assign({ type: "scatter", mode: "lines", line: { color, width: 2, shape: "linear" }, connectgaps: false }, extra);
  const hline = (y, p, text, dash) => ({ shapes: { type: "line", xref: "paper", x0: 0, x1: 1, yref: "y", y0: y, y1: y, line: { color: p.muted, width: 1, dash: dash || "solid" } },
    ann: text ? { xref: "paper", x: 1, xanchor: "right", yref: "y", y: y, yanchor: "bottom", text, showarrow: false, font: { size: 11, color: p.ink2 } } : null });
  function refLines(list, p) {
    const shapes = [], annotations = [];
    list.forEach((r) => { const h = hline(r[0], p, r[1], r[2]); shapes.push(h.shapes); if (h.ann) annotations.push(h.ann); });
    return { shapes, annotations };
  }

  // ------------------------------------------------------------------ panels
  function makePanel(parent, id, title, sub) {
    const box = el("div", "panel"); box.id = id;
    const head = el("div", "panel-head");
    const left = el("div");
    left.appendChild(el("h3", null, title));
    const subEl = el("p", "sub", sub || ""); subEl.dataset.role = "sub"; left.appendChild(subEl);
    const btn = el("button", "btn tbtn", "Table"); btn.type = "button"; btn.setAttribute("aria-expanded", "false");
    head.appendChild(left); head.appendChild(btn);
    const plot = el("div", "plot"); plot.setAttribute("role", "img"); plot.setAttribute("aria-label", title + " chart. Use the Table button for the same data as text.");
    const tbl = el("div", "tbl"); tbl.hidden = true;
    box.appendChild(head); box.appendChild(plot); box.appendChild(tbl);
    parent.appendChild(box);
    const P = { id, box, plot, tbl, subEl, btn, table: null, sync: false };
    btn.addEventListener("click", () => {
      const open = tbl.hidden; tbl.hidden = !open; btn.setAttribute("aria-expanded", String(open));
      if (open && P.table) fillTable(tbl, P.table());
    });
    S.plots.push(P);
    return P;
  }
  function fillTable(host, t) {
    host.textContent = "";
    if (!t || !t.rows.length) { host.appendChild(el("p", "empty", "No rows in this range.")); return; }
    const tb = el("table"); const th = el("thead"); const tr = el("tr");
    t.head.forEach((h) => tr.appendChild(el("th", null, h))); th.appendChild(tr); tb.appendChild(th);
    const body = el("tbody");
    const step = Math.max(1, Math.ceil(t.rows.length / 300));
    t.rows.forEach((r, i) => { if (i % step) return; const row = el("tr"); r.forEach((c) => row.appendChild(el("td", null, c == null ? "–" : String(c)))); body.appendChild(row); });
    tb.appendChild(body); host.appendChild(tb);
    if (step > 1) host.appendChild(el("p", "empty", "Showing every " + step + "th row; download the CSV for all of it."));
  }
  function draw(P, traces, layout, tableFn, subText) {
    if (subText != null) P.subEl.textContent = subText;
    P.table = tableFn || null;
    if (!traces.length || !traces.some((t) => t.__real !== false && ((t.y && t.y.some((v) => v != null)) || (t.z && t.z.length) || t.__keep))) {
      Plotly.purge(P.plot); P.bound = false; P.plot.textContent = ""; P.plot.appendChild(el("p", "empty", "No data in this range.")); P.empty = true;
      if (!P.tbl.hidden) fillTable(P.tbl, tableFn ? tableFn() : null);
      return;
    }
    if (P.empty) { P.plot.textContent = ""; P.empty = false; }
    const clean = traces.map((t) => { const c = Object.assign({}, t); delete c.__keep; delete c.__real; return c; });
    Plotly.react(P.plot, clean, layout, CFG);
    if (!P.bound && layout.xaxis && layout.xaxis.type === "date" && P.sync) {
      P.bound = true;
      P.plot.on("plotly_relayout", (ev) => onRelayout(P, ev));
    }
    if (!P.tbl.hidden) fillTable(P.tbl, tableFn ? tableFn() : null);
  }
  function onRelayout(src, ev) {
    if (S.syncing) return;
    let rng = null;
    if (ev["xaxis.range[0]"] != null) rng = [ev["xaxis.range[0]"], ev["xaxis.range[1]"]];
    else if (ev["xaxis.range"]) rng = ev["xaxis.range"];
    else if (ev["xaxis.autorange"]) { S.zoom = null; renderAll(); return; }
    if (!rng) return;
    S.zoom = rng; S.syncing = true;
    const jobs = S.plots.filter((q) => q !== src && q.sync && !q.empty).map((q) => Plotly.relayout(q.plot, { "xaxis.range": rng }));
    Promise.all(jobs).finally(() => { S.syncing = false; });
  }

  // ------------------------------------------------------------------ tiles
  function spark(values, color) {
    const v = values.filter(num);
    const s = document.createElementNS(svgNS, "svg"); s.setAttribute("class", "spark"); s.setAttribute("viewBox", "0 0 120 26"); s.setAttribute("preserveAspectRatio", "none"); s.setAttribute("aria-hidden", "true");
    if (v.length < 3) return s;
    const lo = Math.min(...v), hi = Math.max(...v), span = hi - lo || 1;
    const n = values.length; const pts = [];
    values.forEach((y, i) => { if (num(y)) pts.push(((i / (n - 1)) * 118 + 1).toFixed(1) + "," + (24 - ((y - lo) / span) * 22).toFixed(1)); });
    const pl = document.createElementNS(svgNS, "polyline"); pl.setAttribute("points", pts.join(" ")); pl.setAttribute("fill", "none"); pl.setAttribute("stroke", color); pl.setAttribute("stroke-width", "2"); pl.setAttribute("stroke-linejoin", "round"); pl.setAttribute("stroke-linecap", "round");
    s.appendChild(pl);
    const last = pts[pts.length - 1].split(","); const c = document.createElementNS(svgNS, "circle");
    c.setAttribute("cx", last[0]); c.setAttribute("cy", last[1]); c.setAttribute("r", "3"); c.setAttribute("fill", color); s.appendChild(c);
    return s;
  }
  function last24(key, nbuckets) {
    const t = S.ser.t, a = S.ser[key]; if (!t.length || !a || !a.length) return [];
    const end = tEnd(), start = end - 86400, out = [];
    for (let b = 0; b < nbuckets; b++) {
      const lo = start + (b * 86400) / nbuckets, hi = start + ((b + 1) * 86400) / nbuckets; let s = 0, c = 0;
      const [i0, i1] = bounds(t, lo, hi - 1);
      for (let i = i0; i < i1; i++) if (num(a[i])) { s += a[i]; c++; }
      out.push(c ? s / c : null);
    }
    return out;
  }
  function tile(host, o) {
    const t = el("div", "tile" + (o.hero ? " hero" : ""));
    const lbl = el("div", "lbl", o.label); if (o.tag) lbl.appendChild(el("span", "tag", o.tag)); t.appendChild(lbl);
    const v = el("div", "val"); v.appendChild(document.createTextNode(o.value)); if (o.unit) v.appendChild(el("small", null, " " + o.unit)); t.appendChild(v);
    (o.subs || []).forEach((s) => t.appendChild(el("div", "sub", s)));
    if (o.chips) { const c = el("div", "chips"); o.chips.forEach((ch) => { const sp = el("span"); const d = el("i", "dot"); d.style.color = ch.color; sp.appendChild(d); sp.appendChild(document.createTextNode(ch.text)); c.appendChild(sp); }); t.appendChild(c); }
    if (o.spark) t.appendChild(spark(o.spark, o.sparkColor || pal().s1));
    host.appendChild(t);
  }
  function renderTiles() {
    const L = S.L, C = L.cards, N = L.now, p = pal(), host = $("#tiles"); host.textContent = "";
    const m = C.moisture || {}, fc = L.forecast || {};
    const trend = num(m.trend_6h) ? (m.trend_6h >= 0 ? "▲ +" : "▼ ") + m.trend_6h.toFixed(2) + " in 6 h" : "";
    tile(host, { hero: true, label: "Soil moisture index", tag: m.calibrated ? null : "uncalibrated", value: num(m.w) ? m.w.toFixed(2) : "–",
      subs: [(m.label || "no valid probe") + (trend ? " · " + trend : ""), "water-soon level " + fmt(m.target, 2) + " · saturated " + fmt(m.wet, 2)],
      chips: ["m1", "m2", "m3"].map((k, i) => ({ color: [p.s1, p.s2, p.s3][i], text: "P" + (i + 1) + " " + fmt((m.probes || {})[k], 2) })),
      spark: last24("w_med", 24), sparkColor: p.s1 });

    let wv = "–", ws = [];
    if (fc.status === "ok") {
      wv = dur(fc.hours_lo) + " – " + (fc.hours_hi == null || fc.hours_hi > 24 * 14 ? ">14 d" : dur(fc.hours_hi));
      ws = ["best guess " + dur(fc.hours_mid) + " from the last reading", nice(fc.t_lo) + " to " + (fc.t_hi ? nice(fc.t_hi) : "later")];
    } else if (fc.status === "at_or_below") { wv = "now"; ws = ["index " + fmt(fc.w_now, 2) + " is at or below " + fmt(fc.w_star, 2)]; }
    else if (fc.status === "no_trend") { wv = "no trend"; ws = [fc.note || "index is not falling"]; }
    else if (fc.status === "insufficient") { wv = "learning"; ws = [fc.note || "needs more dry-down data"]; }
    else { wv = "–"; ws = ["no valid moisture data"]; }
    tile(host, { label: "Water-soon window", value: wv, subs: ws });

    const evs = (S.ev.events || []).filter((e) => e.t);
    const le = evs.length ? evs[evs.length - 1] : null;
    tile(host, { label: "Last watering", value: le ? ago(tEnd() - le.t) : "none seen", subs: le ? [nice(le.t) + " · " + ({ both: "detected + logged", detected: "detected", manual: "logged" })[le.source], num(le.amplitude) ? "index rose by " + le.amplitude.toFixed(2) : ""].filter(Boolean) : ["no event in the loaded history"] });

    const v = C.vpd || {};
    tile(host, { label: "Evaporative demand (VPD)", value: num(v.now) ? v.now.toFixed(2) : "–", unit: "kPa", subs: [({ low: "very humid", ok: "comfortable", warn: "elevated", high: "very dry air", unknown: "no data" })[v.band] + (num(v.sigma) ? " · ±" + v.sigma.toFixed(2) : ""), v.frac_24h ? Math.round(v.frac_24h.ok * 100) + " % of last 24 h in the comfortable band" : ""].filter(Boolean), spark: last24("vpd", 24), sparkColor: p.s2 });

    tile(host, { label: "Air", value: num(N.air_t.v) ? N.air_t.v.toFixed(1) : "–", unit: "°C", subs: [fmt(N.rh.v, 0) + " % RH · dew point " + fmt(N.dew.v, 1) + " °C", C.comfort && C.comfort.status === "ok" ? Math.round(C.comfort.frac_in_range * 100) + " % of 24 h in comfort range" : ""].filter(Boolean), spark: last24("air_t", 24), sparkColor: p.s2 });

    const th = C.thermal || {};
    tile(host, { label: "Soil temperature", value: num(N.soil_t.v) ? N.soil_t.v.toFixed(1) : "–", unit: "°C", subs: [th.status === "ok" ? "lags air by ~" + th.phase_lag_h.toFixed(1) + " h (τ " + th.tau_h.toFixed(1) + " h)" : "thermal lag: " + (th.status || "n/a"), num(th.delta_now) ? (th.delta_now >= 0 ? "+" : "") + th.delta_now.toFixed(1) + " °C vs air" : ""].filter(Boolean), spark: last24("soil_t", 24), sparkColor: p.s1 });

    const lt = C.light || {};
    tile(host, { label: "Light", value: num(N.lux.v) ? Math.round(N.lux.v).toLocaleString("en-GB") : "–", unit: "lx", subs: [num(N.ppfd.v) ? "≈ " + N.ppfd.v.toFixed(0) + " µmol/m²/s PPFD" : "", lt.dli != null ? "yesterday " + lt.dli.toFixed(1) + " ± " + lt.dli_unc.toFixed(1) + " mol/m²/d (" + ({ low: "low", ok: "in range", high: "high", uncertain: "borderline" })[lt.status] + ")" : "no complete day yet"].filter(Boolean), spark: last24("ppfd", 24), sparkColor: p.s7 });

    const ow = C.overwet || {}, hu = C.humid || {};
    tile(host, { label: "Wet and humid exposure", value: dur(ow.dwell_h || 0), subs: ["saturated-soil dwell (" + (ow.level || "none") + ")", "humid-air exposure " + dur(hu.hours || 0) + " (" + (hu.level || "none") + ")"] });
  }

  // ------------------------------------------------------------------ flags, banners, header
  function renderFlags() {
    const L = S.L, ul = $("#flags"); ul.textContent = "";
    if (!L.flags.length) { const li = el("li", "flag"); li.appendChild(icon("ok")); const d = el("div"); d.appendChild(el("span", "t", "Nothing to report")); li.appendChild(d); ul.appendChild(li); return; }
    L.flags.forEach((f) => {
      const li = el("li", "flag"); li.appendChild(icon(f.sev === "crit" ? "crit" : f.sev === "warn" ? "warn" : "info"));
      const h = el("div"); h.appendChild(el("span", "t", f.title)); h.appendChild(el("span", "sev", SEV_LABEL[f.sev]));
      li.appendChild(h); li.appendChild(el("div", "d", f.detail)); ul.appendChild(li);
    });
  }
  function renderHeader() {
    const L = S.L, pill = $("#status-pill"); pill.textContent = "";
    const lv = L.status.level; pill.appendChild(icon(lv === "ok" ? "ok" : lv === "warn" ? "warn" : "crit"));
    pill.appendChild(document.createTextNode(L.status.label + (L.status.n_crit + L.status.n_warn ? " · " + (L.status.n_crit + L.status.n_warn) : "")));
    const age = Date.now() / 1000 - tEnd();
    $("#subtitle").textContent = (L.device_id || "node") + " · last reading " + nice(tEnd()) + " (" + S.tz + ") · " + (S.demo ? "demo data" : ago(age)) + " · page built " + nice(Date.parse(L.generated_at) / 1000);
    const b = $("#banners"); b.textContent = "";
    if (S.demo) { const d = el("div", "banner demo"); d.appendChild(icon("info")); d.appendChild(el("span", null, "You are looking at simulated demo data. The real snapshot appears here after the first successful scheduled run.")); b.appendChild(d); }
    else if (age > 3 * 3600) { const d = el("div", "banner"); d.appendChild(icon("warn")); d.appendChild(el("span", null, "This snapshot is " + ago(age) + " old. Either the node has stopped reporting or the scheduled job has not run (GitHub can delay scheduled runs).")); b.appendChild(d); }
  }

  // ------------------------------------------------------------------ chart builders
  function buildPanels() {
    S.plots = [];
    const soil = $("#sec-soil"), air = $("#sec-air"), light = $("#sec-light"), raw = $("#sec-raw");
    [soil, air, light, raw].forEach((n) => (n.textContent = ""));
    makePanel(soil, "p-moist", "Moisture index per probe and median", "");
    makePanel(soil, "p-k", "Drying coefficient per dry-down", "");
    makePanel(soil, "p-over", "Spread between probes", "");
    makePanel(air, "p-vpd", "Vapour-pressure deficit", "");
    makePanel(air, "p-temp", "Soil and air temperature", "");
    makePanel(air, "p-dew", "Condensation margin (soil temperature minus dew point)", "");
    makePanel(air, "p-humid", "Humid-air exposure", "");
    makePanel(light, "p-ppfd", "Light level (PPFD estimate)", "");
    makePanel(light, "p-dli", "Daily light integral", "");
    makePanel(light, "p-heat", "Light by hour of day", "");
    [["r-mv", "Probe voltage"], ["r-mad", "Probe noise (median absolute deviation within one reading)"], ["r-soil", "Soil temperature (DS18B20)"], ["r-air", "Air temperature (DHT22)"],
      ["r-rh", "Relative humidity (DHT22)"], ["r-lux", "Light (BH1750)"], ["r-rssi", "Wi-Fi signal at the previous upload"]].forEach((a) => makePanel(raw, a[0], a[1], ""));
    S.plots.forEach((P) => { P.sync = !["p-k", "p-dli", "p-heat"].includes(P.id); });
  }
  const P_ = (id) => S.plots.find((q) => q.id === id);
  const rows = (o, keys, nds) => o.t.map((t, i) => [loc(t)].concat(keys.map((k, j) => (num(o[k][i]) ? +o[k][i].toFixed(nds[j]) : null))));

  function renderMoisture(p) {
    const [t0, t1] = range(), L = S.L, cfg = L.config;
    const d = slice(S.ser, ["w_m1", "w_m2", "w_m3", "w_med", "w_spread"], t0, t1);
    const tr = [line(p.s1, { name: "Probe 1", x: d.x, y: d.w_m1 }), line(p.s2, { name: "Probe 2", x: d.x, y: d.w_m2 }), line(p.s3, { name: "Probe 3", x: d.x, y: d.w_m3 }),
      line(p.ink, { name: "Median", x: d.x, y: d.w_med })];
    tr.forEach((t) => (t.hovertemplate = "%{y:.2f}"));
    const fc = L.forecast || {};
    if (fc.fit_line && fc.fit_line.t.length) tr.push(line(p.muted, { name: "Dry-down fit", x: fc.fit_line.t.map(loc), y: fc.fit_line.w, line: { color: p.muted, width: 2, dash: "dot" }, hovertemplate: "%{y:.2f}" }));
    if (fc.status === "ok" && fc.projection) {
      const pj = fc.projection, x = pj.t.map(loc);
      tr.push({ type: "scatter", mode: "lines", x: x.concat(x.slice().reverse()), y: pj.hi.concat(pj.lo.slice().reverse()), fill: "toself", fillcolor: withAlpha(p.s1, 0.1), line: { width: 0 }, hoverinfo: "skip", showlegend: false, name: "band" });
      tr.push(line(p.s1, { name: "Forecast (expected VPD)", x, y: pj.mid, line: { color: p.s1, width: 2, dash: "dot" }, hovertemplate: "%{y:.2f}" }));
    }
    const evs = (S.ev.events || []).filter((e) => e.t >= t0 && e.t <= t1);
    const det = evs.filter((e) => e.w_peak != null), logged = evs.filter((e) => e.w_peak == null);
    if (det.length) tr.push({ type: "scatter", mode: "markers", name: "Watering detected", x: det.map((e) => loc(e.t_peak || e.t)), y: det.map((e) => e.w_peak), marker: { symbol: "triangle-up", size: 11, color: p.ink, line: { color: p.surface, width: 2 } }, customdata: det.map((e) => e.amplitude), hovertemplate: "watering, +%{customdata:.2f}" });
    if (logged.length) tr.push({ type: "scatter", mode: "markers", name: "Watering logged, not detected", x: logged.map((e) => loc(e.t)), y: logged.map(() => 0.04), marker: { symbol: "diamond-open", size: 11, color: p.ink, line: { width: 2 } }, hovertemplate: "logged watering" });
    const rl = refLines([[cfg.w_star, "water soon " + cfg.w_star.toFixed(2)], [cfg.w_wet, "saturated " + cfg.w_wet.toFixed(2)]], p);
    const xr = fc.status === "ok" && fc.projection ? [loc(t0), loc(Math.max(t1, fc.projection.t[Math.min(fc.projection.t.length - 1, 24)]))] : [loc(t0), loc(t1)];
    const lay = baseLayout(p, { height: 330, ytitle: "index (0 dry – 1 wet)", yaxis: { range: [0, 1.05] }, shapes: rl.shapes, annotations: rl.annotations, xaxis: { type: "date", range: S.zoom || xr, showspikes: true, spikemode: "across", spikethickness: 1, spikecolor: p.muted, spikedash: "solid", spikesnap: "cursor", tickformat: "%d %b\n%H:%M", gridcolor: p.grid, tickfont: { color: p.ink2, size: 11 }, zeroline: false } });
    draw(P_("p-moist"), tr, lay, () => ({ head: ["time", "P1", "P2", "P3", "median", "spread"], rows: rows(d, ["w_m1", "w_m2", "w_m3", "w_med", "w_spread"], [3, 3, 3, 3, 3]) }),
      (cfg.moisture_calibrated ? "Relative wetness between the dry and wet anchors." : "Uncalibrated: the shape and timing are reliable, absolute levels are placeholders until you calibrate.") + " Triangles are detected waterings; the dotted band is the forecast window.");

    // spread
    const tr2 = [line(p.s1, { name: "Spread (max − min)", x: d.x, y: d.w_spread, hovertemplate: "%{y:.2f}" })];
    const r2 = refLines([[S.L.quality && S.L.quality.spread ? 0.25 : 0.25, "flag at 0.25"]], p);
    draw(P_("p-over"), tr2, baseLayout(p, { height: 160, ytitle: "index units", yaxis: { rangemode: "tozero" }, legend: false, shapes: r2.shapes, annotations: r2.annotations }),
      () => ({ head: ["time", "spread"], rows: rows(d, ["w_spread"], [3]) }), "Large or growing spread between probes means a placement, contact or sensor problem.");
  }

  function renderK(p) {
    const fc = S.L.forecast || {}, hist = (fc.history || []).slice(-12), cur = fc.fit;
    const x = hist.map((h) => nice(h.t0)), y = hist.map((h) => h.k);
    const tr = [];
    if (hist.length) tr.push({ type: "bar", name: "Completed dry-downs", x, y, marker: { color: p.s1, cornerradius: 4 }, width: barW(P_("p-k"), hist.length + 1), hovertemplate: "k = %{y:.3f}", __keep: true });
    if (cur && cur.k > 0) tr.push({ type: "bar", name: "Current", x: ["now"], y: [cur.k], marker: { color: p.s2, cornerradius: 4 }, width: barW(P_("p-k"), hist.length + 1), error_y: { type: "data", symmetric: false, array: [cur.k_hi - cur.k], arrayminus: [cur.k - cur.k_lo], color: p.ink2, thickness: 1.5, width: 4 }, hovertemplate: "k = %{y:.3f}", __keep: true });
    const lay = baseLayout(p, { height: 190, ytitle: "k (index per kPa·day)", yaxis: { rangemode: "tozero" }, xaxis: { type: "category", gridcolor: p.grid, tickfont: { color: p.ink2, size: 11 }, zeroline: false }, hovermode: "closest", legend: tr.length > 1 });
    lay.bargap = 0.4;
    draw(P_("p-k"), tr, lay, () => ({ head: ["dry-down starting", "k", "points"], rows: hist.map((h) => [nice(h.t0), h.k, h.n]).concat(cur ? [["current", +cur.k.toFixed(3), cur.n]] : []) }),
      "How fast the index falls per unit of evaporative demand. A rising k means the soil dries faster for the same air (more light on the pot, root growth, a heater). Error bar: widened 95 % range.");
  }

  function renderAir(p) {
    const [t0, t1] = range(), cfg = S.L.config, v = cfg.vpd;
    const d = slice(S.ser, ["vpd", "vpd_sigma", "soil_t", "air_t", "dew", "margin", "humid_h", "ppfd"], t0, t1);
    const up = d.vpd.map((y, i) => (num(y) && num(d.vpd_sigma[i]) ? y + d.vpd_sigma[i] : null)), lo = d.vpd.map((y, i) => (num(y) && num(d.vpd_sigma[i]) ? Math.max(0, y - d.vpd_sigma[i]) : null));
    const tr = [{ type: "scatter", mode: "lines", x: d.x, y: lo, line: { width: 0 }, hoverinfo: "skip", showlegend: false, __real: false, connectgaps: false },
      { type: "scatter", mode: "lines", x: d.x, y: up, line: { width: 0 }, fill: "tonexty", fillcolor: withAlpha(p.s2, 0.14), hoverinfo: "skip", showlegend: false, __real: false, connectgaps: false },
      line(p.s2, { name: "VPD", x: d.x, y: d.vpd, hovertemplate: "%{y:.2f} kPa" })];
    const rl = refLines([[v.low, "low " + v.low], [v.ok_max, "comfortable up to " + v.ok_max], [v.warn_max, "high above " + v.warn_max]], p);
    const band = { type: "rect", xref: "paper", x0: 0, x1: 1, yref: "y", y0: v.low, y1: v.ok_max, fillcolor: withAlpha(p.muted, 0.14), line: { width: 0 }, layer: "below" };
    draw(P_("p-vpd"), tr, baseLayout(p, { height: 230, ytitle: "kPa", yaxis: { rangemode: "tozero" }, legend: false, shapes: [band].concat(rl.shapes), annotations: rl.annotations }),
      () => ({ head: ["time", "VPD kPa", "±1σ"], rows: rows(d, ["vpd", "vpd_sigma"], [3, 3]) }), "Shaded band = comfortable range for general indoor plants. Thin ribbon = ±1σ from DHT22 accuracy.");

    const tt = [line(p.s1, { name: "Soil", x: d.x, y: d.soil_t, hovertemplate: "%{y:.1f} °C" }), line(p.s2, { name: "Air", x: d.x, y: d.air_t, hovertemplate: "%{y:.1f} °C" })];
    const th = S.L.cards.thermal || {};
    draw(P_("p-temp"), tt, baseLayout(p, { height: 230, ytitle: "°C" }), () => ({ head: ["time", "soil °C", "air °C"], rows: rows(d, ["soil_t", "air_t"], [2, 2]) }),
      th.status === "ok" ? "Soil follows air like a one-pole filter: time constant " + th.tau_h.toFixed(1) + " h, so a 24 h swing is damped to " + Math.round(th.amplitude_ratio_24h * 100) + " % and arrives " + th.phase_lag_h.toFixed(1) + " h late." : "Thermal lag needs about two days of paired soil and air data.");

    const c = cfg.condensation, rl2 = refLines([[c.warn_c, "warn " + c.warn_c + " °C"], [c.crit_c, "critical " + c.crit_c + " °C"]], p);
    draw(P_("p-dew"), [line(p.s1, { name: "Margin", x: d.x, y: d.margin, hovertemplate: "%{y:.1f} °C" })], baseLayout(p, { height: 190, ytitle: "°C", legend: false, shapes: rl2.shapes, annotations: rl2.annotations }),
      () => ({ head: ["time", "dew point °C", "margin °C"], rows: rows(d, ["dew", "margin"], [2, 2]) }), "When the soil surface is close to the dew point, moisture condenses on it and mould risk rises.");

    const h = cfg.humid, rl3 = refLines([[h.elevated_h, h.elevated_h + " h"], [h.high_h, h.high_h + " h"]], p);
    draw(P_("p-humid"), [line(p.s1, { name: "Exposure", x: d.x, y: d.humid_h, hovertemplate: "%{y:.1f} h" })], baseLayout(p, { height: 190, ytitle: "hours", yaxis: { rangemode: "tozero" }, legend: false, shapes: rl3.shapes, annotations: rl3.annotations }),
      () => ({ head: ["time", "exposure h"], rows: rows(d, ["humid_h"], [2]) }), "Counts up while RH ≥ " + h.rh_threshold + " % (5–40 °C) and drains at half speed otherwise. Not a mould measurement.");
    return d;
  }

  function renderLight(p) {
    const [t0, t1] = range(), cfg = S.L.config;
    const d = slice(S.ser, ["ppfd"], t0, t1);
    draw(P_("p-ppfd"), [line(p.s7, { name: "PPFD", x: d.x, y: d.ppfd, hovertemplate: "%{y:.0f} µmol/m²/s" })], baseLayout(p, { height: 210, ytitle: "µmol m⁻² s⁻¹", yaxis: { rangemode: "tozero" }, legend: false }),
      () => ({ head: ["time", "PPFD"], rows: rows(d, ["ppfd"], [1]) }), "Lux × " + cfg.ppfd_per_lux + " (daylight + lamp blend). The BH1750 saturates near 54,600 lx.");

    const D = (S.daily.daily || []).slice(-(S.rangeDays ? Math.max(S.rangeDays, 3) : 9999));
    const ok = D.filter((r) => r.dli != null);
    const tr = [{ type: "bar", name: "DLI (complete days)", x: ok.map((r) => r.date), y: ok.map((r) => r.dli), marker: { color: p.s7, cornerradius: 4 }, width: barW(P_("p-dli"), D.length), error_y: { type: "data", array: ok.map((r) => r.dli_unc), color: p.ink2, thickness: 1.5, width: 3 }, hovertemplate: "%{y:.1f} ± %{error_y.array:.1f} mol/m²/d", __keep: ok.length > 0 }];
    const part = D.filter((r) => r.dli == null);
    if (part.length) tr.push({ type: "scatter", mode: "markers", name: "Incomplete day (so far)", x: part.map((r) => r.date), y: part.map((r) => r.dli_so_far), marker: { symbol: "circle-open", size: 9, color: p.ink2, line: { width: 2 } }, hovertemplate: "%{y:.1f} mol/m²/d so far" });
    const band = { type: "rect", xref: "paper", x0: 0, x1: 1, yref: "y", y0: cfg.dli[0], y1: cfg.dli[1], fillcolor: withAlpha(p.muted, 0.14), line: { width: 0 }, layer: "below" };
    const lay = baseLayout(p, { height: 230, ytitle: "mol m⁻² day⁻¹", yaxis: { rangemode: "tozero" }, hovermode: "closest", shapes: [band], xaxis: { type: "category", gridcolor: p.grid, tickfont: { color: p.ink2, size: 11 }, zeroline: false }, legend: part.length > 0 });
    lay.bargap = 0.35;
    draw(P_("p-dli"), tr, lay, () => ({ head: ["date", "DLI", "± ", "coverage", "photoperiod h", "peak PPFD"], rows: D.map((r) => [r.date, r.dli, r.dli_unc, r.coverage, r.photoperiod_h, r.ppfd_peak]) }),
      "Shaded band = " + cfg.dli[0] + "–" + cfg.dli[1] + " mol/m²/d (typical foliage plants; edit in thresholds.yaml). Error bars ≈ ±30 %: sensor error plus the unknown light-source mix. Days with under 80 % valid samples are not reported as complete.");

    const H = S.daily.heatmap || { dates: [], z: [] };
    const hz = { type: "heatmap", x: H.dates, y: H.hours, z: H.z, colorscale: [[0, p.seqLo], [1, p.seqHi]], xgap: 2, ygap: 2, hoverongaps: false, colorbar: { title: { text: "µmol/m²/s", font: { size: 11, color: p.ink2 } }, tickfont: { size: 11, color: p.ink2 }, thickness: 12, len: 0.9, outlinewidth: 0 }, hovertemplate: "%{x} %{y}:00<br>%{z:.0f} µmol/m²/s<extra></extra>" };
    hz.__keep = H.dates.length > 0;
    const lay2 = baseLayout(p, { height: 320, ytitle: "hour of day (site time)", hovermode: "closest", legend: false, yaxis: { autorange: "reversed", dtick: 3, range: [23.5, -0.5] }, xaxis: { type: "category", tickfont: { size: 11, color: p.ink2 }, gridcolor: p.grid, zeroline: false, tickmode: "array", tickvals: H.dates.filter((_, i) => i % Math.max(1, Math.ceil(H.dates.length / 10)) === 0), tickangle: 0 } });
    lay2.yaxis.autorange = false;
    draw(P_("p-heat"), H.dates.length ? [hz] : [], lay2, () => ({ head: ["hour"].concat(H.dates.slice(-10)), rows: H.hours.map((h, i) => [h + ":00"].concat(H.z[i].slice(-10).map((v) => (v == null ? null : +v.toFixed(0))))) }), "When the plant gets its light: darker cells are brighter hours.");
  }

  function renderRaw(p) {
    const [t0, t1] = range(), R = S.raw, cad = (S.L.cadence_s || 900) * 2.5;
    const keys = ["m1_mv", "m2_mv", "m3_mv", "m1_mad", "m2_mad", "m3_mad", "m1_state", "m2_state", "m3_state", "soil_t", "air_t", "rh", "lux", "rssi", "excluded", "vbat_mv"];
    const d0 = slice(R, keys, t0, t1), d = gapify(d0, keys, cad);
    const tab = (cols, nds) => () => ({ head: ["time"].concat(cols.map((c) => c[1])), rows: rows(d0, cols.map((c) => c[0]), nds) });
    const stNames = Object.fromEntries(Object.entries(S.L.state_codes || {}).map(([k, v]) => [v, k]));

    // probe voltage with the states that took a reading out of the analysis
    const col = [p.s1, p.s2, p.s3];
    const tr = ["m1", "m2", "m3"].map((k, i) => line(col[i], { name: "Probe " + (i + 1), x: d.x, y: d[k + "_mv"], hovertemplate: "%{y:.0f} mV" }));
    const bx = [], by = [], bt = [];
    d0.t.forEach((t, i) => ["m1", "m2", "m3"].forEach((k, j) => { const s = d0[k + "_state"][i]; if (s && num(d0[k + "_mv"][i])) { bx.push(d0.x[i]); by.push(d0[k + "_mv"][i]); bt.push("probe " + (j + 1) + ": " + (stNames[s] || "excluded")); } }));
    if (bx.length) tr.push({ type: "scatter", mode: "markers", name: "left out of analysis", x: bx, y: by, text: bt, marker: { symbol: "circle-open", size: 9, color: p.ink2, line: { width: 2 } }, hovertemplate: "%{y:.0f} mV · %{text}" });
    draw(P_("r-mv"), tr, baseLayout(p, { height: 230, ytitle: "mV at the ADC pin", yaxis: { rangemode: "tozero" } }), tab([["m1_mv", "P1 mV"], ["m2_mv", "P2 mV"], ["m3_mv", "P3 mV"]], [0, 0, 0]),
      "Higher = drier. Rings: reading exists but was rejected (below " + S.L.config.mv_min_valid + " mV looks like a floating pin, near the supply looks unplugged, noisy, or flagged by the firmware).");

    const trm = ["m1", "m2", "m3"].map((k, i) => line(col[i], { name: "Probe " + (i + 1), x: d.x, y: d[k + "_mad"], hovertemplate: "%{y:.0f} mV" }));
    const rl = refLines([[S.L.config.mad_max_mv, "rejected above " + S.L.config.mad_max_mv + " mV"]], p);
    draw(P_("r-mad"), trm, baseLayout(p, { height: 170, ytitle: "mV", yaxis: { rangemode: "tozero" }, shapes: rl.shapes, annotations: rl.annotations }), tab([["m1_mad", "P1 MAD"], ["m2_mad", "P2 MAD"], ["m3_mad", "P3 MAD"]], [0, 0, 0]),
      "Scatter of the 15 ADC samples inside one reading. High values mean a loose wire or a probe drying out at the surface.");

    const ring = (key, bit, name, unit, nd) => { const xs = [], ys = []; d0.t.forEach((t, i) => { if ((d0.excluded[i] & bit) && num(d0[key][i])) { xs.push(d0.x[i]); ys.push(d0[key][i]); } }); return xs.length ? [{ type: "scatter", mode: "markers", name: "left out of analysis", x: xs, y: ys, marker: { symbol: "circle-open", size: 9, color: p.ink2, line: { width: 2 } }, hovertemplate: "%{y:." + nd + "f} " + unit + " (left out)" }] : []; };
    draw(P_("r-soil"), [line(p.s1, { name: "Soil temperature", x: d.x, y: d.soil_t, hovertemplate: "%{y:.2f} °C" })].concat(ring("soil_t", 1, "", "°C", 2)), baseLayout(p, { height: 170, ytitle: "°C", legend: false }), tab([["soil_t", "°C"]], [2]), "");
    draw(P_("r-air"), [line(p.s2, { name: "Air temperature", x: d.x, y: d.air_t, hovertemplate: "%{y:.1f} °C" })].concat(ring("air_t", 2, "", "°C", 1)), baseLayout(p, { height: 170, ytitle: "°C", legend: false }), tab([["air_t", "°C"]], [1]), "");
    draw(P_("r-rh"), [line(p.s1, { name: "Relative humidity", x: d.x, y: d.rh, hovertemplate: "%{y:.1f} %" })].concat(ring("rh", 4, "", "%", 1)), baseLayout(p, { height: 170, ytitle: "% RH", yaxis: { range: [0, 100] }, legend: false }), tab([["rh", "% RH"]], [1]), "");

    // lux on a log axis: zero cannot be drawn there, so it is plotted at 0.5 lx and the tooltip shows the real value
    const luxPlot = d.lux.map((v) => (num(v) ? Math.max(v, 0.5) : null));
    const tl = line(p.s7, { name: "Illuminance", x: d.x, y: luxPlot, customdata: d.lux, hovertemplate: "%{customdata:.0f} lx" });
    draw(P_("r-lux"), [tl].concat(ring("lux", 8, "", "lx", 0)), baseLayout(p, { height: 190, ytitle: "lux (log scale)", yaxis: { type: "log" }, legend: false }), tab([["lux", "lux"]], [1]), "Log scale; 0 lx is drawn at 0.5 lx. Very bright periods can saturate the sensor near 54,600 lx.");
    draw(P_("r-rssi"), [line(p.s1, { name: "RSSI", x: d.x, y: d.rssi, hovertemplate: "%{y:.0f} dBm" })], baseLayout(p, { height: 150, ytitle: "dBm", legend: false }), tab([["rssi", "dBm"]], [0]), "Closer to 0 is better; below about −80 dBm uploads start failing.");
  }

  // ------------------------------------------------------------------ tables, health, explain
  function simpleTable(host, head, rowsData, empty) {
    host.textContent = "";
    if (!rowsData.length) { host.appendChild(el("p", "empty", empty)); return; }
    const t = el("table"), th = el("thead"), tr = el("tr");
    head.forEach((h) => tr.appendChild(el("th", null, h))); th.appendChild(tr); t.appendChild(th);
    const b = el("tbody"); rowsData.forEach((r) => { const row = el("tr"); r.forEach((c) => row.appendChild(el("td", null, c == null ? "–" : String(c)))); b.appendChild(row); }); t.appendChild(b); host.appendChild(t);
  }
  function renderTables() {
    const evs = (S.ev.events || []).slice().reverse();
    simpleTable($("#events-table"), ["time (site)", "source", "rise", "peak", "probes"], evs.map((e) => [nice(e.t), ({ both: "detected + logged", detected: "detected", manual: "logged only" })[e.source], e.amplitude != null ? "+" + e.amplitude.toFixed(2) : null, e.w_peak != null ? e.w_peak.toFixed(2) : null, e.n_valid ? e.n_probes + " of " + e.n_valid : null]), "No watering events found yet.");
    const D = (S.daily.daily || []).slice().reverse().slice(0, 60);
    simpleTable($("#daily-table"), ["date", "DLI", "photoperiod h", "VPD mean", "T min–max", "events"], D.map((r) => [r.date, r.dli != null ? r.dli.toFixed(1) : (r.dli_so_far != null ? r.dli_so_far.toFixed(1) + "*" : null), r.photoperiod_h, r.vpd_mean, (r.air_t_min != null ? r.air_t_min.toFixed(1) + "–" + r.air_t_max.toFixed(1) : null), r.water_events]), "No days yet.");
  }
  function meter(frac, color) { const m = el("div", "meter"); const i = el("i"); i.style.width = Math.round(Math.max(0, Math.min(1, frac || 0)) * 100) + "%"; i.style.background = color; m.appendChild(i); return m; }
  function renderHealth() {
    const L = S.L, H = L.health || {}, host = $("#health"), p = pal(); host.textContent = "";
    const c1 = el("div", "card"); c1.appendChild(el("h3", null, "Uploads"));
    const kv = el("dl", "kv");
    [["Last reading", nice(tEnd())], ["Cadence", H.cadence_s ? Math.round(H.cadence_s / 60) + " min" : "–"], ["Rows used / received", (H.rows_used || 0) + " / " + (H.rows_in || 0)],
      ["Without timestamp (left out)", H.rows_no_ts || 0], ["Duplicates dropped", H.rows_duplicate || 0], ["Spikes removed", H.spikes_removed || 0], ["Gaps in data", H.gaps || 0],
      ["Records lost, last 7 d", (H.seq_lost_7d || 0) + " of " + (H.seq_expected_7d || 0)], ["Cold restarts, last 7 d", H.restarts_7d || 0], ["Wi-Fi RSSI", S.L.now.rssi != null ? S.L.now.rssi + " dBm" : "–"],
      ["Battery", S.L.now.vbat_mv ? S.L.now.vbat_mv + " mV" : "not measured"]].forEach((r) => { kv.appendChild(el("dt", null, r[0])); kv.appendChild(el("dd", null, String(r[1]))); });
    c1.appendChild(kv); host.appendChild(c1);

    const c2 = el("div", "card"); c2.appendChild(el("h3", null, "Valid readings, last 24 h"));
    const vf = H.valid_frac_24h || {};
    [["m1", "Probe 1"], ["m2", "Probe 2"], ["m3", "Probe 3"], ["soil_t", "Soil temperature"], ["air_t", "Air temperature"], ["rh", "Humidity"], ["lux", "Light"]].forEach(([k, n]) => {
      const f = vf[k]; const row = el("div", "sub", n + " · " + (f == null ? "–" : Math.round(f * 100) + " %")); c2.appendChild(row); c2.appendChild(meter(f, f != null && f < 0.5 ? p.crit : p.s1));
    });
    host.appendChild(c2);

    const c3 = el("div", "card"); c3.appendChild(el("h3", null, "Probe states, last 24 h"));
    const ps = H.probe_states_24h || {}, kv3 = el("dl", "kv");
    Object.keys(ps).forEach((k) => { kv3.appendChild(el("dt", null, "Probe " + k.slice(1))); kv3.appendChild(el("dd", null, Object.entries(ps[k]).map(([s, n]) => s + " " + n).join(", ") || "–")); });
    c3.appendChild(kv3);
    const V = L.validation || {};
    c3.appendChild(el("h3", null, "Watering detector vs your log")).style.marginTop = "12px";
    const kv4 = el("dl", "kv");
    if (V.logged) [["Logged waterings", V.logged], ["Detected", V.detected], ["Matched", V.matched], ["Recall", V.recall != null ? Math.round(V.recall * 100) + " %" : "–"], ["Precision", V.precision != null ? Math.round(V.precision * 100) + " %" : "–"]].forEach((r) => { kv4.appendChild(el("dt", null, r[0])); kv4.appendChild(el("dd", null, String(r[1]))); });
    else c3.appendChild(el("p", "sub", "No waterings in config/manual_events.csv within the data range. Logging a few lets this page check itself."));
    c3.appendChild(kv4); host.appendChild(c3);
  }
  function renderExplain() {
    const ex = S.L.explain || {}, host = $("#explain"); host.textContent = "";
    const names = { moisture: "Moisture index", vpd: "VPD", dew: "Dew point and condensation margin", ppfd: "Light and PPFD", dli: "Daily light integral", forecast: "Water-soon window", thermal: "Soil thermal lag", events: "Watering events" };
    Object.keys(names).forEach((k) => { if (!ex[k]) return; const d = el("details"); d.appendChild(el("summary", null, names[k])); d.appendChild(el("p", null, ex[k])); host.appendChild(d); });
    if (!host.children.length) host.appendChild(el("p", "muted", "Nothing to show yet."));
    $("#foot").textContent = "Data from " + (S.L.data_from ? nice(Date.parse(S.L.data_from) / 1000) : "–") + " to " + nice(tEnd()) + " · source: " + S.L.source + " · pipeline v" + S.L.version;
  }

  // ------------------------------------------------------------------ CSV
  function csv(name, cols, getRows) {
    const rowsOut = [cols.join(",")].concat(getRows().map((r) => r.map((v) => (v == null ? "" : String(v))).join(",")));
    const a = el("a"); a.href = URL.createObjectURL(new Blob([rowsOut.join("\n")], { type: "text/csv" })); a.download = name; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  }
  function wireDownloads() {
    $("#dl-raw").onclick = () => { const [t0, t1] = range(), R = S.raw, [a, b] = bounds(R.t, t0, t1); const ks = Object.keys(R).filter((k) => k !== "t");
      csv("soil_raw_" + S.tz.replace("/", "-") + ".csv", ["time_utc_iso"].concat(ks), () => R.t.slice(a, b).map((t, i) => [new Date(t * 1000).toISOString()].concat(ks.map((k) => R[k][a + i])))); };
    $("#dl-der").onclick = () => { const [t0, t1] = range(), s = S.ser, [a, b] = bounds(s.t, t0, t1); const ks = Object.keys(s).filter((k) => k !== "t" && Array.isArray(s[k]) && s[k].length);
      csv("soil_derived.csv", ["time_utc_iso"].concat(ks), () => s.t.slice(a, b).map((t, i) => [new Date(t * 1000).toISOString()].concat(ks.map((k) => s[k][a + i])))); };
  }

  // ------------------------------------------------------------------ render
  function renderAll() {
    const p = pal();
    S.plots.forEach((P) => P.box.classList.add("busy"));
    requestAnimationFrame(() => {
      renderTiles(); renderFlags(); renderHeader();
      renderMoisture(p); renderK(p); renderAir(p); renderLight(p); renderRaw(p);
      renderTables(); renderHealth(); renderExplain();
      S.plots.forEach((P) => P.box.classList.remove("busy"));
      document.body.dataset.ready = "1";
    });
  }

  function wireControls() {
    document.querySelectorAll("#range button").forEach((b) => b.addEventListener("click", () => {
      document.querySelectorAll("#range button").forEach((x) => x.removeAttribute("aria-pressed")); b.setAttribute("aria-pressed", "true");
      S.rangeDays = +b.dataset.r; S.zoom = null; store.set("range", b.dataset.r); renderAll();
    }));
    $("#theme").addEventListener("click", () => { const t = currentIsDark() ? "light" : "dark"; applyTheme(t); store.set("theme", t); renderAll(); });
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (!store.get("theme")) renderAll(); });
    const saved = store.get("range");
    if (saved != null && document.querySelector('#range button[data-r="' + saved + '"]')) {
      S.rangeDays = +saved; document.querySelectorAll("#range button").forEach((x) => x.removeAttribute("aria-pressed")); document.querySelector('#range button[data-r="' + saved + '"]').setAttribute("aria-pressed", "true");
    }
  }

  async function main() {
    const t = store.get("theme"); if (t) applyTheme(t);
    wireControls();
    try { await loadAll(); }
    catch (e) { $("#subtitle").textContent = "Could not load data (" + e.message + ")."; const b = $("#banners"); const d = el("div", "banner"); d.appendChild(icon("crit")); d.appendChild(el("span", null, "No data files were found. After the first scheduled run (or running the job locally) they appear next to this page in data/.")); b.appendChild(d); return; }
    setTz(S.L.timezone); buildPanels(); wireDownloads(); renderAll();
  }
  main();
})();
