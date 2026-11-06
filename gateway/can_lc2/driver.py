"""CAN-Treiber (LC2-Protokoll) – implementiert ``gateway.drivers.BusDriver``.

Ablauf und Sicherheit:
- ``open()`` öffnet den Adapter und hört ``ADDRESS_CHECK_S`` Sekunden passiv mit. Sendet jemand mit
  der Gateway-Adresse, wird abgebrochen (``InterfaceError``). Identify an die eigene Adresse ist
  durch ``safety`` verboten, daher nur passiv.
- Jeder Sendewunsch läuft über ``CanClient._send_frame`` -> ``safety.check_can_tx``.
- ``write_param``: liest den Parameter vorher frisch per 0x38 (VarTyp, Ausgabetyp, Min/Max des
  Geräts), prüft Sperren inkl. Ausgabetyp, rechnet den Rohwert, prüft VarTyp-Bereich und Geräte-
  Min/Max (sonst setzt die Firmware stillschweigend die Werkseinstellung, ``CheckParameter``),
  sendet **einmal** 0x08/0x00 und wartet auf die Bestätigung 0x0A/0x38. Keine Bestätigung ->
  ``DeviceTimeout`` (kein zweites Senden).
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass

from gateway import safety
from gateway.can_lc2 import idcodec, messages, vartyp
from gateway.can_lc2.client import CanClient
from gateway.config import DeviceConfig, GatewayConfig, ParamDef, ValueDef
from gateway.devices import fw_numbers, names
from gateway.drivers import (
    DeviceInfo, DeviceTimeout, InterfaceError, ProgressCb, ProtocolError, TraceCb, ValueInfo,
)
from gateway.errors import SafetyViolation, WriteRejected

log = logging.getLogger(__name__)

#: Passives Mithören vor der ersten Nutzung (Gateway-Adresse frei?)
ADDRESS_CHECK_S = 3.0
#: Wartezeit auf die Schreibbestätigung: Die Firmware speichert vor der Antwort
#: (``ParameterGeaendert`` -> ``save_einstellungen``, BusOut.cpp:927-934) – EEPROM braucht Zeit.
#: TODO(Feld): an echter Anlage messen.
WRITE_CONFIRM_TIMEOUT_S = 3.0
#: read_values: nach so vielen Timeouts in Folge ohne eine einzige Antwort -> Gerät offline
OFFLINE_AFTER = 3
#: Ausgabetyp-Nachfrage (0x30) für Messwerte ohne bekannten Ausgabetyp nach Fehlschlag erneut nach
META_RETRY_S = 600.0

#: Schnellscan-Bereiche (Firmware: _BUS_OFFSET in App/*/Config.h, MC2 Hex-Schalter + 16,
#: Databox 0xFC in App/Databox/Hardware.cpp:128)
FAST_SCAN_RANGES: tuple[range, ...] = (
    range(0x01, 0x10),     # LC2 / LC1 BSB (P_BUSADR 1..15)
    range(0x10, 0x20),     # MultiControl 2 (BUS_BASIS 16 + Hex-Schalter)
    range(0x20, 0x30),     # WRS-D (_BUS_OFFSET 0x20)
    range(0x50, 0x60),     # Erweiterungen / Zündung (_BUS_OFFSET 0x50)
    range(0xE0, 0xF0),     # O2-Sonde (_BUS_OFFSET 0xE0)
    range(0xFC, 0xFD),     # Databox
)

_FRIENDLY: list[tuple[range, str]] = [
    (range(0x0900, 0x0907), "LambdaControl 2 (alt, AT90CAN128)"),
    (range(0x0A01, 0x0A02), "MultiControl 2"),
    (range(0x0B00, 0x0B05), "LambdaControl 2 Neu"),
    (range(0x0D00, 0x0D05), "LambdaControl 1 BSB"),
    (range(0x0F00, 0x0F01), "WRS-D"),
    (range(0x1A00, 0x1A01), "Erweiterung 1"),
    (range(0x2A00, 0x2A01), "Erweiterung 2"),
    (range(0x3A00, 0x3A01), "Zündung"),
    (range(0x3A01, 0x3A02), "Zündung+WT"),
    (range(0xFFF0, 0xFFF1), "Databox"),
    (range(0xFFF2, 0xFFF3), "O2-Sonde (NGK)"),
    (range(0xFFFE, 0xFFFF), "Programmiergerät"),
    (range(0xFFFF, 0x10000), "LambdaControl 2 Parametrierung"),
]


def type_name(regler_typ: int | None, address: int | None = None) -> str:
    """Lesbarer Gerätetyp aus dem Reglertyp (``fw_numbers.REGLER_TYPEN``)."""
    if regler_typ is None:
        return "unbekannt"
    fw = fw_numbers.REGLER_TYPEN.get(regler_typ)
    friendly = next((n for r, n in _FRIENDLY if regler_typ in r), None)
    if regler_typ == 0x0A01 and address is not None and 0x50 <= address <= 0x5F:
        # 0x0A01 ist auch der Reglertyp der Parametrier-Builds von Erweiterung 2 / Zündung+WT
        friendly = "Erweiterung/Zündung (Reglertyp 0x0A01, nicht eindeutig)"
    if friendly and fw:
        return f"{friendly} ({fw})"
    return friendly or fw or f"Reglertyp {regler_typ:#06x} (unbekannt)"


@dataclass
class _Meta:
    vartyp: int
    decimals: int
    out_type: int | None


class CanDriver:
    bus = "can"

    def __init__(self, cfg: GatewayConfig, trace: TraceCb | None = None, *, bus_factory=None,
                 address_check_s: float = ADDRESS_CHECK_S) -> None:
        self.cfg = cfg
        self.trace = trace
        self._bus_factory = bus_factory
        self.address_check_s = address_check_s
        self.client: CanClient | None = None
        self._meta: dict[tuple[int, int, str], _Meta] = {}
        self._meta_fail: dict[tuple[int, int], float] = {}
        self._reopen = False
        self._closed_stats: dict = {}

    # ===== Lebenszyklus =========================================================================
    @property
    def is_open(self) -> bool:
        return self.client is not None and self.client.is_open

    def reload(self, cfg: GatewayConfig) -> None:
        """Konfiguration austauschen. Geänderte CAN-Einstellungen -> beim nächsten Zugriff neu öffnen."""
        changed = cfg.can.model_dump() != self.cfg.can.model_dump()
        self.cfg = cfg
        if changed and self.client is not None:
            self._reopen = True

    async def open(self) -> None:
        if self.is_open and not self._reopen:
            return
        if self.client is not None:
            await self.close()
        self._reopen = False
        client = CanClient(self.cfg.can, trace=self.trace, bus_factory=self._bus_factory)
        await client.open()
        self.client = client
        if self.address_check_s > 0:
            res = await client.listen(self.address_check_s)
            if res["own_address_seen"]:
                await self.close()
                raise InterfaceError(
                    f"Gateway-Adresse {self.cfg.can.gateway_address:#04x} ist belegt: ein anderer "
                    f"Teilnehmer sendet mit dieser Quelladresse. Bitte can.gateway_address ändern.")
            log.info("CAN: %d Rahmen in %.1f s gehört, Quellen: %s", res["frames"], self.address_check_s,
                     ", ".join(f"{a:#04x}" for a in res["sources"]) or "keine")

    async def close(self) -> None:
        client, self.client = self.client, None
        if client is not None:
            self._closed_stats = client.stats()
            await client.close()

    async def _client(self) -> CanClient:
        if self.client is None or not self.client.is_open or self._reopen:
            await self.open()
        assert self.client is not None
        return self.client

    def stats(self) -> dict:
        s = self.client.stats() if self.client is not None else dict(self._closed_stats)
        for k in ("errors", "tx", "rx", "timeouts"):
            s.setdefault(k, 0)
        s["open"] = self.is_open
        return s

    # ===== Hilfen ===============================================================================
    @staticmethod
    def _check_device(device: DeviceConfig) -> int:
        if device.bus != "can" or device.address is None:
            raise ValueError(f"{device.id}: kein CAN-Gerät")
        return device.address

    async def _detail(self, client: CanClient, addr: int, sub: int, nr: int, regler_typ: int | None,
                      *, retries: int | None = None, timeout_s: float | None = None) -> messages.DetailAntwort:
        block = await client.request(addr, idcodec.KANAL_ANFRAGE, sub, messages.build_anfrage(nr),
                                     mode="block", nr=nr, regler_typ=regler_typ, retries=retries,
                                     timeout_s=timeout_s)
        assert isinstance(block, bytes)
        try:
            det = messages.DetailAntwort.parse(block)
        except ValueError as exc:
            raise ProtocolError(f"{addr:#04x}: Detail {nr:#06x} unlesbar: {exc}") from exc
        self._remember(addr, det, "param" if sub == 0x38 else "value")
        return det

    def _remember(self, addr: int, det: messages.DetailAntwort, kind: str) -> None:
        self._meta[(addr, det.nr, kind)] = _Meta(det.vartyp, det.nachkomma, det.ausgabe)

    @staticmethod
    def _det_value(det: messages.DetailAntwort) -> float | None:
        if not vartyp.is_numeric(det.vartyp):
            return None
        try:
            raw = vartyp.decode_raw(det.vartyp, det.wert)
        except ValueError:
            return None
        v = vartyp.to_value(raw, det.vartyp, det.nachkomma, det.ausgabe)
        return v if math.isfinite(v) else None

    # ===== Scan =================================================================================
    def scan_addresses(self, full: bool = False) -> list[int]:
        own = self.cfg.can.gateway_address
        if full:
            addrs = set(range(0x01, 0xFF))
        else:
            addrs = {a for r in FAST_SCAN_RANGES for a in r}
            if self.client is not None:
                addrs |= set(self.client.heard)
            addrs |= {d.address for d in self.cfg.devices if d.bus == "can" and d.address is not None}
        addrs -= {own, 0x00, 0xFF}
        return sorted(addrs)

    async def identify(self, address: int, *, retries: int | None = None) -> DeviceInfo:
        client = await self._client()
        block = await client.request(address, idcodec.KANAL_ANFRAGE, 0x00, b"", mode="block",
                                     retries=retries)
        assert isinstance(block, bytes)
        try:
            ident = messages.IdentifyAntwort.parse(block)
        except (ValueError, Exception) as exc:  # noqa: BLE001
            raise ProtocolError(f"{address:#04x}: Identify unlesbar: {exc}") from exc
        return DeviceInfo(bus="can", address=address, regler_typ=ident.regler_typ,
                          type_name=type_name(ident.regler_typ, address), name=ident.name,
                          version=ident.version_text, serial=ident.seriennummer,
                          raw={"prg_crc": ident.prg_crc, "datum": ident.datum,
                               "struktur_version": ident.version, "major": ident.major,
                               "minor": ident.minor})

    async def scan(self, progress: ProgressCb | None = None, full: bool = False) -> list[DeviceInfo]:
        """Identify an alle Kandidaten (gedrosselt). ``full=False``: nur bekannte Adressbereiche
        plus passiv gehörte und konfigurierte Adressen (Default für den Feldtest)."""
        client = await self._client()
        if progress:
            progress(0, 1, "Höre passiv mit …")
        await client.listen(1.0)
        addrs = self.scan_addresses(full)
        heard = set(client.heard)
        configured = {d.address for d in self.cfg.devices if d.bus == "can"}
        found: list[DeviceInfo] = []
        for i, addr in enumerate(addrs):
            if progress:
                progress(i, len(addrs), f"Identify {addr:#04x}")
            # Wiederholung nur für Adressen, von denen wir wissen, dass dort jemand ist
            retries = None if addr in heard or addr in configured else 0
            try:
                info = await self.identify(addr, retries=retries)
            except DeviceTimeout:
                continue
            except ProtocolError as exc:
                log.warning("Scan %#04x: %s", addr, exc)
                continue
            info.raw["heard"] = addr in heard
            found.append(info)
            log.info("Scan: %#04x %s „%s“ V%s SN %s", addr, info.type_name, info.name, info.version,
                     info.serial)
        silent = sorted(a for a in client.heard if a not in {f.address for f in found}
                        and a not in (self.cfg.can.gateway_address, 0x00, 0xFF))
        if silent:
            log.info("Scan: gehört, aber ohne Identify-Antwort: %s", ", ".join(f"{a:#04x}" for a in silent))
        if progress:
            progress(len(addrs), len(addrs), f"{len(found)} Gerät(e) gefunden")
        return found

    # ===== Aufzählung ===========================================================================
    def _info(self, kind: str, det: messages.DetailAntwort, regler_typ: int | None) -> ValueInfo:
        vt, nk, out = det.vartyp, det.nachkomma, det.ausgabe
        numeric = vartyp.is_numeric(vt) and (out & safety.OUT_MASK) not in vartyp.NON_NUMERIC_OUT
        lock = None
        if kind == "param":
            lock = safety.param_lock_reason("can", regler_typ, det.nr, out_type=out)
            if lock is None and not vartyp.is_numeric(vt):
                lock = f"VarTyp {vartyp.vartyp_name(vt)} nicht numerisch"
        lim = (lambda x: vartyp.limit_to_value(x, vt, nk)) if vartyp.is_numeric(vt) else (lambda x: None)
        default = None
        if kind == "param" and vartyp.is_numeric(vt) and det.werk != -32768:   # USERWERK
            default = lim(det.werk)
        return ValueInfo(kind=kind, key=det.nr, name=names.display_name(det.name, det.nr), unit=det.unit,
                         decimals=vartyp.effective_decimals(nk, out), vartyp=vt, out_type=out,
                         out_textauswahl=(out & safety.OUT_MASK) == safety.OUT_TEXTAUSWAHL,
                         min=lim(det.min), max=lim(det.max), default=default,
                         value=self._det_value(det) if numeric else None, tip=det.tip,
                         supported=numeric, lock_reason=lock, texts=names.fixed_texts(out))

    async def enumerate(self, device: DeviceConfig, progress: ProgressCb | None = None) -> list[ValueInfo]:
        """Werteliste per 0x30 und 0x38 mit Nr=0xFFFF (nur auf Knopfdruck – große Antwortflut)."""
        addr = self._check_device(device)
        client = await self._client()
        infos: dict[tuple[str, int], ValueInfo] = {}
        for kind, sub, text in (("value", 0x30, "Messwerte"), ("param", 0x38, "Parameter")):
            if progress:
                progress(len(infos), len(infos) + 1, f"{text} anfordern …")
            try:
                blocks = await client.request(addr, idcodec.KANAL_ANFRAGE, sub, messages.build_anfrage(0xFFFF),
                                              mode="enum", regler_typ=device.regler_typ, retries=0,
                                              allow_enumeration=True)
            except DeviceTimeout:
                if kind == "value":
                    # RS_POWERUP: Messwert-Anfragen werden ignoriert (BusIn.cpp:163, 172)
                    log.warning("%s: keine Messwert-Details (Gerät im Hochlauf?)", device.id)
                    continue
                raise
            assert isinstance(blocks, list)
            for block in blocks:
                try:
                    det = messages.DetailAntwort.parse(block)
                except ValueError as exc:
                    log.warning("%s: Detail unlesbar: %s", device.id, exc)
                    continue
                self._remember(addr, det, kind)
                infos[(kind, det.nr)] = self._info(kind, det, device.regler_typ)
            if progress:
                progress(len(infos), len(infos) + 1, f"{text}: {len(blocks)} Einträge")
        result = sorted(infos.values(), key=lambda i: (i.kind != "value", i.key))
        await self._load_texts(client, device, result, progress)
        if progress:
            progress(len(result), len(result), f"{len(result)} Einträge")
        if not result:
            raise DeviceTimeout(f"{device.id}: keine Werteliste erhalten")
        return result

    async def _load_texts(self, client: CanClient, device: DeviceConfig, infos: list[ValueInfo],
                          progress: ProgressCb | None) -> None:
        """Auswahltexte (Anfrage 0x0B/0x08) für alle Einträge mit OUT_TEXTAUSWAHL – vom Gerät in
        dessen eingestellter Sprache. Fehlt die Antwort, bleibt es bei der Zahl."""
        todo = [i for i in infos if i.out_textauswahl and i.supported and i.min is not None and i.max is not None]
        seen: dict[int, dict[int, str]] = {}
        for n, info in enumerate(todo):
            if progress:
                progress(n, len(todo), f"Auswahltexte {n + 1}/{len(todo)}")
            if info.key in seen:                      # Messwert und Parameter mit gleicher Nummer
                info.texts = dict(seen[info.key])
                continue
            try:
                texts = await self.read_auswahltexte(client, device, info.key, int(info.min), int(info.max))
            except (DeviceTimeout, ProtocolError) as exc:
                log.info("%s: keine Auswahltexte für %#06x (%s)", device.id, info.key, exc)
                continue
            if texts:
                info.texts = seen[info.key] = texts

    async def read_auswahltexte(self, client: CanClient, device: DeviceConfig, nr: int,
                                lo: int, hi: int) -> dict[int, str]:
        """Wert -> Text für ``lo..hi``. Die Firmware sendet bis zu 10 Texte je Block."""
        addr = self._check_device(device)
        if not 0 <= hi - lo <= 255:
            return {}
        blocks = await client.request(addr, idcodec.KANAL_ANFRAGE, 0x08, messages.build_anfrage(nr),
                                      mode="enum", regler_typ=device.regler_typ, retries=0,
                                      idle_s=0.4, max_s=10.0)
        assert isinstance(blocks, list)
        out: dict[int, str] = {}
        for block in blocks:
            try:
                ans = messages.AuswahltexteAntwort.parse(block)
            except ValueError as exc:
                log.warning("%s: Auswahltexte %#06x unlesbar: %s", device.id, nr, exc)
                continue
            if ans.nr != nr:
                continue
            for j, text in enumerate(ans.texte):
                if lo <= ans.offset + j <= hi and text.strip():
                    out[ans.offset + j] = text.strip()
        return out

    # ===== Lesen ================================================================================
    async def _value_meta(self, client: CanClient, device: DeviceConfig, v: ValueDef) -> _Meta | None:
        addr = device.address
        assert addr is not None
        key = (addr, v.key, "value")
        if key in self._meta:
            return self._meta[key]
        failed = self._meta_fail.get((addr, v.key))
        if failed is not None and time.monotonic() - failed < META_RETRY_S:
            return None
        try:
            await self._detail(client, addr, 0x30, v.key, device.regler_typ)
        except (DeviceTimeout, ProtocolError) as exc:
            self._meta_fail[(addr, v.key)] = time.monotonic()
            log.warning("%s: Ausgabetyp von %#06x unbekannt (%s) – Skalierung nur über Nachkomma",
                        device.id, v.key, exc)
            return None
        return self._meta.get(key)

    async def read_values(self, device: DeviceConfig, values: list[ValueDef]) -> dict[int, float | None]:
        addr = self._check_device(device)
        client = await self._client()
        out: dict[int, float | None] = {}
        answered = 0
        misses = 0
        for v in values:
            meta = await self._value_meta(client, device, v)
            try:
                data = await client.request(addr, idcodec.KANAL_ANFRAGE, 0x20, messages.build_anfrage(v.key),
                                            mode="single", nr=v.key, regler_typ=device.regler_typ)
            except DeviceTimeout:
                out[v.key] = None
                misses += 1
                if not answered and misses >= OFFLINE_AFTER:
                    raise DeviceTimeout(f"{device.id} ({addr:#04x}) antwortet nicht") from None
                continue
            answered += 1
            misses = 0
            assert isinstance(data, bytes)
            ans = messages.MesswertAntwort.parse(data)
            if not vartyp.is_numeric(ans.vartyp):
                log.warning("%s: %#06x hat nicht-numerischen VarTyp %s", device.id, v.key,
                            vartyp.vartyp_name(ans.vartyp))
                out[v.key] = None
                continue
            if not vartyp.same_vartyp(ans.vartyp, v.vartyp):
                log.warning("%s: %#06x VarTyp am Bus %s ≠ Konfiguration %s – Buswert maßgeblich",
                            device.id, v.key, vartyp.vartyp_name(ans.vartyp), vartyp.vartyp_name(v.vartyp))
            raw = vartyp.decode_raw(ans.vartyp, ans.wert)
            nk = meta.decimals if meta else v.decimals
            val = vartyp.to_value(raw, ans.vartyp, nk, meta.out_type if meta else None)
            out[v.key] = val if math.isfinite(val) else None
        if values and not answered:
            raise DeviceTimeout(f"{device.id} ({addr:#04x}) antwortet nicht")
        return out

    async def read_params(self, device: DeviceConfig, params: list[ParamDef]) -> dict[int, float | None]:
        addr = self._check_device(device)
        client = await self._client()
        out: dict[int, float | None] = {}
        answered = 0
        misses = 0
        for p in params:
            try:
                det = await self._detail(client, addr, 0x38, p.key, device.regler_typ)
            except DeviceTimeout:
                out[p.key] = None
                misses += 1
                if not answered and misses >= OFFLINE_AFTER:
                    raise DeviceTimeout(f"{device.id} ({addr:#04x}) antwortet nicht") from None
                continue
            answered += 1
            misses = 0
            if det.vartyp != p.vartyp:
                log.warning("%s: Parameter %#06x VarTyp am Bus %s ≠ Konfiguration %s", device.id, p.key,
                            vartyp.vartyp_name(det.vartyp), vartyp.vartyp_name(p.vartyp))
            out[p.key] = self._det_value(det)
        if params and not answered:
            raise DeviceTimeout(f"{device.id} ({addr:#04x}) antwortet nicht")
        return out

    # ===== Schreiben ============================================================================
    async def write_param(self, device: DeviceConfig, param: ParamDef, value: float) -> float:
        addr = self._check_device(device)
        nr = param.key
        if reason := safety.param_lock_reason("can", device.regler_typ, nr):
            raise SafetyViolation(f"{device.id}: Parameter {nr:#06x} gesperrt ({reason})")
        if not vartyp.is_numeric(param.vartyp):
            raise SafetyViolation(f"{device.id}: Parameter {nr:#06x} hat nicht-numerischen VarTyp")
        client = await self._client()

        # 1. aktuellen Stand frisch vom Gerät (VarTyp, Ausgabetyp, Min/Max)
        cur = await self._detail(client, addr, 0x38, nr, device.regler_typ)
        if reason := safety.param_lock_reason("can", device.regler_typ, nr, out_type=cur.ausgabe):
            raise SafetyViolation(f"{device.id}: Parameter {nr:#06x} gesperrt ({reason})")
        if cur.vartyp != param.vartyp:
            raise ProtocolError(f"{device.id}: Parameter {nr:#06x}: VarTyp am Gerät "
                                f"{vartyp.vartyp_name(cur.vartyp)} ≠ Konfiguration "
                                f"{vartyp.vartyp_name(param.vartyp)} – nicht gesendet "
                                f"(Gerät würde stillschweigend verwerfen). Werteliste neu laden.")
        vt, nk = cur.vartyp, cur.nachkomma

        # 2. Rohwert + Bereichsprüfungen
        try:
            raw = vartyp.value_to_raw(value, vt, nk, cur.ausgabe)
            wert = vartyp.encode_raw(vt, raw)
        except ValueError as exc:
            raise WriteRejected("value", f"{nr:#06x}: {exc}") from exc
        lo, hi = vartyp.limit_raw(vt, cur.min), vartyp.limit_raw(vt, cur.max)
        if not lo <= raw <= hi:
            raise WriteRejected("value", f"{nr:#06x}: Wert {value} außerhalb des Gerätebereichs "
                                         f"{vartyp.limit_to_value(cur.min, vt, nk)}.."
                                         f"{vartyp.limit_to_value(cur.max, vt, nk)}")
        try:
            cur_raw = vartyp.decode_raw(vt, cur.wert)
        except ValueError:
            cur_raw = None
        if cur_raw is not None and cur_raw == raw:
            log.info("%s: Parameter %#06x hat bereits den Wert %s – nicht gesendet", device.id, nr, value)
            return vartyp.to_value(cur_raw, vt, nk, cur.ausgabe)

        # 3. genau einmal senden, Bestätigung abwarten (kein Retry!)
        data = messages.build_parameter_setzen(nr, vt, wert)
        log.info("%s: schreibe Parameter %#06x = %s (roh %s, %s)", device.id, nr, value, raw,
                 vartyp.vartyp_name(vt))
        try:
            block = await client.request(addr, idcodec.KANAL_BEFEHLE, 0x00, data, mode="block", nr=nr,
                                         regler_typ=device.regler_typ, retries=0,
                                         timeout_s=max(WRITE_CONFIRM_TIMEOUT_S,
                                                       self.cfg.can.timing.request_timeout_ms / 1000.0),
                                         confirm_sub=0x38)
        except ProtocolError as exc:
            raise DeviceTimeout(f"{device.id}: Schreibbestätigung {nr:#06x} unlesbar ({exc}) – "
                                f"Ergebnis unbekannt, nicht wiederholt") from exc
        except DeviceTimeout as exc:
            raise DeviceTimeout(f"{device.id}: keine Schreibbestätigung für {nr:#06x} – Ergebnis "
                                f"unbekannt, nicht wiederholt") from exc
        assert isinstance(block, bytes)
        try:
            det = messages.DetailAntwort.parse(block)
        except ValueError as exc:
            raise DeviceTimeout(f"{device.id}: Schreibbestätigung {nr:#06x} unlesbar: {exc}") from exc
        self._remember(addr, det, "param")
        actual = self._det_value(det)
        if actual is None:
            raise ProtocolError(f"{device.id}: Schreibbestätigung {nr:#06x} ohne gültigen Wert")
        if not safety.verify_readback(vartyp.to_value(raw, vt, nk, cur.ausgabe), actual, nk):
            log.error("%s: Parameter %#06x Soll %s, Gerät meldet %s (Firmware hat korrigiert?)",
                      device.id, nr, value, actual)
        return actual
