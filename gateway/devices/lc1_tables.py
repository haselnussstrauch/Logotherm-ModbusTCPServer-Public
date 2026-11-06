"""LambdaControl 1 (RS232): Messwert- und Parametertabellen – generiert, nicht von Hand ändern!

Erzeugt (Generator nicht veröffentlicht) aus den LC1-Quellen:
  LCS/LCS9/LCS9.ARB (K-Werte, Status-Codes), LCS/LCS9/LCS1.par (Parameter),
  LCS/LCS9/HS-LCS9.BIN (LCD-Texte Deutsch), D-Lcs-alt/LCS1.PAR (Skalierung O2x10/CO2x10).

ACHTUNG: Die E-Belegung gilt für die Firmware-Generation LCS9 / LCS1 V1.xx. Ältere Stände
(z. B. D-Lcs-alt/LCS1.PAR) haben eine andere Belegung (dort ist E43 der Kesselsollwert).
Gesperrte E-Indizes stehen NICHT hier, sondern in gateway.safety.param_lock_reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Lc1Value:
    """Messwert K(k) aus der Antwort auf Befehl 50 (Arbeitsdaten)."""
    k: int
    name: str
    unit: str
    decimals: int                 # Anzeige; der Regler sendet Format ######.##
    source: str
    tip: str = ""
    texts: dict[int, str] = field(default_factory=dict)   # Code -> Text (Status/Störung)


@dataclass(frozen=True)
class Lc1Param:
    """Parameter E(e): 16 Bit unsigned im EEPROM; Wert = Rohwert / 10**decimals."""
    e: int
    text: str
    level: str                    # Ebene aus LCS1.par, siehe LEVEL_TEXT
    unit: str
    unit_raw: str                 # Einheitentext aus LCS1.par
    min_raw: int
    max_raw: int
    decimals: int
    source: str
    note: str = ""

    def scale(self, raw: int) -> float:
        return raw / 10 ** self.decimals

    def to_raw(self, value: float) -> int:
        return round(value * 10 ** self.decimals)

    @property
    def min(self) -> float:
        return self.scale(self.min_raw)

    @property
    def max(self) -> float:
        return self.scale(self.max_raw)


#: Ebenen in LCS1.par. TODO(vor Ort): Bezeichnungen 2..5 mit Logotherm abstimmen.
LEVEL_TEXT: dict[str, str] = {
    "0": "Betreiber",             # E44/E45 Kesselsollwert, Sommer/Winter
    "1": "Installateur",          # LCS9.ARB:1082 "INSTALLATEUR [1]"
    "2": "Kesselhersteller",      # Regelparameter E14..E28 (Annahme)
    "3": "Werk",                  # Reglernummer, Produktionsdatum (Annahme)
    "4": "Kalibrierung",          # Fühler-/Sonden-/Servoabgleich (Annahme)
    "5": "Zähler/Diagnose",       # Stunden, Störungen (Annahme)
    "/": "Reserve",
}

#: Störungsnummer S = K(7) (LCS9.ARB:535-604; Texte HS-LCS9.BIN, LCS9.ARB:877).
STOERUNG_TEXTE: dict[int, str] = {
    0: "Keine Störung",  # LCS9.ARB:539 S=0; Text L+848
    1: 'Datenverlust Regler defekt',  # Ursachen: Regler defekt, Regler defekt, Regler defekt
    2: 'Kesseltemperatur Messwerte falsch',  # Ursachen: Stecker/Leitung, Fühler defekt, Regler defekt
    3: 'Abgastemperatur Messwerte falsch',  # Ursachen: Stecker/Leitung, Fühler defekt, Regler defekt
    4: 'STB ausgelöst Rücksetzen !',  # Ursachen: Wärmeentnahme, zu gering !, Pumpen/Ventile
    5: 'Luftregeleinheit primär defekt',  # Ursachen: Stecker/Leitung, Motor/Regler, 24/12 V-AC ?
    6: 'Luftregeleinheit sekundär defekt',  # Ursachen: Stecker/Leitung, Motor/Regler, 24/12 V-AC ?
    7: 'Sauerstoff Messwerte falsch',  # Ursachen: Sonde verbraucht, oder verschmutzt, Stecker/Leitung
    8: 'Speichertemp. Messwerte falsch',  # Ursachen: Stecker/Leitung, Fühler defekt, Regler defekt
    9: 'Kesseltemperatur zu niedrig',  # Ursachen: Rücklaufventil, Stecker/Leitung, Motor/Regler
    10: 'Kein Feuer',  # Ursachen: Brennstoff, nicht gezündet, Brennstoff aus
    11: 'Rücklauftemp. Messwerte falsch',  # Ursachen: Stecker/Leitung, Fühler defekt, Regler defekt
}

#: Betriebsstatus B = K(28) (LCS9.ARB:84-85, 544, 863-870; Texte HS-LCS9.BIN).
BETRIEBSSTATUS_TEXTE: dict[int, str] = {
    0: 'Wärmeerzeugung AUS',
    1: 'Wärmeerzeugung EIN (O2-Regelprogramm)',
    2: 'Wärmeerzeugung EIN (TA-Regelprogramm)',
    3: 'Bereitschaft',
    4: 'Schutzfunktionen',
}

#: weitere Code-Texte je K
_EXTRA_TEXTE: dict[int, dict[int, str]] = {
    30: {0: 'Sommerbetrieb', 1: 'Winterbetrieb'},
    31: {0: '-', 1: 'Nennleistung', 2: 'Teillast', 3: 'Sicherheitstest'},
    33: {0: 'NEIN', 1: 'JA'},
}

VALUES: dict[int, Lc1Value] = {
    1: Lc1Value(1, 'T-Speicher', '°C', 2, 'LCS/LCS9/LCS9.ARB:496', 'Speichertemperatur; bei System 5 Rücklauftemperatur (D-Lcs-alt/LCS17.BAS:262, 660)', {}),  # [°C]
    2: Lc1Value(2, 'TK Ist', '°C', 2, 'LCS/LCS9/LCS9.ARB:497', '', {}),  # [°C]
    3: Lc1Value(3, 'TA Ist', '°C', 2, 'LCS/LCS9/LCS9.ARB:497', '', {}),  # [°C]
    4: Lc1Value(4, 'O2 Ist aktuell', '%', 2, 'LCS/LCS9/LCS9.ARB:498', '', {}),  # [%]
    5: Lc1Value(5, 'R-Ventil (Y1)', '%', 2, 'LCS/LCS9/LCS9.ARB:498', '', {}),  # [%]
    6: Lc1Value(6, 'L-Ventil (Y2)', '%', 2, 'LCS/LCS9/LCS9.ARB:499', '', {}),  # [%]
    7: Lc1Value(7, 'Störung', '', 0, 'LCS/LCS9/LCS9.ARB:499', 'Störungsnummer S (LCS9.ARB:492, 535-604)', STOERUNG_TEXTE),  # [Nr]
    8: Lc1Value(8, 'TAR Ist', '°C', 2, 'LCS/LCS9/LCS9.ARB:500', '', {}),  # [°C]
    9: Lc1Value(9, 'CO2 Ist', '%', 2, 'LCS/LCS9/LCS9.ARB:500', '', {}),  # [%]
    10: Lc1Value(10, 'Lambda', '', 2, 'LCS/LCS9/LCS9.ARB:501', '', {}),  # [/]
    11: Lc1Value(11, 'ETA-F', '%', 2, 'LCS/LCS9/LCS9.ARB:501', '', {}),  # [%]
    12: Lc1Value(12, 'O2 Ist', '%', 2, 'LCS/LCS9/LCS9.ARB:502', '', {}),  # [%]
    13: Lc1Value(13, 'CO2 Soll', '%', 2, 'LCS/LCS9/LCS9.ARB:502', '', {}),  # [%]
    14: Lc1Value(14, 'O2 Soll', '%', 2, 'LCS/LCS9/LCS9.ARB:503', '', {}),  # [%]
    15: Lc1Value(15, 'TK Soll', '°C', 2, 'LCS/LCS9/LCS9.ARB:503', '', {}),  # [°C]
    16: Lc1Value(16, 'TK-Trend', '°C/min', 2, 'LCS/LCS9/LCS9.ARB:504', '', {}),  # [°C/min]
    17: Lc1Value(17, 'R-Ventil-Soll', '°C', 2, 'LCS/LCS9/LCS9.ARB:504', '', {}),  # [°C]
    18: Lc1Value(18, 'TAR Soll', '°C', 2, 'LCS/LCS9/LCS9.ARB:505', '', {}),  # [°C]
    19: Lc1Value(19, 'TAR-Trend', '°C/min', 2, 'LCS/LCS9/LCS9.ARB:505', '', {}),  # [°C/min]
    20: Lc1Value(20, 'Begrenzung', '', 0, 'LCS/LCS9/LCS9.ARB:506', 'Begrenzungsnummer GQ (LCS9.ARB:153, 162, 488) – TODO: Bedeutung der Codes klären', {}),  # [Nr]
    21: Lc1Value(21, 'VL-primär', '%', 2, 'LCS/LCS9/LCS9.ARB:506', '', {}),  # [%]
    22: Lc1Value(22, 'Rv-Bandbreite', '°C', 2, 'LCS/LCS9/LCS9.ARB:507', '', {}),  # [°C]
    23: Lc1Value(23, 'VL-sekundär', '%', 2, 'LCS/LCS9/LCS9.ARB:507', '', {}),  # [%]
    24: Lc1Value(24, 'VLp-Verstärker', '%', 2, 'LCS/LCS9/LCS9.ARB:508', '', {}),  # [%]
    25: Lc1Value(25, 'Time Betrieb', 'min', 0, 'LCS/LCS9/LCS9.ARB:508', '', {}),  # [min]
    26: Lc1Value(26, 'Timer O2', 'min', 0, 'LCS/LCS9/LCS9.ARB:509', '', {}),  # [min]
    27: Lc1Value(27, 'Timer WZ-AUS', 'min', 0, 'LCS/LCS9/LCS9.ARB:509', '', {}),  # [min]
    28: Lc1Value(28, 'Betriebsstatus', '', 0, 'LCS/LCS9/LCS9.ARB:510', 'Betriebsstatus B (LCS9.ARB:84-85, 544, 863-870)', BETRIEBSSTATUS_TEXTE),  # [F]
    29: Lc1Value(29, 'System', '', 0, 'LCS/LCS9/LCS9.ARB:510', 'Systemanwendung E(32) (LCS9.ARB:493)', {}),  # [Nr]
    30: Lc1Value(30, 'Sommer/Winter', '', 0, 'LCS/LCS9/LCS9.ARB:511', 'Sommer-/Winterbetrieb E(45) (LCS9.ARB:494)', _EXTRA_TEXTE[30]),  # [F]
    31: Lc1Value(31, 'Kaminf. Test', '', 0, 'LCS/LCS9/LCS9.ARB:511', 'Kaminfegertest E (LCS9.ARB:865-867)', _EXTRA_TEXTE[31]),  # [Nr]
    32: Lc1Value(32, 'L-Ventil-Soll', '°C', 2, 'LCS/LCS9/LCS9.ARB:512', '', {}),  # [°C]
    33: Lc1Value(33, 'SP-F NEIN/JA', '', 0, 'LCS/LCS9/LCS9.ARB:512', 'Speicherfühler E(33) (LCS9.ARB:494)', _EXTRA_TEXTE[33]),  # [F]
    34: Lc1Value(34, 'VLs-Verstärker', '%', 2, 'LCS/LCS9/LCS9.ARB:513', '', {}),  # [%]
    35: Lc1Value(35, 'Timer R-Vent', 'min', 0, 'LCS/LCS9/LCS9.ARB:513', '', {}),  # [min]
}

PARAMS: dict[int, Lc1Param] = {
    0: Lc1Param(0, 'Reglernummer', '3', '', 'F', 0, 65534, 0, 'LCS/LCS9/LCS1.par:1'),
    1: Lc1Param(1, 'Produziert im Monat/Jahr', '3', '', 'Code', 0, 65535, 0, 'LCS/LCS9/LCS1.par:2'),
    2: Lc1Param(2, 'Basisfiles', '3', '', 'Nr', 0, 9, 0, 'LCS/LCS9/LCS1.par:3'),
    3: Lc1Param(3, 'Kesselminimaltemperatur', '3', '°C', '°C', 60, 70, 0, 'LCS/LCS9/LCS1.par:4'),
    4: Lc1Param(4, 'TK-Fühler mV bei 93.1R', '4', 'mV', 'mVx10', 940, 2470, 1, 'LCS/LCS9/LCS1.par:5'),
    5: Lc1Param(5, 'TK-Fühler mV bei 155.0R', '4', 'mV', 'mVx10', 22000, 24230, 1, 'LCS/LCS9/LCS1.par:6'),
    6: Lc1Param(6, 'TA-Fühler mV bei 93.1R', '4', 'mV', 'mVx10', 450, 1080, 1, 'LCS/LCS9/LCS1.par:7'),
    7: Lc1Param(7, 'TA-Fühler mV bei 250.0R', '4', 'mV', 'mVx10', 19820, 21090, 1, 'LCS/LCS9/LCS1.par:8'),
    8: Lc1Param(8, 'SP-Fühler mV bei 93.1R', '4', 'mV', 'mVx10', 450, 1080, 1, 'LCS/LCS9/LCS1.par:9'),
    9: Lc1Param(9, 'SP-Fühler mV bei 155.0R', '4', 'mV', 'mVx10', 8223, 8979, 1, 'LCS/LCS9/LCS1.par:10'),
    10: Lc1Param(10, 'O2-Sonde Eingang 0mV', '4', 'mV', 'mVx10', 4390, 4650, 1, 'LCS/LCS9/LCS1.par:11'),
    11: Lc1Param(11, 'O2-Sonde Eingang 100mV', '4', 'mV', 'mVx10', 23810, 24370, 1, 'LCS/LCS9/LCS1.par:12'),
    12: Lc1Param(12, 'Servo-primär bei 10V', '4', '', 'I', 1, 54, 0, 'LCS/LCS9/LCS1.par:13'),
    13: Lc1Param(13, 'Servo-sekundundär bei 10V', '4', '', 'I', 1, 54, 0, 'LCS/LCS9/LCS1.par:14'),
    14: Lc1Param(14, 'Kesseltemperatur max (TKmax)', '2', '°C', '°C', 90, 110, 0, 'LCS/LCS9/LCS1.par:15'),
    15: Lc1Param(15, 'TARmax Maximalleistung', '2', '°C', '°C', 30, 200, 0, 'LCS/LCS9/LCS1.par:16'),
    16: Lc1Param(16, 'TARmin Teillastbetrieb', '2', '°C', '°C', 10, 180, 0, 'LCS/LCS9/LCS1.par:17'),
    17: Lc1Param(17, 'VLmin-primär Betr.-Bereitsch.', '2', '%', '%', 0, 30, 0, 'LCS/LCS9/LCS1.par:18'),
    18: Lc1Param(18, 'VL-Aufteilung TA-Regelung', '2', '', 'F', 10, 200, 0, 'LCS/LCS9/LCS1.par:19'),
    19: Lc1Param(19, 'O2-Regelung NEIN=0/JA=1', '2', '', 'F', 0, 1, 0, 'LCS/LCS9/LCS1.par:20'),
    20: Lc1Param(20, 'O2-Sollwert bei TARmax', '2', '%', '%', 20, 120, 1, "LCS/LCS9/LCS1.par:21; LCS9.ARB:475,481 OS=OS/10; D-Lcs-alt/LCS1.PAR:21 'O2x10'"),
    21: Lc1Param(21, 'O2-Sollwert bei TARmin', '2', '%', '%', 20, 120, 1, "LCS/LCS9/LCS1.par:22; LCS9.ARB:475,481 OS=OS/10; D-Lcs-alt/LCS1.PAR:22 'O2x10'"),
    22: Lc1Param(22, 'Brennstoff CO2%max', '2', '%', '%', 50, 209, 1, "LCS/LCS9/LCS1.par:23; LCS9.ARB:481,484 E(22)/10; D-Lcs-alt/LCS1.PAR:23 'CO2x10'"),
    23: Lc1Param(23, 'Sekundärverstärkung', '2', '', 'F', 1, 255, 0, 'LCS/LCS9/LCS1.par:24'),
    24: Lc1Param(24, 'Primärverstärkung', '2', '', 'F', 1, 255, 0, 'LCS/LCS9/LCS1.par:25'),
    25: Lc1Param(25, 'O2-Abschalttimer Start bei >', '2', '%', '%', 0, 18, 0, 'LCS/LCS9/LCS1.par:26'),
    26: Lc1Param(26, 'Funktion Nachlegen', '2', 'min', 'min', 0, 20, 0, 'LCS/LCS9/LCS1.par:27'),
    27: Lc1Param(27, 'Drehzahl-2 Freigabe', '2', '', 'F', 0, 3, 0, 'LCS/LCS9/LCS1.par:28'),
    28: Lc1Param(28, 'Türmagnet', '2', '', 'F', 0, 2, 0, 'LCS/LCS9/LCS1.par:29'),
    29: Lc1Param(29, 'Kesseltypennummer', '1', '', 'Nr', 0, 108, 0, 'LCS/LCS9/LCS1.par:30'),
    30: Lc1Param(30, 'Reserve (keine Funktion).....', '/', '', '/', 0, 65535, 0, 'LCS/LCS9/LCS1.par:31', 'Max 65565 in LCS1.par > 65535, auf 65535 begrenzt'),
    31: Lc1Param(31, 'Sprache (D=0)', '1', '', 'F', 0, 4, 0, 'LCS/LCS9/LCS1.par:32'),
    32: Lc1Param(32, 'Systemanwendung', '1', '', 'F', 1, 5, 0, 'LCS/LCS9/LCS1.par:33'),
    33: Lc1Param(33, 'Speicherfühler NEIN=0/JA=1', '1', '', 'F', 0, 1, 0, 'LCS/LCS9/LCS1.par:34'),
    34: Lc1Param(34, 'SP-Fühler Leitungslänge', '1', 'm', 'm', 1, 50, 0, 'LCS/LCS9/LCS1.par:35'),
    35: Lc1Param(35, 'Speichermax. (TSmax)', '1', '°C', '°C', 55, 90, 0, 'LCS/LCS9/LCS1.par:36'),
    36: Lc1Param(36, 'Speichermin. (TSmin)', '1', '°C', '°C', 30, 85, 0, 'LCS/LCS9/LCS1.par:37'),
    37: Lc1Param(37, 'Kesselmin. (TKmin)', '1', '°C', '°C', 30, 80, 0, 'LCS/LCS9/LCS1.par:38'),
    38: Lc1Param(38, 'Abgastemp. min (TAmin)', '1', '°C', '°C', 90, 160, 0, 'LCS/LCS9/LCS1.par:39'),
    39: Lc1Param(39, 'Rücklaufventil Laufzeit', '1', 's', 'sec', 120, 240, 0, 'LCS/LCS9/LCS1.par:40'),
    40: Lc1Param(40, 'Ladeventil Laufzeit', '1', 's', 'sec', 120, 240, 0, 'LCS/LCS9/LCS1.par:41'),
    41: Lc1Param(41, 'Differenz (System 5) TK-TR', '1', '°C', '°C', 5, 25, 0, 'LCS/LCS9/LCS1.par:42'),
    42: Lc1Param(42, 'System5 ÖL-GAS NEIN=0/JA=1', '1', '', 'F', 0, 1, 0, 'LCS/LCS9/LCS1.par:43'),
    43: Lc1Param(43, 'System5 ÖL-GAS TR-Soll', '1', '°C', '°C', 30, 80, 0, 'LCS/LCS9/LCS1.par:44'),
    44: Lc1Param(44, 'Kesseltemperatur Sollwert', '0', '°C', '°C', 70, 85, 0, 'LCS/LCS9/LCS1.par:45'),
    45: Lc1Param(45, 'Sommerbetr.=0/Winterbetr.=1', '0', '', 'F', 0, 1, 0, 'LCS/LCS9/LCS1.par:46'),
    46: Lc1Param(46, 'System5 ÖL-GAS NEIN=0/JA=1', '5', '', 'F', 0, 1, 0, 'LCS/LCS9/LCS1.par:47'),
    47: Lc1Param(47, 'System Minimalbegrenzung', '5', '', 'F', 1, 5, 0, 'LCS/LCS9/LCS1.par:48'),
    48: Lc1Param(48, 'Bereitschaftsstunden', '5', 'h', 'h', 0, 65535, 0, 'LCS/LCS9/LCS1.par:49'),
    49: Lc1Param(49, 'Betriebsstunden', '5', 'h', 'h', 0, 65535, 0, 'LCS/LCS9/LCS1.par:50'),
    50: Lc1Param(50, 'Regler am Netz', '5', 'h', 'h', 0, 65535, 0, 'LCS/LCS9/LCS1.par:51'),
    51: Lc1Param(51, 'Abgastemperatur Höchstwert', '5', '°C', '°C', 0, 65535, 0, 'LCS/LCS9/LCS1.par:52'),
    52: Lc1Param(52, 'Letzte Störung [LOGOTHERM]', '5', '', 'F', 0, 65535, 0, 'LCS/LCS9/LCS1.par:53'),
    53: Lc1Param(53, 'Reparatur im Monat/Jahr', '5', '', 'Code', 0, 65535, 0, 'LCS/LCS9/LCS1.par:54'),
    54: Lc1Param(54, 'ON ERROR Zähler', '5', '', 'F', 0, 65535, 0, 'LCS/LCS9/LCS1.par:55'),
    55: Lc1Param(55, 'ERROR CODE', '5', '', 'F', 0, 65535, 0, 'LCS/LCS9/LCS1.par:56'),
    56: Lc1Param(56, 'O2-Sonde kalibriert vor', '5', 'h', 'h', 0, 65535, 0, 'LCS/LCS9/LCS1.par:57'),
    57: Lc1Param(57, 'O2-Sonden Offset', '5', '', '100+mVx10', 50, 260, 0, 'LCS/LCS9/LCS1.par:58', 'Rohwert: 100 + Offset in mV×10 (Anzeige (100-E57)/10, LCS9.ARB:1234)'),
    58: Lc1Param(58, 'Letzte Störung', '5', '', 'F', 0, 65535, 0, 'LCS/LCS9/LCS1.par:59'),
    59: Lc1Param(59, 'SP-Fühler Messwerte falsch', '5', '', 'F', 0, 65535, 0, 'LCS/LCS9/LCS1.par:60'),
    60: Lc1Param(60, 'O2-Sonde Messwerte falsch', '5', '', 'F', 0, 65535, 0, 'LCS/LCS9/LCS1.par:61'),
    61: Lc1Param(61, '. . . . . . . . . . . . . .', '/', '', '/', 0, 65535, 0, 'LCS/LCS9/LCS1.par:62', 'In LCS1 V1.xx Zähler (LCS1.ARB:719-720 DW=E(61)+1) – nicht schreiben'),
    62: Lc1Param(62, '. . . . . . . . . . . . . .', '/', '', '/', 0, 65535, 0, 'LCS/LCS9/LCS1.par:63', 'In LCS1 V1.xx Bereitschaftszähler (BC-Basisdaten/Module1.bas:113-116) – nicht schreiben'),
    63: Lc1Param(63, 'Letzter Betriebsstatus', '5', '', 'F', 0, 65535, 0, 'LCS/LCS9/LCS1.par:64'),
}

assert sorted(VALUES) == list(range(1, 36))
assert sorted(PARAMS) == list(range(64))
