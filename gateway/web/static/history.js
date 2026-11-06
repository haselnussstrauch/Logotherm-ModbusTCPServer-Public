"use strict";
// Seite „6 Verlauf“: Aufzeichnung (Einstellungen, Tage löschen, CSV) und Diagramme.
// Nutzt h(), api(), toast(), fail(), field(), sectionCard(), CFG, PAGES, timer aus app.js.

// Kategoriale Reihenfolge (validierte Referenzpalette, fest – nie zyklisch, nie neu generiert)
const SERIES_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"];
const MAX_SERIES = SERIES_COLORS.length;
const RANGES = [["today", "Heute"], ["24h", "24 Stunden"], ["7d", "7 Tage"], ["14d", "14 Tage"], ["day", "Tag wählen"]];

const hist = loadHistState();
histFromLink();

// Link auf ein Diagramm: #history?dev=can-02&range=24h&sel=value:0x0103,value:0x0105
function histFromLink() {
  const [pg, q] = location.hash.slice(1).split("?");
  if (pg !== "history" || !q) return;
  const p = new URLSearchParams(q);
  if (p.get("dev")) { hist.dev = p.get("dev"); hist.sel = []; }
  if (p.get("range") && RANGES.some(([k]) => k === p.get("range"))) hist.range = p.get("range");
  if (p.get("day")) { hist.day = p.get("day"); hist.range = "day"; }
  if (p.get("sel")) {
    hist.sel = p.get("sel").split(",").slice(0, MAX_SERIES).map((s, slot) => {
      const [kind, key] = s.split(":");
      return { kind: kind === "param" ? "param" : "value", key: parseInt(key), slot };
    }).filter((x) => Number.isFinite(x.key));
  }
  saveHistState();
}
function histLink() {
  const q = new URLSearchParams({ dev: hist.dev || "", range: hist.range });
  if (hist.range === "day" && hist.day) q.set("day", hist.day);
  q.set("sel", hist.sel.map((x) => `${x.kind}:0x${x.key.toString(16).toUpperCase().padStart(4, "0")}`).join(","));
  return `${location.origin}${location.pathname}#history?${q}`;
}

function loadHistState() {
  let s = {};
  try { s = JSON.parse(localStorage.getItem("gw-hist") || "{}"); } catch (e) { s = {}; }
  return { range: s.range || "today", day: s.day || "", dev: s.dev || null, sel: s.sel || [] };
  // sel: [{kind, key, slot}] – Farbe (slot) bleibt am Wert, auch wenn andere abgewählt werden
}
function saveHistState() {
  localStorage.setItem("gw-hist", JSON.stringify({ range: hist.range, day: hist.day, dev: hist.dev, sel: hist.sel }));
}

function svg(tag, attrs, ...kids) {
  const e = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v != null) e.setAttribute(k, v);
  for (const c of kids.flat()) if (c != null && c !== false) e.append(c.nodeType ? c : document.createTextNode(String(c)));
  return e;
}

const fmtSize = (b) => b == null ? "–" : b < 1024 * 1024 ? `${(b / 1024).toFixed(0)} kB` : b < 1024 ** 3 ? `${(b / 1024 / 1024).toFixed(1)} MB` : `${(b / 1024 ** 3).toFixed(2)} GB`;
const WD = ["So", "Mo", "Di", "Mi", "Do", "Fr", "Sa"];
const pad2 = (n) => String(n).padStart(2, "0");
const fmtDate = (d) => `${WD[d.getDay()]} ${pad2(d.getDate())}.${pad2(d.getMonth() + 1)}.`;
const fmtTime = (d) => `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
const fmtDateTime = (d) => `${fmtDate(d)} ${fmtTime(d)}:${pad2(d.getSeconds())}`;
const isoDay = (d) => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;

function rangeBounds() {
  const now = new Date();
  const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  switch (hist.range) {
    case "24h": return [now - 86400e3, +now, true];
    case "7d": return [now - 7 * 86400e3, +now, true];
    case "14d": return [now - 14 * 86400e3, +now, true];
    case "day": {
      if (!hist.day) return [+midnight, +now, true];
      const [y, m, d] = hist.day.split("-").map(Number);
      const a = new Date(y, m - 1, d), b = new Date(y, m - 1, d + 1);
      return [+a, Math.min(+b, +now), +b > +now];
    }
    default: return [+midnight, +now, true];
  }
}

// --- Seite -----------------------------------------------------------------------------------
PAGES.history = async function () {
  const st = await api("GET", "/api/history");
  const settings = sectionCard("Aufzeichnung", "history", [
    field("history", "enabled", "Werte aufzeichnen", "bool"),
    field("history", "interval_s", "Abstand (s)", "number", { step: "any" }),
    field("history", "retention_days", "Aufbewahren (Tage)", "number"),
    h("p", { class: "hint" }, "Gespeichert werden alle aktiven Messwerte und Parameter mit Status ok, " +
      "eine Datei je Tag. Ältere Tage werden automatisch gelöscht. Änderungen unter „4 Speichern“ übernehmen."),
  ], h("p", { class: "hint" },
    `Aktuell: ${st.enabled ? "an" : "aus"}, alle ${st.interval_s} s, ${st.retention_days} Tage · ` +
    `belegt ${fmtSize(st.total_size)} · frei ${fmtSize(st.disk_free)}`,
    st.warning ? h("div", { class: "warn" }, st.warning) : ""));

  const chartCard = h("section", { class: "card", id: "hist-chart" });
  const daysCard = h("section", { class: "card", id: "hist-days" });
  $main.replaceChildren(chartCard, daysCard, settings);
  renderDays(daysCard, st.days);
  await renderChartCard(chartCard);
};

function renderDays(box, days) {
  box.replaceChildren(h("h2", {}, "Gespeicherte Tage"),
    days.length ? h("div", { class: "scroll", style: "max-height:320px" }, h("table", {},
      h("tr", {}, ["Datum", "Größe", "", ""].map((t) => h("th", {}, t))),
      days.map((d) => {
        const [y, m, dd] = d.date.split("-").map(Number);
        return h("tr", {},
          h("td", {}, `${fmtDate(new Date(y, m - 1, dd))}${y}`),
          h("td", { class: "num" }, fmtSize(d.size)),
          h("td", {}, h("a", { href: `/api/history/days/${d.date}.csv`, download: `aufzeichnung-${d.date}.csv` }, "CSV herunterladen"),
            " · ", h("a", { href: "#", onclick: (e) => { e.preventDefault(); hist.range = "day"; hist.day = d.date; saveHistState(); renderChartCard(document.getElementById("hist-chart")).catch(fail); } }, "anzeigen")),
          h("td", {}, h("button", {
            class: "btn sec", onclick: async () => {
              if (!confirm(`Aufzeichnung vom ${d.date} endgültig löschen?`)) return;
              try {
                await api("DELETE", `/api/history/days/${d.date}`);
                toast(`Aufzeichnung ${d.date} gelöscht.`);
                renderDays(box, (await api("GET", "/api/history")).days);
              } catch (e) { fail(e); }
            }
          }, "Löschen")));
      }))) : h("p", { class: "hint" }, "Noch keine Aufzeichnung vorhanden."));
}

// --- Diagramm: Auswahl -----------------------------------------------------------------------
async function renderChartCard(box) {
  const [t0, t1, live] = rangeBounds();
  const cat = (await api("GET", `/api/history/catalog?from=${t0 / 1000}&to=${t1 / 1000}`)).series;
  const devs = [...new Map(cat.map((s) => [s.dev, s.dev_name || s.dev])).entries()];
  if (!devs.some(([id]) => id === hist.dev)) hist.dev = devs.length ? devs[0][0] : null;
  const series = cat.filter((s) => s.dev === hist.dev);
  hist.sel = hist.sel.filter((x) => series.some((s) => s.kind === x.kind && s.key === x.key));
  if (!hist.sel.length && series.length) hist.sel = [{ kind: series[0].kind, key: series[0].key, slot: 0 }];
  saveHistState();

  const rangeBtns = h("div", { class: "seg" }, RANGES.map(([k, label]) => h("button", {
    class: hist.range === k ? "on" : "", onclick: () => { hist.range = k; saveHistState(); renderChartCard(box).catch(fail); }
  }, label)));
  const dayInput = h("input", { type: "date", value: hist.day || isoDay(new Date()), style: hist.range === "day" ? "" : "display:none" });
  dayInput.onchange = () => { hist.day = dayInput.value; saveHistState(); renderChartCard(box).catch(fail); };
  const devSel = h("select", {}, devs.map(([id, name]) => h("option", { value: id, selected: id === hist.dev }, `${name} (${id})`)));
  devSel.onchange = () => { hist.dev = devSel.value; hist.sel = []; saveHistState(); renderChartCard(box).catch(fail); };

  const plot = h("div", { class: "chart" });
  const picker = h("div", { class: "picker" });
  const tableBox = h("div");
  const filterIn = h("input", { type: "text", placeholder: "Werte filtern …", size: 22 });
  const drawPicker = () => {
    const f = filterIn.value.trim().toLowerCase();
    picker.replaceChildren(...["value", "param"].flatMap((kind) => {
      const rows = series.filter((s) => s.kind === kind && (!f || `${s.name} ${s.nr}`.toLowerCase().includes(f)));
      if (!rows.length) return [];
      return [h("div", { class: "hint" }, kind === "value" ? "Messwerte" : "Parameter"), ...rows.map((s) => {
        const sel = hist.sel.find((x) => x.kind === s.kind && x.key === s.key);
        const chk = h("input", { type: "checkbox", checked: !!sel, disabled: !sel && hist.sel.length >= MAX_SERIES });
        chk.onchange = () => {
          if (chk.checked) {
            const used = new Set(hist.sel.map((x) => x.slot));
            hist.sel.push({ kind: s.kind, key: s.key, slot: [...Array(MAX_SERIES).keys()].find((i) => !used.has(i)) });
          } else hist.sel = hist.sel.filter((x) => !(x.kind === s.kind && x.key === s.key));
          saveHistState(); drawPicker(); loadAndDraw();
        };
        return h("label", { class: "block", title: s.nr }, chk,
          sel ? h("span", { class: "key", style: `background:${SERIES_COLORS[sel.slot]}` }) : h("span", { class: "key off" }),
          ` ${s.name}`, s.unit ? h("span", { class: "hint" }, ` ${s.unit}`) : "");
      })];
    }));
  };
  filterIn.oninput = drawPicker;

  let data = null;
  const loadAndDraw = async () => {
    const [a, b] = rangeBounds();
    if (!hist.sel.length) { data = null; plot.replaceChildren(h("p", { class: "hint" }, "Links Werte ankreuzen (bis zu 8).")); tableBox.replaceChildren(); return; }
    const points = Math.max(200, Math.min(1500, Math.round(plot.clientWidth || 900)));
    const res = {};
    for (const kind of ["value", "param"]) {
      const keys = hist.sel.filter((x) => x.kind === kind).map((x) => x.key);
      if (!keys.length) continue;
      const r = await api("GET", `/api/history/series?device=${encodeURIComponent(hist.dev)}&kind=${kind}&keys=${keys.join(",")}&from=${a / 1000}&to=${b / 1000}&points=${points}`);
      for (const s of r.series) res[`${kind}:${s.key}`] = { points: s.points, bucket: r.bucket_s, interval: r.interval_s };
    }
    data = { from: a / 1000, to: b / 1000, series: hist.sel.map((x) => {
      const m = series.find((s) => s.kind === x.kind && s.key === x.key);
      const r = res[`${x.kind}:${x.key}`] || { points: [], bucket: 0, interval: 60 };
      return { ...x, name: m.name, unit: m.unit || "", nr: m.nr, texts: m.texts || null, color: SERIES_COLORS[x.slot], ...r };
    }) };
    drawCharts(plot, data);
    if (tableBox.dataset.open) drawTable(tableBox, data);
  };

  const tableBtn = h("button", { class: "btn sec" }, "Tabelle anzeigen");
  tableBtn.onclick = () => {
    if (tableBox.dataset.open) { delete tableBox.dataset.open; tableBox.replaceChildren(); tableBtn.textContent = "Tabelle anzeigen"; }
    else { tableBox.dataset.open = "1"; tableBtn.textContent = "Tabelle ausblenden"; if (data) drawTable(tableBox, data); }
  };

  // replaceChildren braucht einzelne Knoten -> Arrays immer ausbreiten
  box.replaceChildren(h("h2", {}, "Diagramm"), ...(devs.length ? [
    h("div", { class: "filters" }, rangeBtns, dayInput, h("label", {}, "Gerät ", devSel),
      h("button", { class: "btn sec", onclick: () => loadAndDraw().catch(fail) }, "Aktualisieren"), tableBtn,
      h("button", { class: "btn sec", title: "Link auf dieses Diagramm (Lesezeichen)", onclick: async () => {
        const url = histLink();
        try { await navigator.clipboard.writeText(url); toast("Link kopiert."); } catch (e) { prompt("Link auf dieses Diagramm:", url); }
      } }, "Link")),
    h("div", { class: "chart-layout" }, h("div", {}, filterIn, picker), plot),
    tableBox,
  ] : [
    h("div", { class: "filters" }, rangeBtns, dayInput),
    h("p", { class: "hint" }, "Im gewählten Zeitraum gibt es keine Aufzeichnung. Sie beginnt, sobald Geräte abgefragt " +
      "werden (erster Punkt nach einem Aufzeichnungsabstand)."),
  ]));
  if (!devs.length) return;
  drawPicker();
  await loadAndDraw();
  if (timer) { clearInterval(timer); timer = null; }
  if (live) timer = setInterval(() => { if (document.getElementById("hist-chart")) loadAndDraw().catch(() => {}); }, 60000);
  window.onresize = () => { if (data && document.getElementById("hist-chart")) drawCharts(plot, data); };
}

// --- Diagramm: Zeichnen ----------------------------------------------------------------------
function niceStep(span, target) {
  const raw = span / Math.max(1, target);
  const p = 10 ** Math.floor(Math.log10(raw));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= raw) return m * p;
  return 10 * p;
}
function yTicks(lo, hi) {
  if (!(isFinite(lo) && isFinite(hi))) return { lo: 0, hi: 1, ticks: [0, 1], dec: 0 };
  if (hi - lo < 1e-9) { const d = Math.abs(hi) * 0.05 || 1; lo -= d; hi += d; }
  const step = niceStep(hi - lo, 4);
  const a = Math.floor(lo / step) * step, b = Math.ceil(hi / step) * step;
  const ticks = [];
  for (let v = a; v <= b + step / 2; v += step) ticks.push(+v.toFixed(10));
  return { lo: a, hi: b, ticks, dec: Math.max(0, -Math.floor(Math.log10(step) + 1e-9)) };
}
const TIME_STEPS = [300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800];
function xTicks(t0, t1, width) {
  const want = Math.max(2, Math.floor(width / 90));
  const step = TIME_STEPS.find((s) => (t1 - t0) / s <= want) || 604800;
  const off = new Date(t0 * 1000).getTimezoneOffset() * 60;      // lokale Mitternacht als Raster
  const ticks = [];
  for (let t = Math.ceil((t0 - off) / step) * step + off; t <= t1; t += step) ticks.push(t);
  return { ticks, step };
}

function drawCharts(box, data) {
  const width = Math.max(320, box.clientWidth || 900);
  const units = [...new Set(data.series.map((s) => s.unit))];   // je Einheit ein Diagramm (eine y-Achse)
  const PH = 200, M = { l: 56, r: 12, t: 8, b: 22 };
  const iw = width - M.l - M.r, ih = PH - M.t - M.b;
  const xs = (t) => M.l + ((t - data.from) / (data.to - data.from)) * iw;
  const xt = xTicks(data.from, data.to, iw);
  const panels = [];
  const cross = [];
  for (const unit of units) {
    const ss = data.series.filter((s) => s.unit === unit);
    let lo = Infinity, hi = -Infinity;
    for (const s of ss) for (const p of s.points) { lo = Math.min(lo, p[2]); hi = Math.max(hi, p[3]); }
    const yt = yTicks(lo, hi);
    const ys = (v) => M.t + ih - ((v - yt.lo) / (yt.hi - yt.lo)) * ih;
    const g = svg("svg", { width, height: PH, viewBox: `0 0 ${width} ${PH}`, class: "plot", role: "img",
      "aria-label": `Verlauf ${ss.map((s) => s.name).join(", ")}${unit ? " in " + unit : ""}` });
    // Raster und Achsen (zurückhaltend)
    for (const v of yt.ticks) {
      g.append(svg("line", { x1: M.l, x2: width - M.r, y1: ys(v), y2: ys(v), class: "grid" }),
        svg("text", { x: M.l - 6, y: ys(v) + 4, class: "tick", "text-anchor": "end" }, v.toFixed(yt.dec).replace(".", ",")));
    }
    for (const t of xt.ticks) {
      const d = new Date(t * 1000);
      const label = xt.step >= 86400 || (d.getHours() === 0 && d.getMinutes() === 0) ? fmtDate(d) : fmtTime(d);
      g.append(svg("line", { x1: xs(t), x2: xs(t), y1: M.t, y2: M.t + ih, class: "grid v" }),
        svg("text", { x: xs(t), y: PH - 6, class: "tick", "text-anchor": "middle" }, label));
    }
    if (unit) g.append(svg("text", { x: 4, y: M.t + 10, class: "tick unit" }, unit));
    // Kurven: Min/Max-Band (bei Verdichtung) + Mittelwert-Linie; Lücken bleiben Lücken
    for (const s of ss) {
      const gap = Math.max(3 * s.interval, 2.5 * s.bucket);
      let line = "", band = [], segs = [];
      let prev = null;
      for (const p of s.points) {
        const brk = prev && p[0] - prev[0] > gap;
        if (brk) { segs.push(band); band = []; }
        line += `${!prev || brk ? "M" : "L"}${xs(p[0]).toFixed(1)},${ys(p[1]).toFixed(1)}`;
        band.push(p);
        prev = p;
      }
      segs.push(band);
      if (s.bucket > s.interval * 1.5) {
        for (const b of segs) {
          if (b.length < 2) continue;
          const up = b.map((p) => `${xs(p[0]).toFixed(1)},${ys(p[3]).toFixed(1)}`);
          const dn = b.slice().reverse().map((p) => `${xs(p[0]).toFixed(1)},${ys(p[2]).toFixed(1)}`);
          g.append(svg("polygon", { points: up.concat(dn).join(" "), fill: s.color, "fill-opacity": 0.14, stroke: "none" }));
        }
      }
      g.append(svg("path", { d: line, fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
      // Einzelpunkte (ohne Nachbarn) sichtbar machen
      for (const b of segs) if (b.length === 1) g.append(svg("circle", { cx: xs(b[0][0]), cy: ys(b[0][1]), r: 3, fill: s.color }));
    }
    const hair = svg("line", { y1: M.t, y2: M.t + ih, class: "hair", visibility: "hidden" });
    const dots = ss.map((s) => svg("circle", { r: 4, fill: s.color, stroke: "#fff", "stroke-width": 2, visibility: "hidden" }));
    g.append(hair, ...dots);
    cross.push({ ss, hair, dots, ys });
    const legend = h("div", { class: "legend" }, ss.map((s) => h("span", {},
      h("span", { class: "lkey", style: `background:${s.color}` }), s.name, h("span", { class: "hint" }, ` ${s.nr}`))));
    panels.push(h("div", { class: "panel" }, legend, g));
  }
  const tip = h("div", { class: "tip", style: "display:none" });
  const wrap = h("div", { class: "chart-wrap" }, panels, tip);
  const nearest = (pts, t) => {
    let a = 0, b = pts.length - 1;
    if (b < 0) return null;
    while (b - a > 1) { const m = (a + b) >> 1; if (pts[m][0] < t) a = m; else b = m; }
    return Math.abs(pts[a][0] - t) <= Math.abs(pts[b][0] - t) ? pts[a] : pts[b];
  };
  const hide = () => { tip.style.display = "none"; for (const c of cross) { c.hair.setAttribute("visibility", "hidden"); c.dots.forEach((d) => d.setAttribute("visibility", "hidden")); } };
  wrap.addEventListener("pointerleave", hide);
  wrap.addEventListener("pointermove", (ev) => {
    const svgEl = ev.target.closest && ev.target.closest("svg.plot");
    if (!svgEl) return;
    const r = svgEl.getBoundingClientRect();
    const px = ev.clientX - r.left;
    if (px < M.l || px > width - M.r) { hide(); return; }
    const t = data.from + ((px - M.l) / iw) * (data.to - data.from);
    // Fadenkreuz rastet am nächsten Datenpunkt ein (über alle Reihen)
    let snap = null;
    for (const s of data.series) { const p = nearest(s.points, t); if (p && (!snap || Math.abs(p[0] - t) < Math.abs(snap - t))) snap = p[0]; }
    if (snap == null) { hide(); return; }
    const rows = [];
    for (const c of cross) {
      c.hair.setAttribute("x1", xs(snap)); c.hair.setAttribute("x2", xs(snap)); c.hair.setAttribute("visibility", "visible");
      c.ss.forEach((s, i) => {
        const p = nearest(s.points, snap);
        const ok = p && Math.abs(p[0] - snap) <= Math.max(s.bucket, s.interval) * 1.5;
        c.dots[i].setAttribute("visibility", ok ? "visible" : "hidden");
        if (ok) { c.dots[i].setAttribute("cx", xs(p[0])); c.dots[i].setAttribute("cy", c.ys(p[1])); }
        const raw = s.bucket <= s.interval * 1.5;               // Einzelwert, kein Mittelwert
        const tv = ok && raw ? choiceText(s.texts, p[1]) : null;
        const val = !ok ? "–" : tv != null ? `${tv} (${p[1]})` : fmtVal(p[1]) + (s.unit ? " " + s.unit : "");
        const extra = ok && s.bucket > s.interval * 1.5 ? ` (${fmtVal(p[2])} … ${fmtVal(p[3])})` : "";
        rows.push(h("div", { class: "trow" }, h("span", { class: "tkey", style: `background:${s.color}` }),
          h("b", {}, val), h("span", { class: "hint" }, extra), " ", h("span", {}, s.name)));
      });
    }
    tip.replaceChildren(h("div", { class: "hint" }, fmtDateTime(new Date(snap * 1000))), ...rows);
    tip.style.display = "block";
    const wr = wrap.getBoundingClientRect();
    const x = ev.clientX - wr.left, y = ev.clientY - wr.top;
    tip.style.left = (x + 16 + 260 > wr.width ? x - 16 - tip.offsetWidth : x + 16) + "px";
    tip.style.top = Math.max(0, y - 20) + "px";
  });
  const none = data.series.every((s) => !s.points.length);
  box.replaceChildren(none ? h("p", { class: "hint" }, "Für die gewählten Werte gibt es in diesem Zeitraum keine Daten.") : wrap);
}

function fmtVal(v) {
  if (v == null || !isFinite(v)) return "–";
  const a = Math.abs(v);
  const s = a >= 1e6 ? v.toFixed(0) : a >= 100 ? v.toFixed(1) : v.toFixed(2);
  return s.replace(".", ",");
}

function drawTable(box, data) {
  // Zeilen je Zeitabschnitt: die Werte eines Geräts werden nicht exakt zur selben Sekunde abgefragt
  const step = Math.max(...data.series.map((s) => Math.max(s.bucket, s.interval)));
  const idx = (t) => Math.floor((t - data.from) / step);
  const maps = data.series.map((s) => new Map(s.points.map((p) => [idx(p[0]), p[1]])));
  const rows = [...new Set(maps.flatMap((m) => [...m.keys()]))].sort((a, b) => b - a).slice(0, 1500);
  box.replaceChildren(h("h3", {}, "Tabelle (neueste zuerst)"), h("div", { class: "scroll", style: "max-height:360px" }, h("table", {},
    h("tr", {}, h("th", {}, "Zeit"), data.series.map((s) => h("th", {}, `${s.name}${s.unit ? " [" + s.unit + "]" : ""}`))),
    rows.map((i) => h("tr", {}, h("td", {}, fmtDateTime(new Date((data.from + i * step) * 1000))),
      maps.map((m, j) => {
        const s = data.series[j], v = m.get(i);
        const tv = m.has(i) && s.bucket <= s.interval * 1.5 ? choiceText(s.texts, v) : null;
        return h("td", { class: tv != null ? "" : "num" }, !m.has(i) ? "" : tv != null ? `${tv} (${v})` : fmtVal(v));
      }))))),
  data.series.some((s) => s.bucket > s.interval * 1.5) ? h("p", { class: "hint" }, "Zusammengefasst: Mittelwerte je Zeitabschnitt. Alle Einzelwerte enthält der CSV-Download des Tages.") : "");
}
