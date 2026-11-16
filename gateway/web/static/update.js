"use strict";
// Update-Hinweis unter der Kopfzeile (alle Seiten): erscheint, sobald eine neue Version verfügbar ist.
// Windows-Dienst: Knopf „Jetzt installieren“ (Setup still im Hintergrund, Seite lädt danach neu).
// Docker/Raspberry Pi: Befehl zum Aktualisieren am Host.

let updTimer = null;
let updWaitFor = null;   // Version, auf die nach dem Installieren gewartet wird

async function updRefresh() {
  let u;
  try { u = await api("GET", "/api/update"); } catch (e) { return; }
  renderUpdate(u);
  if (u.state === "downloading" || u.state === "installing") updSchedule(2000);
  else updSchedule(5 * 60 * 1000);
}

function updSchedule(ms) {
  if (updTimer) clearTimeout(updTimer);
  updTimer = setTimeout(updRefresh, ms);
}

function renderUpdate(u) {
  const box = document.getElementById("update-banner");
  if (!u || (!u.available && u.state !== "error")) { box.replaceChildren(); box.style.display = "none"; return; }
  box.style.display = "block";
  const kids = [h("strong", {}, `Neue Version ${u.latest} verfügbar`), ` (installiert: ${u.current}). `];
  if (u.page_url) kids.push(h("a", { href: u.page_url, target: "_blank", rel: "noopener" }, "Was ist neu?"), " ");
  if (u.state === "downloading" || u.state === "installing") {
    kids.push(h("span", { class: "upd-progress" }, u.progress || "…"));
  } else if (u.can_install) {
    kids.push(h("button", { class: "btn", onclick: () => updInstall(u) }, "Jetzt installieren"));
  } else if (u.docker_command) {
    kids.push(h("div", { class: "hint" }, "Am Raspberry Pi bzw. Docker-Host (per SSH) ausführen:"),
      h("code", { class: "upd-cmd" }, u.docker_command), " ",
      h("button", { class: "btn sec", onclick: () => updCopy(u.docker_command) }, "Kopieren"));
  } else {
    kids.push(h("span", { class: "hint" }, "Bitte das neue Setup von GitHub installieren."));
  }
  if (u.state === "error" && u.error) kids.push(h("div", { class: "err-text" }, u.error));
  box.replaceChildren(...kids);
}

// navigator.clipboard gibt es nur über HTTPS/localhost – am Pi (http://logogateway.local) fehlt es.
async function updCopy(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); toast("Befehl kopiert"); return; }
  } catch (e) { /* Rückfall unten */ }
  const ta = document.createElement("textarea");
  ta.value = text; ta.setAttribute("readonly", ""); ta.style.position = "fixed"; ta.style.opacity = "0";
  document.body.appendChild(ta); ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
  ta.remove();
  if (ok) toast("Befehl kopiert"); else prompt("Befehl kopieren (Strg+C):", text);
}

async function updInstall(u) {
  if (!confirm(`Version ${u.latest} jetzt herunterladen und installieren?\n\n` +
      "Der Dienst wird dafür angehalten: Modbus und Weboberfläche sind etwa 1–2 Minuten nicht erreichbar. " +
      "Konfiguration, Passwort und Aufzeichnung bleiben erhalten.")) return;
  updWaitFor = u.latest;
  renderUpdate({ ...u, state: "downloading", progress: "Lade herunter …" });
  try {
    const st = await api("POST", "/api/update/install");
    renderUpdate(st);
  } catch (e) { updWaitFor = null; fail(e); updRefresh(); return; }
  updAwaitRestart();
}

// Während der Installation ist der Dienst weg; sobald er mit der neuen Version antwortet: neu laden.
function updAwaitRestart() {
  const started = Date.now();
  const tick = async () => {
    try {
      const r = await fetch("/api/status", { headers: { "X-Requested-With": "gateway" } });
      const s = await r.json();
      if (s.version && s.version === updWaitFor) { toast(`Update auf ${s.version} installiert`); setTimeout(() => location.reload(), 1500); return; }
    } catch (e) { /* Dienst gerade neu gestartet */ }
    if (Date.now() - started > 10 * 60 * 1000) { fail(new Error("Update dauert ungewöhnlich lange – Protokoll prüfen (logs/update-setup-*.log)")); return; }
    setTimeout(tick, 3000);
  };
  setTimeout(tick, 5000);
}

async function updCheckNow(btn) {
  btn.disabled = true;
  try {
    const u = await api("POST", "/api/update/check");
    renderUpdate(u);
    toast(u.error ? u.error : (u.available ? `Neue Version ${u.latest} verfügbar` : `Aktuell – Version ${u.current}`), u.error ? "err" : "ok");
  } catch (e) { fail(e); }
  btn.disabled = false;
}

updRefresh();
