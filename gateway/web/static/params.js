"use strict";
// Seite „7 Parameter“: Parameter am Regler setzen. Gleicher Weg und gleiche Prüfungen wie Modbus FC 16
// (globale Freigabe, Freigabe je Parameter, Sperrliste, Min/Max, 10 min je Parameter, Tagesbudget,
// Rücklesen). Nutzt h(), api(), toast(), fail(), go(), CFG, PAGES, timer aus app.js.

let paramDev = null;

PAGES.params = async function () {
  const devs = (CFG.active.devices || []).filter((d) => (d.params || []).some((p) => p.active));
  if (!devs.length) {
    $main.replaceChildren(h("section", { class: "card" }, h("h2", {}, "Parameter setzen"),
      h("p", { class: "hint" }, "Kein Gerät hat aktive Parameter. Unter „3 Werteliste“ Parameter ankreuzen und unter „4 Speichern“ übernehmen.")));
    return;
  }
  if (!devs.some((d) => d.id === paramDev)) paramDev = devs[0].id;
  const sel = h("select", {}, devs.map((d) => h("option", { value: d.id, selected: d.id === paramDev }, `${d.name || d.id} (${d.id})`)));
  sel.onchange = () => { paramDev = sel.value; renderParams().catch(fail); };
  $main.replaceChildren(
    h("section", { class: "card" }, h("h2", {}, "Parameter setzen"),
      h("div", { class: "filters" }, h("label", {}, "Gerät ", sel),
        h("button", { class: "btn sec", onclick: () => renderParams().catch(fail) }, "Aktualisieren")),
      h("div", { id: "par-info" }), h("div", { id: "par-table" })),
    h("section", { class: "card", id: "par-hist" }));
  await renderParams();
  if (timer) clearInterval(timer);
  timer = setInterval(() => { if (document.getElementById("par-table")) refreshParamValues().catch(() => {}); }, 10000);
};

let paramRows = [];

async function renderParams() {
  const ov = await api("GET", `/api/devices/${encodeURIComponent(paramDev)}/params`);
  paramRows = ov.params;
  const info = document.getElementById("par-info");
  info.replaceChildren(
    ov.write_enabled
      ? h("div", { class: "warn" }, h("b", {}, "Achtung: "), "Werte werden direkt an den Regler gesendet und verändern den Betrieb der Anlage. ",
        "Jeder Parameter ist höchstens alle 10 Minuten änderbar. ", `Heute noch ${ov.budget_remaining} von ${ov.budget} Schreibvorgängen.`)
      : h("div", { class: "warn" }, "Schreiben ist global gesperrt. Zum Freigeben: ",
        h("a", { href: "#iface", onclick: (e) => { e.preventDefault(); go("iface"); } }, "1 Schnittstellen → Schreibschutz"),
        ", danach unter „4 Speichern“ übernehmen. Zusätzlich muss jeder Parameter unter ",
        h("a", { href: "#values", onclick: (e) => { e.preventDefault(); go("values"); } }, "3 Werteliste"), " als änderbar markiert sein."));
  const dev = CFG.active.devices.find((d) => d.id === paramDev);
  const tbody = paramRows.map((p) => paramRow(p, paramDev, dev ? dev.name || dev.id : paramDev, () => renderParams()));
  document.getElementById("par-table").replaceChildren(h("div", { class: "scroll" }, h("table", {},
    h("tr", {}, ["Nr", "Parameter", "Aktuell", "Bereich", "Neuer Wert", "", "Hinweis"].map((t) => h("th", {}, t))), tbody)));
  await renderParamHistory();
}

function fmtParam(v, dec) {
  if (v == null || !isFinite(v)) return "–";
  return Number(v).toFixed(dec || 0).replace(".", ",");
}

// Eine Tabellenzeile mit Eingabe und „Setzen“ (auch vom Dashboard genutzt).
// onDone: nach dem Schreibversuch (Tabelle neu laden). compact: Darstellung fürs Dashboard.
// Anzeige eines Parameterwerts: Auswahltext mit Index bzw. Zahl mit Einheit
function paramValueText(p, v) {
  const val = v === undefined ? p.value : v;
  const t = choiceText(p.texts, val);
  if (t != null) return `${t} (${Number(val)})`;
  return fmtParam(val, p.decimals) + (p.unit ? " " + p.unit : "");
}

function paramRow(p, devId, devName, onDone, compact) {
  const step = p.decimals ? (1 / 10 ** p.decimals).toFixed(p.decimals) : "1";
  // Auswahl (Textauswahl, JA/NEIN, EIN/AUS): Liste der Texte im erlaubten Bereich statt Zahlenfeld
  const choices = p.texts ? Object.entries(p.texts).map(([k, t]) => [Number(k), t])
    .filter(([k]) => (p.min == null || k >= p.min) && (p.max == null || k <= p.max)).sort((a, b) => a[0] - b[0]) : [];
  const input = choices.length
    ? h("select", { disabled: !p.can_write, style: "min-width:150px" },
      choices.map(([k, t]) => h("option", { value: k, selected: Number(p.value) === k }, `${t} (${k})`)))
    : h("input", { type: "number", step, min: p.min ?? undefined, max: p.max ?? undefined,
      value: p.value == null ? "" : Number(p.value).toFixed(p.decimals || 0), disabled: !p.can_write, style: "width:110px" });
  const btn = h("button", { class: "btn", disabled: !p.can_write }, "Setzen");
  const unit = p.unit ? " " + p.unit : "";
  btn.onclick = async () => {
    const v = choices.length ? Number(input.value) : input.valueAsNumber;
    if (!isFinite(v)) { toast("Bitte eine Zahl eingeben.", "err"); return; }
    if ((p.min != null && v < p.min) || (p.max != null && v > p.max)) {
      toast(`Wert außerhalb des Bereichs ${fmtParam(p.min, p.decimals)} … ${fmtParam(p.max, p.decimals)}${unit}.`, "err"); return;
    }
    if (!confirm(`Parameter „${p.name}“ (${p.nr}) am Regler ${devName} setzen?\n\n` +
      `bisher: ${paramValueText(p)}\nneu:    ${paramValueText(p, v)}\n\n` +
      "Der Wert wird sofort gesendet. Danach ist dieser Parameter 10 Minuten lang gesperrt.")) return;
    btn.disabled = true; btn.textContent = "Sende …";
    try {
      const r = await api("POST", `/api/devices/${encodeURIComponent(devId)}/params/${p.key}`, { value: v });
      if (r.ok) toast(r.text ? `${p.name}: ${r.text}.` : `${p.name} gesetzt. Rückmeldung des Reglers: ${paramValueText(p, r.readback)}.`, "ok");
      else toast(`${p.name}: ${r.text || "Fehler"} (Rückmeldung ${paramValueText(p, r.readback)}). Bitte am Regler prüfen.`, "err");
    } catch (e) { fail(e); }
    await Promise.resolve(onDone && onDone()).catch(fail);
  };
  const range = choices.length ? `${choices.length} Auswahlmöglichkeiten`
    : p.min == null && p.max == null ? "–" : `${fmtParam(p.min, p.decimals)} … ${fmtParam(p.max, p.decimals)}`;
  const tr = compact
    ? h("tr", { class: p.can_write ? "" : "off", "data-key": p.key },
      h("td", {}, p.name, h("div", { class: "hint" }, `${p.nr} · ${range}${choices.length ? "" : unit}`)),
      h("td", { class: "num cur" }, paramValueText(p)),
      h("td", { class: "nowrap" }, input, " ", btn), h("td", { class: "hint" }, p.why))
    : h("tr", { class: p.can_write ? "" : "off", "data-key": p.key },
      h("td", {}, p.nr), h("td", {}, p.name),
      h("td", { class: "num cur" }, paramValueText(p), p.status ? h("span", { class: "hint" }, p.status === 1 ? " (kein Wert)" : " (veraltet)") : ""),
      h("td", { class: "num" }, range),
      h("td", {}, input), h("td", {}, btn),
      h("td", { class: "hint" }, p.why));
  input.onkeydown = (e) => { if (e.key === "Enter" && !btn.disabled) btn.click(); };
  return tr;
}

// Nur die Spalte „Aktuell“ auffrischen – Eingaben bleiben stehen
async function refreshParamValues() {
  const ov = await api("GET", `/api/devices/${encodeURIComponent(paramDev)}/params`);
  for (const p of ov.params) {
    const cell = document.querySelector(`#par-table tr[data-key="${p.key}"] td.cur`);
    const old = paramRows.find((x) => x.key === p.key);
    if (!cell || !old) continue;
    // Nur bei geänderter Schreibbarkeit neu aufbauen (sonst gingen getippte Eingaben verloren)
    if (old.can_write !== p.can_write) { await renderParams(); return; }
    cell.replaceChildren(paramValueText(p),
      p.status ? h("span", { class: "hint" }, p.status === 1 ? " (kein Wert)" : " (veraltet)") : "");
    const hint = cell.parentElement.lastElementChild;
    if (hint) hint.textContent = p.why;
  }
  paramRows = ov.params;
}

async function renderParamHistory() {
  const box = document.getElementById("par-hist");
  if (!box) return;
  const wr = (await api("GET", "/api/writes")).writes.filter((w) => w.device === paramDev).slice(0, 20);
  box.replaceChildren(h("h2", {}, "Letzte Schreibvorgänge an diesem Gerät"),
    wr.length ? h("table", {}, h("tr", {}, ["Zeit", "Quelle", "Parameter", "alt", "neu", "Rückmeldung", "Ergebnis"].map((t) => h("th", {}, t))),
      wr.map((w) => h("tr", {}, h("td", {}, w.ts.replace("T", " ")), h("td", {}, w.source || ""), h("td", {}, w.name || hex(w.key)),
        h("td", { class: "num" }, w.old ?? ""), h("td", { class: "num" }, w.new ?? ""), h("td", { class: "num" }, w.readback ?? ""),
        h("td", {}, w.ok ? "OK" : h("b", { class: "err-text" }, "Fehler"), w.text ? ": " + w.text : ""))))
      : h("p", { class: "hint" }, "Seit dem Start des Dienstes keine Schreibvorgänge."));
}
