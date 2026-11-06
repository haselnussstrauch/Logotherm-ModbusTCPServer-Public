"use strict";
// Startseite „Dashboard“: Dienststatus, letzte Aktualisierung, Regler online, Messwerte und
// Parameter-Schnellzugriff. Welche Werte erscheinen, legt die Spalte „Dashboard“ der Werteliste fest.
// Nutzt h(), api(), toast(), fail(), go(), $main, PAGES, timer aus app.js und paramRow() aus params.js.

const fmtAge = (s) => s == null ? "–" : s < 60 ? `vor ${Math.round(s)} s` : s < 3600 ? `vor ${Math.round(s / 60)} min` : `vor ${(s / 3600).toFixed(1).replace(".", ",")} h`;
const fmtUptime = (s) => s < 3600 ? `${Math.floor(s / 60)} min` : s < 86400 ? `${Math.floor(s / 3600)} h ${Math.floor(s % 3600 / 60)} min` : `${Math.floor(s / 86400)} d ${Math.floor(s % 86400 / 3600)} h`;

function dashValue(v, dec) {
  if (v == null || !isFinite(v)) return "–";
  return Number(v).toFixed(dec || 0).replace(".", ",");
}

PAGES.dashboard = async function () {
  $main.replaceChildren(h("div", { id: "dash-tiles", class: "tiles" }), h("div", { id: "dash-warn" }), h("div", { id: "dash-groups" }));
  const d = await api("GET", "/api/dashboard");
  renderDashTiles(d);
  renderDashGroups(d);
  if (timer) clearInterval(timer);
  timer = setInterval(() => {
    if (!document.getElementById("dash-groups")) return;
    api("GET", "/api/dashboard").then((x) => { renderDashTiles(x); updateDashGroups(x); }).catch(() => {});
  }, 5000);
};

function tile(label, value, sub, cls) {
  return h("div", { class: "tile " + (cls || "") }, h("div", { class: "tile-label" }, label),
    h("div", { class: "tile-value" }, value), sub ? h("div", { class: "tile-sub" }, sub) : "");
}

function renderDashTiles(d) {
  const offline = d.devices.filter((x) => !x.online);
  const fresh = d.last_update_age_s != null && d.last_update_age_s < 120;
  document.getElementById("dash-tiles").replaceChildren(
    tile("Dienst", h("span", {}, h("span", { class: "dot " + (d.running ? "on" : "offl") }), d.running ? "läuft" : "gestoppt"),
      `Version ${d.version} · Laufzeit ${fmtUptime(d.uptime_s)}` + (d.polling === "paused" ? " · Abfrage pausiert" : "")),
    tile("Letzte Aktualisierung", fmtAge(d.last_update_age_s),
      d.last_update ? d.last_update.replace("T", " ") : "noch keine Werte", fresh ? "" : "warn"),
    tile("Regler online", `${d.online} von ${d.total}`,
      offline.length ? "offline: " + offline.map((x) => x.name).join(", ") : (d.total ? "alle erreichbar" : "keine Geräte konfiguriert"),
      d.total && offline.length ? "warn" : ""));
  const warns = d.warnings || [];
  document.getElementById("dash-warn").replaceChildren(...warns.map((w) => h("div", { class: /allowlist|passwort/i.test(w) ? "err" : "warn" }, w)));
}

function renderDashGroups(d) {
  const box = document.getElementById("dash-groups");
  if (!d.groups.length) {
    box.replaceChildren(h("section", { class: "card" }, h("h2", {}, "Messwerte und Parameter"),
      h("p", {}, "Noch nichts für das Dashboard ausgewählt."),
      h("p", { class: "hint" }, "Unter ", h("a", { href: "#values", onclick: (e) => { e.preventDefault(); go("values"); } }, "3 Werteliste"),
        " in der Spalte „Dashboard“ die gewünschten Messwerte und Parameter ankreuzen, „Änderungen in Entwurf übernehmen“ und unter „4 Speichern“ aktivieren.")));
    return;
  }
  box.replaceChildren(...d.groups.map((g) => {
    const devName = g.name;
    const values = g.values.length ? h("div", { class: "vgrid" }, g.values.map((v) => valueTile(g, v))) : "";
    const params = g.params.length ? h("div", {}, h("h3", {}, "Parameter"),
      d.write_enabled ? "" : h("p", { class: "hint" }, "Schreiben ist global gesperrt – Parameter werden nur angezeigt."),
      h("table", { class: "ptable" }, g.params.map((p) => paramRow(p, g.id, devName, () => PAGES.dashboard(), true)))) : "";
    return h("section", { class: "card", "data-dev": g.id },
      h("h2", {}, h("span", { class: "dot " + (g.online ? "on" : "offl") }), devName, h("span", { class: "hint" }, ` ${g.id}`)),
      values, params);
  }));
}

function valueTile(g, v) {
  const bad = v.status !== 0;
  const link = `#history?dev=${encodeURIComponent(g.id)}&range=24h&sel=value:${v.key}`;
  return h("a", { class: "vtile" + (bad ? " stale" : ""), href: link, "data-key": v.key, title: "Verlauf der letzten 24 Stunden",
    onclick: (e) => { e.preventDefault(); location.hash = link; histFromLink(); go("history"); } },
    h("div", { class: "vname" }, v.name),
    h("div", { class: "vval" }, dashValue(v.value, v.decimals), v.unit ? h("span", { class: "vunit" }, " " + v.unit) : ""),
    h("div", { class: "vsub" }, bad ? (v.status === 1 ? "kein Wert" : "veraltet") : v.nr));
}

// Nur Werte auffrischen; Parameter-Eingaben bleiben stehen
function updateDashGroups(d) {
  const shown = [...document.querySelectorAll("#dash-groups section[data-dev]")].map((s) => s.dataset.dev);
  if (shown.join() !== d.groups.map((g) => g.id).join()) { renderDashGroups(d); return; }
  for (const g of d.groups) {
    const sec = document.querySelector(`#dash-groups section[data-dev="${CSS.escape(g.id)}"]`);
    sec.querySelector("h2 .dot").className = "dot " + (g.online ? "on" : "offl");
    for (const v of g.values) {
      const t = sec.querySelector(`.vtile[data-key="${v.key}"]`);
      if (t) t.replaceWith(valueTile(g, v));
    }
    for (const p of g.params) {
      const tr = sec.querySelector(`tr[data-key="${p.key}"]`);
      if (!tr) continue;
      const input = tr.querySelector("input");
      const canNow = !tr.classList.contains("off");
      if (canNow !== p.can_write && document.activeElement !== input) {
        tr.replaceWith(paramRow(p, g.id, g.name, () => PAGES.dashboard(), true));
        continue;
      }
      tr.querySelector("td.cur").textContent = dashValue(p.value, p.decimals) + (p.unit ? " " + p.unit : "");
      tr.lastElementChild.textContent = p.why;
    }
  }
}
