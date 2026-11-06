"use strict";
const H = { "X-Requested-With": "gateway", "Content-Type": "application/json" };
const $ = (id) => document.getElementById(id);
let setup = false;

function show(text) {
  const m = $("msg");
  m.textContent = text;
  m.className = "err";
}

async function post(url, body) {
  const r = await fetch(url, { method: "POST", headers: H, body: JSON.stringify(body) });
  let d = {};
  try { d = await r.json(); } catch (e) { /* leer */ }
  if (!r.ok) throw new Error(d.detail || ("Fehler " + r.status));
  return d;
}

async function go() {
  try {
    if (setup) {
      if ($("pw").value !== $("pw2").value) { show("Die Passwörter stimmen nicht überein."); return; }
      await post("/api/setup-password", { password: $("pw").value });
    } else {
      await post("/api/login", { password: $("pw").value });
    }
    location.href = "/";
  } catch (e) { show(e.message); }
}

async function init() {
  const st = await (await fetch("/api/auth/state")).json();
  setup = st.setup_needed;
  if (st.setup_local_only) {
    $("title").textContent = "Noch kein Passwort";
    $("info").textContent = "Die Weboberfläche ist im Netzwerk freigegeben, aber es ist noch kein Passwort gesetzt. " +
      "Bitte das Passwort zuerst am Gateway-PC festlegen: dort im Launcher „Einrichtung öffnen“ → " +
      "„1 Schnittstellen“ → „Passwort der Weboberfläche“. Danach ist die Anmeldung hier möglich.";
    for (const id of ["pw", "go"]) $(id).closest("label, button").style.display = "none";
    return;
  }
  if (setup) {
    $("title").textContent = "Passwort festlegen";
    $("info").textContent = "Das Gateway ist über das Netzwerk erreichbar. Bitte zuerst ein Passwort festlegen (mindestens 6 Zeichen).";
    $("pw2row").style.display = "block";
    $("go").textContent = "Passwort speichern";
  }
  $("go").onclick = go;
  document.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
}
init();
