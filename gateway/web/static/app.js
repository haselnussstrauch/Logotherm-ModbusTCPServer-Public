"use strict";
// Logotherm Gateway - Weboberfläche (ohne Build, ohne externe Abhängigkeiten)

const $main = document.getElementById("main");
let CFG = null;            // Antwort von /api/config
let page = "iface";
let timer = null;          // Auto-Refresh nur auf der Statusseite
let selDevice = null;      // Gerät der Werteliste
let valRows = [];          // Zeilen der Werteliste (lokale, noch nicht übernommene Änderungen)
let valDirty = false;
let valFilter = "";
let scanResult = [];
let statusDev = null;

// --- Hilfsfunktionen ----------------------------------------------------------------------
function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "class") e.className = v;
    else if (k in e && k !== "list") e[k] = v;
    else e.setAttribute(k, v);
  }
  for (const c of kids.flat()) {
    if (c == null || c === false) continue;
    e.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return e;
}

async function api(method, url, body) {
  const opt = { method, headers: { "X-Requested-With": "gateway" } };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(url, opt);
  if (r.status === 401) { location.reload(); throw new Error("Anmeldung erforderlich"); }
  let d = null;
  try { d = await r.json(); } catch (e) { /* kein JSON */ }
  if (!r.ok) throw new Error((d && d.detail) || ("Fehler " + r.status));
  return d;
}

function toast(text, cls) {
  const t = h("div", { class: cls || "ok" }, text);
  document.getElementById("toast").append(t);
  setTimeout(() => t.remove(), cls === "err" ? 9000 : 4000);
}
const fail = (e) => toast(e.message || String(e), "err");

async function loadCfg() {
  CFG = await api("GET", "/api/config");
  const d = document.getElementById("dirty");
  d.style.display = (CFG.dirty || valDirty) ? "inline-block" : "none";
  d.textContent = valDirty ? "Ungeprüfte Änderungen in der Werteliste" : "Ungespeicherte Änderungen";
}

function num(v, dec) {
  if (v == null) return "";
  return Number.isInteger(v) && !dec ? String(v) : Number(v).toFixed(dec == null ? 2 : dec);
}
const hex = (n, d) => "0x" + Number(n).toString(16).toUpperCase().padStart(d || 4, "0");

// Auswahltext zum Wert (texts: {"0": "AUS", "1": "EIN"} aus der Konfiguration), sonst null
function choiceText(texts, v) {
  if (!texts || v == null || !Number.isInteger(Number(v))) return null;
  const t = texts[String(Number(v))];
  return t == null ? null : t;
}
// Wert für Tabellen: "Heizbetrieb (4)" bei Auswahl, sonst Zahl
function numOrText(v, dec, texts) {
  const t = choiceText(texts, v);
  return t != null ? `${t} (${Number(v)})` : num(v, dec);
}

// Fortschrittsauftrag abfragen, bis fertig
async function runJob(job, box) {
  const bar = h("div", { class: "bar" }, h("div", { style: "width:0%" }));
  const txt = h("div", { class: "hint" }, "Starte ...");
  box.replaceChildren(bar, txt);
  while (job.state === "running") {
    const p = job.progress;
    bar.firstChild.style.width = (p.total ? Math.round(100 * p.done / p.total) : 5) + "%";
    txt.textContent = p.text + (p.total ? ` (${p.done}/${p.total})` : "");
    await new Promise((r) => setTimeout(r, 500));
    job = await api("GET", "/api/jobs/" + job.id);
  }
  box.replaceChildren();
  if (job.state === "error") throw new Error(job.error || "Auftrag fehlgeschlagen");
  return job;
}

// --- Navigation ---------------------------------------------------------------------------
function go(p) {
  if (timer) { clearInterval(timer); timer = null; }
  page = p;
  if (location.hash.slice(1).split("?")[0] !== p) history.replaceState(null, "", "#" + p);
  for (const b of document.querySelectorAll("#nav button")) b.classList.toggle("active", b.dataset.page === p);
  loadCfg().then(() => PAGES[p]()).catch(fail);
}
document.getElementById("nav").addEventListener("click", (e) => {
  if (e.target.dataset.page) go(e.target.dataset.page);
});
document.getElementById("dirty").onclick = () => go(valDirty ? "values" : "save");

// --- Formularfelder für Einstellungen -----------------------------------------------------
function field(section, name, label, kind, opts) {
  const cur = CFG.draft[section][name];
  let input;
  if (kind === "bool") input = h("input", { type: "checkbox", checked: !!cur });
  else if (kind === "select")
    input = h("select", {}, opts.map((o) => h("option", { value: o, selected: String(o) === String(cur) }, o)));
  else if (kind === "list")
    input = h("input", { type: "text", size: 40, value: (cur || []).join(", "), placeholder: "z. B. 192.168.1.0/24, 10.0.0.5" });
  else input = h("input", { type: kind === "number" ? "number" : "text", value: cur == null ? "" : cur, step: opts && opts.step });
  input.dataset.name = name;
  input.dataset.kind = kind;
  return h("label", { class: kind === "list" ? "block" : "" }, label + " ", input);
}

function readSection(box) {
  const v = {};
  for (const i of box.querySelectorAll("[data-name]")) {
    const k = i.dataset.kind;
    if (k === "bool") v[i.dataset.name] = i.checked;
    else if (k === "number") v[i.dataset.name] = i.value === "" ? null : Number(i.value);
    else if (k === "list") v[i.dataset.name] = i.value.split(/[,;\s]+/).filter(Boolean);
    else if (k === "bitrate") v[i.dataset.name] = Number(i.value);
    else v[i.dataset.name] = i.value;
  }
  return v;
}

async function saveSection(section, box) {
  try {
    await api("PATCH", "/api/draft/settings/" + section, readSection(box));
    await loadCfg();
    toast("Im Entwurf gemerkt. Zum Aktivieren unter „Speichern“ übernehmen.");
  } catch (e) { fail(e); }
}

function sectionCard(title, section, fields, extra) {
  const box = h("div", {}, fields);
  return h("section", { class: "card" }, h("h2", {}, title), box, extra || "",
    h("button", { class: "btn", onclick: () => saveSection(section, box) }, "Übernehmen (Entwurf)"));
}

// --- Seite 1: Schnittstellen --------------------------------------------------------------
const PAGES = {};
PAGES.iface = async function () {
  const d = CFG.draft;
  const canBox = h("div", {},
    field("can", "enabled", "CAN-Bus verwenden", "bool"),
    field("can", "interface", "Adapter", "select", ["socketcan", "gs_usb", "pcan", "virtual"]),
    field("can", "channel", "Kanal", "text"),
    h("label", {}, "Bitrate ",
      h("select", { "data-name": "bitrate", "data-kind": "bitrate" },
        [50000, 20000].map((b) => h("option", { value: b, selected: d.can.bitrate === b }, (b / 1000) + " kbit/s")))),
    field("can", "gateway_address", "Gateway-Adresse (dezimal; 160 = 0xA0)", "number"),
    h("p", { class: "hint" }, "Lambda Control 2 neu: 50 kbit/s; ältere LC2-HW1 evtl. 20 kbit/s."));
  const testOut = h("div");
  const canCard = h("section", { class: "card" }, h("h2", {}, "CAN-Bus"), canBox, testOut,
    h("button", { class: "btn", onclick: () => saveSection("can", canBox) }, "Übernehmen (Entwurf)"),
    h("button", {
      class: "btn sec", onclick: async () => {
        const v = readSection(canBox);
        try {
          const job = await runJob(await api("POST", "/api/can/test",
            { interface: v.interface, channel: v.channel, bitrate: v.bitrate }), testOut);
          const r = job.result || {};
          testOut.replaceChildren(h("div", { class: r.ok ? "ok" : "err" }, r.text || (r.ok ? "In Ordnung" : "Fehlgeschlagen")),
            (r.details || []).length ? h("pre", { class: "log" }, r.details.map((d) =>
              typeof d === "string" ? d : `[${d.ok ? "OK " : "FEHLER"}] ${d.name}: ${d.text}`).join("\n")) : "");
        } catch (e) { testOut.replaceChildren(h("div", { class: "err" }, e.message)); }
      }
    }, "CAN-Adapter testen"));

  const portsBox = h("div", {}, "Lade ...");
  const lc1Card = h("section", { class: "card" }, h("h2", {}, "Serielle Anschlüsse (LC1 / RS232)"),
    h("p", { class: "hint" }, "Die Auswahl der Anschlüsse erfolgt beim Geräte-Suchen. FTDI-Adapter sind markiert. Unter Linux wird, wenn vorhanden, der stabile Pfad (by-id) verwendet."),
    portsBox, h("button", { class: "btn sec", onclick: () => loadPorts(portsBox) }, "Neu einlesen"));

  const mbFields = [
    field("modbus", "enabled", "Modbus-TCP-Server aktiv", "bool"),
    field("modbus", "bind", "Bind-Adresse", "text"),
    field("modbus", "port", "Port", "number"),
    field("modbus", "word_order", "Wortreihenfolge Float32", "select", ["ABCD", "CDAB"]),
    field("modbus", "int16_mirror", "Zusätzlich Int16-Spiegel bereitstellen", "bool"),
    field("modbus", "allow", "Erlaubte Clients (IP/Netz, Allowlist)", "list")];
  const mbCard = sectionCard("Modbus-TCP-Server", "modbus", mbFields, h("p", { class: "hint" }, "Aus = nur Weboberfläche, Dashboard und Aufzeichnung (kein Modbus-Port offen). Leere Allowlist = jeder Client im Netz darf zugreifen!"));
  // sectionCard liest aus der eigenen Box; Felder dort einsetzen
  const pollCard = sectionCard("Abfrage", "poll", [field("poll", "interval_s", "Poll-Intervall (s)", "number", { step: "any" }),
    field("poll", "stale_after_s", "Wert gilt als veraltet nach (s)", "number", { step: "any" })]);
  const safeCard = sectionCard("Schreibschutz", "safety", [
    h("div", { class: "warn" }, "ACHTUNG: Ist das Schreiben freigegeben, können Modbus-Clients Parameter an den Reglern ändern (nur Parameter, die einzeln als änderbar markiert sind). Das ist ein Eingriff in die Anlage! Nur aktivieren, wenn nötig. Schreiben nur per FC 16, genau 2 Register pro Anfrage."),
    field("safety", "write_enabled", "Schreiben global freigeben", "bool"),
    field("safety", "daily_write_budget", "Tagesbudget (Schreibvorgänge pro Tag)", "number")]);
  const webCard = sectionCard("Weboberfläche", "web", [
    field("web", "bind", "Bind-Adresse", "text"), field("web", "port", "Port", "number"),
    h("p", { class: "hint" }, "Bind-Adresse 127.0.0.1 = nur an diesem PC, 0.0.0.0 = auch von anderen PCs und Smartphones im Netz " +
      "(Windows-Firewall: Regel „LogothermGateway-Web“ aus dem Installer, nur lokales Subnetz). Aus dem Netz ist ein Passwort Pflicht. " +
      "Änderungen wirken nach Neustart des Dienstes.")]);
  const pw = h("input", { type: "password", autocomplete: "new-password" });
  const pwCard = h("section", { class: "card" }, h("h2", {}, "Passwort der Weboberfläche"),
    h("p", { class: "hint" }, CFG.password_set ? "Ein Passwort ist gesetzt." : "Kein Passwort gesetzt (nur lokaler Zugriff ohne Anmeldung)."),
    h("label", {}, "Neues Passwort (min. 6 Zeichen) ", pw),
    h("button", {
      class: "btn", onclick: async () => {
        try { await api("POST", "/api/password", { password: pw.value }); pw.value = ""; toast("Passwort gesetzt."); await loadCfg(); } catch (e) { fail(e); }
      }
    }, "Passwort setzen"));
  $main.replaceChildren(canCard, lc1Card, mbCard, pollCard, safeCard, webCard, pwCard);
  loadPorts(portsBox);
};

async function loadPorts(box) {
  try {
    const { ports } = await api("GET", "/api/serial-ports");
    if (!ports.length) { box.replaceChildren(h("p", { class: "hint" }, "Keine seriellen Anschlüsse gefunden.")); return; }
    box.replaceChildren(h("table", {}, h("tr", {}, ["Anschluss", "Beschreibung", "Stabiler Pfad", "FTDI"].map((t) => h("th", {}, t))),
      ports.map((p) => h("tr", {}, h("td", {}, p.device), h("td", {}, p.description || ""), h("td", {}, p.by_id || ""),
        h("td", {}, p.ftdi ? "FTDI" : "")))));
  } catch (e) { box.replaceChildren(h("div", { class: "err" }, e.message)); }
}

// --- Seite 2: Geräte suchen ---------------------------------------------------------------
PAGES.scan = async function () {
  const progress = h("div");
  const resultBox = h("div");
  const portsBox = h("div", {}, "Lade ...");
  const canChk = h("input", { type: "checkbox", checked: true });
  const full = h("input", { type: "checkbox" });
  const btn = h("button", {
    class: "btn", onclick: async () => {
      const lc1 = [...portsBox.querySelectorAll("input:checked")].map((i) => i.value);
      btn.disabled = true;
      try {
        const job = await runJob(await api("POST", "/api/scan", { can: canChk.checked, lc1_ports: lc1, full: full.checked }), progress);
        scanResult = job.result || [];
        renderScan(resultBox);
        toast(`${scanResult.length} Gerät(e) gefunden.`);
      } catch (e) { fail(e); } finally { btn.disabled = false; }
    }
  }, "Suche starten");
  $main.replaceChildren(
    h("section", { class: "card" }, h("h2", {}, "Geräte suchen"),
      h("label", {}, canChk, " CAN-Bus durchsuchen"),
      h("label", {}, full, " vollständig (alle Adressen, dauert länger)"),
      h("h3", {}, "LC1 an seriellen Anschlüssen (ankreuzen)"), portsBox,
      h("p", { class: "hint" }, "Während der Suche pausiert die normale Abfrage."), btn, progress, resultBox),
    h("section", { class: "card", id: "cfgdev" }));
  renderScan(resultBox);
  renderConfigured();
  try {
    const { ports } = await api("GET", "/api/serial-ports");
    // replaceChildren braucht einzelne Knoten (ein Array erschiene als Text "[object …]")
    portsBox.replaceChildren(...(ports.length ? ports.map((p) => h("label", { class: "block" },
      h("input", { type: "checkbox", value: p.by_id || p.device }), ` ${p.device} ${p.description || ""} `,
      p.ftdi ? h("b", {}, "[FTDI]") : "")) : [h("span", { class: "hint" }, "Keine seriellen Anschlüsse gefunden.")]));
  } catch (e) { portsBox.replaceChildren(h("div", { class: "err" }, e.message)); }
};

function isConfigured(info) {
  return CFG.draft.devices.some((d) => d.bus === info.bus && (info.bus === "can" ? d.address === info.address : d.port === info.port));
}

function renderScan(box) {
  if (!scanResult.length) { box.replaceChildren(); return; }
  box.replaceChildren(h("h3", {}, "Gefundene Geräte"), h("table", {},
    h("tr", {}, ["Typ", "Adresse / Port", "Name", "Version", "Seriennummer", ""].map((t) => h("th", {}, t))),
    scanResult.map((i) => h("tr", {}, h("td", {}, i.type_name || i.bus),
      h("td", {}, i.bus === "can" ? hex(i.address, 2) : i.port), h("td", {}, i.name || ""),
      h("td", {}, i.version || ""), h("td", {}, i.serial == null ? "" : i.serial),
      h("td", {}, isConfigured(i) ? "bereits übernommen" : h("button", {
        class: "btn", onclick: async () => {
          try {
            const d = await api("POST", "/api/draft/devices", i);
            toast(`Gerät ${d.id} übernommen (Unit-ID ${d.unit_id}). Als Nächstes: Werteliste laden.`);
            await loadCfg(); renderScan(box); renderConfigured();
          } catch (e) { fail(e); }
        }
      }, "übernehmen"))))));
}

function renderConfigured() {
  const box = document.getElementById("cfgdev");
  if (!box) return;
  const devs = CFG.draft.devices;
  box.replaceChildren(h("h2", {}, "Konfigurierte Geräte"),
    devs.length ? h("table", {}, h("tr", {}, ["ID", "Bus", "Adresse / Port", "Name", "Unit-ID (Modbus)", "Aktiv", "Werte", ""].map((t) => h("th", {}, t))),
      devs.map((d) => {
        const uid = h("input", { type: "number", value: d.unit_id, min: 1, max: 246 });
        const en = h("input", { type: "checkbox", checked: d.enabled });
        const upd = async () => {
          try { await api("PATCH", "/api/draft/devices/" + encodeURIComponent(d.id), { unit_id: Number(uid.value), enabled: en.checked }); await loadCfg(); toast("Im Entwurf gemerkt."); } catch (e) { fail(e); }
        };
        uid.onchange = upd; en.onchange = upd;
        return h("tr", {}, h("td", {}, d.id), h("td", {}, d.bus), h("td", {}, d.bus === "can" ? hex(d.address, 2) : d.port),
          h("td", {}, d.name || ""), h("td", {}, uid), h("td", {}, en),
          h("td", {}, `${(d.values || []).filter((v) => v.active !== false).length} Messwerte, ${(d.params || []).filter((v) => v.active !== false).length} Parameter`),
          h("td", {}, h("button", { class: "btn sec", onclick: () => { selDevice = d.id; go("values"); } }, "Werteliste"),
            h("button", {
              class: "btn danger", onclick: async () => {
                if (!confirm(`Gerät ${d.id} entfernen?`)) return;
                try { await api("DELETE", "/api/draft/devices/" + encodeURIComponent(d.id)); await loadCfg(); renderConfigured(); } catch (e) { fail(e); }
              }
            }, "Entfernen")));
      })) : h("p", { class: "hint" }, "Noch keine Geräte übernommen."),
    h("p", { class: "hint" }, "Die Unit-ID ist die Modbus-Adresse des Geräts für die Leittechnik (1 bis 246; 247 = Diagnose)."));
}

// --- Seite 3: Werteliste ------------------------------------------------------------------
PAGES.values = async function () {
  const devs = CFG.draft.devices;
  if (!devs.length) { $main.replaceChildren(h("section", { class: "card" }, "Zuerst unter „Geräte suchen“ ein Gerät übernehmen.")); return; }
  if (!devs.some((d) => d.id === selDevice)) selDevice = devs[0].id;
  const sel = h("select", { onchange: async () => {
    if (valDirty && !confirm("Nicht übernommene Änderungen verwerfen?")) { sel.value = selDevice; return; }
    valDirty = false; selDevice = sel.value; go("values");
  } }, devs.map((d) => h("option", { value: d.id, selected: d.id === selDevice }, `${d.id} (${d.name || d.bus})`)));
  const progress = h("div");
  const tableBox = h("div", {}, "Lade ...");
  const enumBtn = h("button", {
    class: "btn", onclick: async () => {
      if (!confirm("Die Werteliste wird direkt vom Gerät gelesen (ca. 200 Rahmen, dauert einen Moment; die Abfrage pausiert). Nur bei Bedarf ausführen. Fortfahren?")) return;
      enumBtn.disabled = true;
      try {
        await runJob(await api("POST", `/api/draft/devices/${encodeURIComponent(selDevice)}/enumerate`), progress);
        valDirty = false; await loadCfg(); await loadValues(tableBox);
        toast("Werteliste geladen. Neue Werte sind noch nicht ausgewählt.");
      } catch (e) { fail(e); } finally { enumBtn.disabled = false; }
    }
  }, "Werteliste vom Gerät laden");
  const filter = h("input", { type: "text", placeholder: "Suchen ...", value: valFilter, oninput: () => { valFilter = filter.value; renderValues(tableBox); } });
  $main.replaceChildren(h("section", { class: "card" }, h("h2", {}, "Werteliste"),
    h("label", {}, "Gerät ", sel), enumBtn, progress,
    h("p", { class: "hint" }, "Setzen Sie bei „bereitstellen“ ein Häkchen für alle Werte, die per Modbus sichtbar sein sollen. Abgewählte Werte behalten ihre Registeradresse."),
    h("p", { class: "hint" }, "Parameter: Schreiben nur per FC 16, genau 2 Register pro Anfrage."),
    h("label", {}, "Filter ", filter),
    h("button", { class: "btn sec", onclick: () => { for (const r of visibleRows()) if (r.kind === "value" && r.supported) r.active = true; markDirty(); renderValues(tableBox); } }, "alle Messwerte auswählen"),
    h("button", { class: "btn sec", onclick: () => { for (const r of visibleRows()) if (r.kind === "value") r.active = false; markDirty(); renderValues(tableBox); } }, "alle Messwerte abwählen"),
    h("button", { class: "btn", onclick: () => commitValues(tableBox) }, "Änderungen in Entwurf übernehmen"),
    h("button", { class: "btn sec", onclick: () => renumber("values") }, "Messwerte neu nummerieren"),
    h("button", { class: "btn sec", onclick: () => renumber("params") }, "Parameter neu nummerieren"),
    tableBox));
  await loadValues(tableBox);
};

function markDirty() { valDirty = true; document.getElementById("dirty").style.display = "inline-block"; document.getElementById("dirty").textContent = "Ungeprüfte Änderungen in der Werteliste"; }

async function loadValues(box) {
  try {
    const r = await api("GET", `/api/draft/devices/${encodeURIComponent(selDevice)}/values`);
    valRows = r.rows; valDirty = false;
    renderValues(box, r.from_device);
  } catch (e) { box.replaceChildren(h("div", { class: "err" }, e.message)); }
}

function visibleRows() {
  const f = valFilter.trim().toLowerCase();
  return valRows.filter((r) => !f || (r.name || "").toLowerCase().includes(f) || hex(r.key).toLowerCase().includes(f) || String(r.key) === f);
}

function renderValues(box, fromDevice) {
  const rows = visibleRows();
  const mk = (kind) => {
    const list = rows.filter((r) => r.kind === kind);
    return h("div", {}, h("h3", {}, kind === "value" ? `Messwerte (${list.length})` : `Parameter (${list.length})`),
      h("div", { class: "scroll" }, h("table", {},
        h("tr", {}, ["Nr.", "Name", "Einheit", "Min", "Max", "aktueller Wert", "Register", "bereitstellen", "Dashboard", kind === "param" ? "änderbar" : ""].map((t) => h("th", {}, t))),
        list.map((r) => {
          const unsup = !r.supported;
          const act = h("input", { type: "checkbox", checked: r.active, disabled: unsup || !r.in_config,
            title: unsup ? "Datentyp wird nicht bereitgestellt" : "", onchange: () => { r.active = act.checked; if (!r.active) { r.writable = false; r.dashboard = false; } markDirty(); renderValues(box); } });
          const dash = h("input", { type: "checkbox", checked: !!r.dashboard, disabled: unsup || !r.in_config,
            title: kind === "param" ? "Auf dem Dashboard anzeigen (Schnellzugriff; Setzen nur, wenn änderbar)" : "Auf dem Dashboard anzeigen",
            onchange: () => { r.dashboard = dash.checked; if (r.dashboard) r.active = true; markDirty(); renderValues(box); } });
          let wr = "";
          if (kind === "param") {
            const locked = !!r.lock_reason;
            wr = h("input", { type: "checkbox", checked: r.writable, disabled: locked || unsup || !r.in_config,
              title: locked ? r.lock_reason : (unsup ? "Datentyp wird nicht bereitgestellt" : "Mit FC 16 beschreibbar"),
              onchange: () => { r.writable = wr.checked; if (r.writable) r.active = true; markDirty(); renderValues(box); } });
          }
          return h("tr", { class: (r.active ? "" : "off ") + (unsup ? "unsupported " : "") + (r.lock_reason ? "locked" : ""), title: unsup ? "Datentyp wird nicht bereitgestellt" : (r.tip || "") },
            h("td", {}, hex(r.key)), h("td", {}, r.name || "", unsup ? h("i", {}, " (Datentyp wird nicht bereitgestellt)") : ""),
            h("td", {}, r.unit || ""), h("td", { class: "num" }, num(r.min, r.decimals)), h("td", { class: "num" }, num(r.max, r.decimals)),
            h("td", { class: r.texts ? "" : "num", title: r.texts ? Object.entries(r.texts).map(([k, t]) => `${k} = ${t}`).join("\n") : "" },
              numOrText(r.value, r.decimals, r.texts)), h("td", { class: "num" }, r.reg == null ? "" : r.reg),
            h("td", {}, act), h("td", {}, dash), h("td", { class: "rw", title: r.lock_reason || "" }, wr, r.lock_reason ? h("small", {}, " gesperrt") : ""));
        }))));
  };
  const info = fromDevice === false || (!valRows.length) ? h("div", { class: "warn" }, "Noch keine Werteliste vom Gerät geladen. Mit „Werteliste vom Gerät laden“ holen.") : "";
  box.replaceChildren(info, mk("value"), mk("param"));
}

async function commitValues(box) {
  const entries = valRows.filter((r) => r.in_config).map((r) => ({ kind: r.kind, key: r.key, active: !!r.active, dashboard: !!r.dashboard && !!r.active, writable: r.kind === "param" && !!r.writable }));
  try {
    await api("PATCH", "/api/draft/devices/" + encodeURIComponent(selDevice), { entries });
    valDirty = false; await loadCfg(); await loadValues(box);
    toast("Auswahl im Entwurf gemerkt. Unter „Speichern“ aktivieren.");
  } catch (e) { fail(e); }
}

async function renumber(table) {
  if (!confirm("ACHTUNG: Neu nummerieren entfernt abgewählte Einträge und VERSCHIEBT Registeradressen. Die Leittechnik muss danach angepasst werden. Wirklich fortfahren?")) return;
  try {
    await api("POST", `/api/draft/devices/${encodeURIComponent(selDevice)}/renumber`, { table, confirm: true });
    await loadCfg(); go("values");
  } catch (e) { fail(e); }
}

// --- Seite 4: Speichern -------------------------------------------------------------------
PAGES.save = async function () {
  const out = h("div");
  $main.replaceChildren(h("section", { class: "card" }, h("h2", {}, "Speichern und übernehmen"),
    valDirty ? h("div", { class: "warn" }, "In der Werteliste gibt es Änderungen, die noch nicht in den Entwurf übernommen wurden.") : "",
    CFG.dirty ? h("div", { class: "warn" }, h("b", {}, "Ungespeicherte Änderungen:"), h("ul", {}, CFG.changes.map((c) => h("li", {}, c))))
      : h("p", { class: "hint" }, "Keine ungespeicherten Änderungen."),
    h("button", {
      class: "btn", disabled: !CFG.dirty, onclick: async () => {
        try {
          const r = await api("POST", "/api/draft/apply");
          await loadCfg(); PAGES.save();
          toast("Gespeichert und übernommen." + (r.warnings.length ? " Hinweise: " + r.warnings.join(" | ") : ""), r.warnings.length ? "warn" : "ok");
        } catch (e) { out.replaceChildren(h("div", { class: "err" }, "Fehler: " + e.message)); }
      }
    }, "Speichern & übernehmen"),
    h("button", {
      class: "btn sec", disabled: !CFG.dirty, onclick: async () => {
        if (!confirm("Alle ungespeicherten Änderungen verwerfen?")) return;
        try { await api("POST", "/api/draft/discard"); valDirty = false; await loadCfg(); PAGES.save(); } catch (e) { fail(e); }
      }
    }, "Verwerfen"),
    out,
    h("h3", {}, "Registerliste für die Leittechnik"),
    h("p", { class: "hint" }, "Die Liste gehört zur zuletzt gespeicherten (aktiven) Konfiguration."),
    h("a", { class: "btn", href: "/api/registers.csv", download: "registerliste.csv", style: "display:inline-block;text-decoration:none" }, "CSV herunterladen")));
};

// --- Seite 5: Status ----------------------------------------------------------------------
PAGES.status = async function () {
  $main.replaceChildren(h("section", { class: "card", id: "st-main" }), h("section", { class: "card", id: "st-dev" }),
    h("section", { class: "card", id: "st-log" }), h("section", { class: "card", id: "st-wr" }));
  await refreshStatus();
  timer = setInterval(() => refreshStatus().catch(() => {}), 2000);
};

function fmtUp(s) { const d = Math.floor(s / 86400), hh = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60); return (d ? d + " d " : "") + hh + " h " + m + " min"; }

async function refreshStatus() {
  const [st, logs, wr] = await Promise.all([api("GET", "/api/status"), api("GET", "/api/logs?n=200"), api("GET", "/api/writes")]);
  if (page !== "status") return;
  const warns = st.warnings || [];
  const dg = st.diag || {};
  const polling = { running: "läuft", paused: "pausiert", stopped: "gestoppt" }[st.polling] || st.polling;
  document.getElementById("st-main").replaceChildren(h("h2", {}, "Dienst"),
    h("p", {}, h("span", { class: "dot " + (st.running ? "on" : "offl") }), st.running ? "Dienst läuft" : "Dienst gestoppt",
      ` - Version ${st.version}, Laufzeit ${fmtUp(st.uptime_s || 0)}`),
    h("p", {}, `Abfrage: ${polling} | Schreiben: ${st.write_enabled ? "FREIGEGEBEN" : "gesperrt"}`),
    ...warns.map((w) => h("div", { class: /allowlist/i.test(w) ? "err" : "warn" }, w)),
    h("h3", {}, "Diagnose"),
    h("p", { class: "hint" }, `Heartbeat ${dg.heartbeat ?? "-"} | Zykluszeit ${dg.cycle_time_ms ?? "-"} ms | CAN-Fehler ${dg.can_error_count ?? "-"} | Schreibvorgänge heute ${dg.writes_today ?? "-"} | Rest-Budget ${dg.budget_remaining ?? "-"}`));
  const devs = st.devices || [];
  if (!devs.some((d) => d.id === statusDev)) statusDev = devs.length ? devs[0].id : null;
  const dbox = document.getElementById("st-dev");
  const vals = statusDev ? (await api("GET", `/api/devices/${encodeURIComponent(statusDev)}/values`)).values : [];
  dbox.replaceChildren(h("h2", {}, "Geräte"),
    devs.length ? h("table", {}, h("tr", {}, ["", "ID", "Name", "Bus", "Adresse / Port", "Unit-ID", "letzter Fehler", "Alter", ""].map((t) => h("th", {}, t))),
      devs.map((d) => h("tr", {}, h("td", {}, h("span", { class: "dot " + (d.online ? "on" : "offl") }), d.online ? "online" : "offline"),
        h("td", {}, d.id), h("td", {}, d.name || ""), h("td", {}, d.bus), h("td", {}, d.bus === "can" ? hex(d.address, 2) : d.port),
        h("td", {}, d.unit_id), h("td", {}, d.last_error || "", d.write_alarm ? h("b", { class: "err" }, " Schreib-Alarm") : ""),
        h("td", {}, d.age_s == null ? "" : d.age_s + " s"),
        h("td", {}, h("button", { class: "btn sec", onclick: () => { statusDev = d.id; refreshStatus(); } }, "Werte"))))) : h("p", { class: "hint" }, "Keine Geräte konfiguriert."),
    statusDev ? h("h3", {}, "Letzte Werte: " + statusDev) : "",
    statusDev ? h("div", { class: "scroll" }, h("table", {}, h("tr", {}, ["Art", "Nr.", "Register", "Name", "Wert", "Einheit", "Status"].map((t) => h("th", {}, t))),
      vals.map((v) => h("tr", {}, h("td", {}, v.kind === "param" ? "Parameter" : "Messwert"), h("td", {}, hex(v.key)), h("td", {}, v.reg),
        h("td", {}, v.name), h("td", { class: v.text ? "" : "num" }, v.value == null ? "-" : (v.text ? `${v.text} (${v.value})` : num(v.value, 2))), h("td", {}, v.unit || ""),
        h("td", {}, ["OK", "Timeout", "veraltet"][v.status] || v.status))))) : "");
  const lbox = document.getElementById("st-log");
  const pre = h("pre", { class: "log" }, logs.lines.join("\n"));
  lbox.replaceChildren(h("h2", {}, "Protokoll"), pre);
  pre.scrollTop = pre.scrollHeight;
  document.getElementById("st-wr").replaceChildren(h("h2", {}, "Schreib-Historie"),
    wr.writes.length ? h("table", {}, h("tr", {}, ["Zeit", "Quelle", "Gerät", "Parameter", "alt", "neu", "Rücklesewert", "Ergebnis"].map((t) => h("th", {}, t))),
      wr.writes.map((w) => h("tr", {}, h("td", {}, w.ts), h("td", {}, w.source || ""), h("td", {}, w.device), h("td", {}, w.name || hex(w.key)),
        h("td", {}, w.old ?? ""), h("td", {}, w.new ?? ""), h("td", {}, w.readback ?? ""), h("td", {}, w.ok ? "OK" : "Fehler", w.text ? ": " + w.text : "")))) : h("p", { class: "hint" }, "Noch keine Schreibvorgänge."));
}

// --- Start --------------------------------------------------------------------------------
(async function () {
  try {
    const a = await api("GET", "/api/auth/state");
    if (a.password_set) {
      const b = document.getElementById("logout");
      b.style.display = "inline-block";
      b.onclick = async () => { await api("POST", "/api/logout"); location.reload(); };
    }
  } catch (e) { /* ignorieren */ }
  const start = location.hash.slice(1).split("?")[0];
  go(PAGES[start] ? start : "dashboard");
})();
