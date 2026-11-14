# Home Assistant IKB Direkt Smart Meter Integration

[![HACS Validation](https://github.com/FranzFerdiand/ha-ikb-direkt/actions/workflows/validate.yml/badge.svg)](https://github.com/FranzFerdiand/ha-ikb-direkt/actions/workflows/validate.yml)
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/default)
[![GitHub Release](https://img.shields.io/github/v/release/FranzFerdiand/ha-ikb-direkt)](https://github.com/FranzFerdiand/ha-ikb-direkt/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Home Assistant Integration und Python-CLI zum automatischen Abruf von Smart-Meter-Verbrauchsdaten (15-Minuten-Lastprofile) aus dem Kundenportal der **Innsbrucker Kommunalbetriebe AG (IKB)** ([direkt.ikb.at](https://direkt.ikb.at)).

Die Daten werden als offizielle **Home Assistant Long-Term Statistics** eingepflegt und stehen direkt im **Energy Dashboard** mit stündlicher Historie und rückwirkend berechneten Kosten zur Verfügung.

---

## ✨ Funktionen

* ⚡ **15-Minuten-Auflösung:** Ruft Viertelstundenwerte ab und speichert vollständige Stunden als Langzeitstatistik. Der CLI-Export behält die Viertelstundenwerte.
* 💶 **Präzises Kosten-Tracking (EUR):** Berechnet stündliche Stromkosten direkt als persistente Long-Term Statistics (`..._kosten`) für das Home Assistant Energy Dashboard.
* 📈 **Flexible Tarifmodelle:** Unterstützt Einzeltarife (fester Arbeitspreis) sowie Mehrtarife mit Zeitfenstern (z. B. IKB-dual fix mit Sommer-Mittagsrabatt) inklusive Jahresgrundgebühr.
* 🔄 **Automatischer Kosten-Backfill:** Berechnet Kosten aus der gesamten vorhandenen Verbrauchsstatistik; fehlende Kostenpunkte und Preisänderungen werden beim nächsten Abruf berücksichtigt.
* 📊 **Energy Dashboard Integration:** Nahtlose Verknüpfung von Netzverbrauch (kWh) und Stromkosten (EUR) unter *Energie-Dashboard → Stromnetz*.
* 🛡️ **Sichere Authentifizierung & Resilienz:** Keycloak OpenID Connect SSO-Flow, automatische Session-Wiederverwendung und Retry-Mechanismus bei temporären Portal-Fehlern (HTTP 500).
* ⏱️ **Verzugs-Erkennung:** IKB liefert bis zur tatsächlichen Lieferung (oft etwa 24 Stunden später) Null-Platzhalter. Abschließende Nullstunden bleiben unbestätigt; auch ihr Alter beweist keine Lieferung.
* 💻 **Standalone CLI Tool:** Voll funktionsfähiges Befehlszeilenwerkzeug (`cli.py`) zum Exportieren von Messdaten nach CSV oder JSON – auch komplett ohne Home Assistant nutzbar.
* 🔢 **Live- & Diagnose-Sensoren:**
  * *Verbrauch Gestern (kWh)* (mit Attributen über Datenlieferung und Messdatum)
  * *Letzter 15-Min-Wert (kWh)*
  * *Zeitpunkt letzter positiver Messwert*
  * *Gesamter importierter Zählerstand (kWh)*

---

## 💶 Kosten-Tracking & Tarifmodelle

### Warum eine externe Kosten-Statistik?
Smart-Meter-Lastgänge werden vom Netzbetreiber zeitverzögert (meist in den frühen Morgenstunden für den Vortag) bereitgestellt. Daher kann kein einfacher Live-Preis-Sensor verwendet werden.

Die Integration berechnet für jedes stündliche Verbrauchsintervall rückwirkend die exakten Kosten in Euro und speichert sie als eigenständige Statistik (`ikb_direkt:<zählpunkt>_<art>_kosten`) in der Home Assistant Recorder-Datenbank ab.

### Unterstützte Tarifmodelle

Die Tarifkonfiguration erfolgt komfortabel über die Integrationseinstellungen (**Konfigurieren**):

| Modell | Identifier | Beschreibung |
|---|---|---|
| **Keines** | `none` | Nur Verbrauch (kWh), keine Kostenberechnung. |
| **Fester Tarif** | `fixed` | Konstanter Arbeitspreis pro kWh plus jährliche Grundgebühr. |
| **IKB-dual fix** | `dual_summer_noon` | Standard-Arbeitspreis, vergünstigter Nebentarif im Sommerhalbjahr während der Mittagsstunden (10:00–16:00 Uhr bzw. 12:00–14:00 Uhr je nach Vertragsfenster) sowie jährliche Grundgebühr. |

### Einstellbare Parameter im Options Flow

* **Tarifmodell (`tariff_model`):** Wähle zwischen `none`, `fixed` oder `dual_summer_noon`.
* **Arbeitspreis (`energy_price`):** Standardpreis in €/kWh (z. B. `0.1387` für 13,87 ct/kWh).
* **Nebentarif / Sommer-Mittagspreis (`offpeak_price`):** Vergünstigter Preis in €/kWh (z. B. `0.06935` für 6,935 ct/kWh).
* **Grundgebühr pro Jahr (`base_price_year`):** Feste Grundgebühr in €/Jahr (z. B. `26.40` für 26,40 €/Jahr; wird stundengenau umgelegt).
* **Historischer Erfassungszeitraum (`historical_days`):** Zeitraum in Tagen für Erstimport und wiederkehrende Korrekturen (Standard 35, maximal 365).

### Korrekturen und Kostenhistorie

Bei jedem stündlichen Abruf wird der konfigurierte Zeitraum erneut abgefragt. Vollständige, abgeschlossene Stunden werden mit der gespeicherten Historie abgeglichen. Nachgelieferte oder korrigierte Stunden ersetzen vorhandene Werte; alle nachfolgenden Summen werden angepasst. Fehlende Intervalle und unbestätigte abschließende Nullstunden überschreiben keine vorhandenen Messungen. Änderungen außerhalb des Abruffensters erfordern eine Vergrößerung von `historical_days` (maximal 365 Tage).

Die Kosten werden aus **allen vorhandenen Verbrauchsstunden** berechnet, auch wenn das Portal diese nicht mehr liefert. Preisänderungen rechnen die gesamte vorhandene Historie mit dem neu eingestellten Tarif um. Eine zeitlich gestaffelte Vertragshistorie wird nicht unterstützt: Bei einem echten Tarifwechsel sind historische Kosten daher nicht automatisch abrechnungsgenau. Kosten werden auf vier Nachkommastellen gespeichert; sie ersetzen keine Rechnung des Versorgers.

Das Sommerfenster gilt von April bis September in `Europe/Vienna`. Anfang und Ende sind als volle Stunden einstellbar (Standard 10–16, alternativ z. B. 12–14). Auch ein Preis von 0 €/kWh ist gültig. Die Jahresgrundgebühr wird auf die Stunden des jeweiligen Kalenderjahres verteilt, einschließlich Schaltjahren, und nur für vorhandene bestätigte Verbrauchsstunden verbucht.

**Grenze der Datenquelle:** Ohne Lieferstatus kann ein echter Nullverbrauch nicht von einem Platzhalter unterschieden werden. Nullstunden vor einem späteren positiven Messwert werden als geliefert behandelt; abschließende Nullstunden bleiben offen, bis spätere Daten diese Einordnung ermöglichen. Es gibt keine automatische Freigabe nach 24 oder 48 Stunden. `data_delivered` für gestern wird erst bei vollständiger Stundenabdeckung gesetzt (23/24/25 Stunden je nach Zeitumstellung). Der letzte positive Viertelstundenwert ist ein Diagnosewert, kein Live-Messwert.

---

## ⚡ Energy Dashboard einrichten

Für die vollständige Visualisierung von Verbrauch und Kosten im Home Assistant Energie-Dashboard:

1. Gehe in Home Assistant zu **Einstellungen → Dashboards → Energie**.
2. Klicke im Bereich **Stromnetz** auf **Verbrauch hinzufügen** (oder bearbeite den bestehenden Eintrag).
3. **Verbrauchssensor:** Wähle die IKB-Verbrauchsstatistik:
   ```text
   IKB Direkt [Zählernummer] (Verbrauch) (ikb_direkt:..._verbrauch)
   ```
4. **Kosten-Konfiguration:**
   * Wähle die Option **"Eine existierende Entität oder Statistik für die Kosten verwenden"**.
   * Wähle die korrespondierende IKB-Kostenstatistik:
     ```text
     IKB [Zählernummer] Kosten (ikb_direkt:..._verbrauch_kosten)
     ```
5. *(Optional)* Falls die Grundgebühr nicht über die Integration berechnet wird, kann alternativ eine feste tägliche Grundgebühr (z. B. `0.07233 €/Tag` bei 26,40 €/Jahr) im Dashboard hinterlegt werden.
6. Klicke auf **Speichern**. Die Daten werden geladen und stehen für Tag-, Woche-, Monat- und Jahresansichten bereit.

---

## 📦 Installation

### Option 1: Über HACS (Empfohlen)

1. Öffne **HACS** in Home Assistant.
2. Klicke oben rechts auf das Drei-Punkte-Menü `...` → **Benutzerdefinierte Repositories** (*Custom repositories*).
3. Füge die Repository-URL ein:
   ```text
   https://github.com/FranzFerdiand/ha-ikb-direkt
   ```
   Wähle als Kategorie: **Integration**.
4. Klicke auf **Hinzufügen** und installiere die Integration **IKB Direkt Smart Meter**.
5. Starte Home Assistant neu.

### Option 2: Manuelle Installation

1. Lade das Repository herunter oder klone es:
   ```bash
   git clone https://github.com/FranzFerdiand/ha-ikb-direkt.git
   ```
2. Kopiere den Ordner `custom_components/ikb_direkt` in dein Home Assistant Konfigurationsverzeichnis:
   ```text
   config/custom_components/ikb_direkt
   ```
3. Starte Home Assistant neu.

---

## ⚙️ Konfiguration

1. Gehe in Home Assistant zu **Einstellungen → Geräte & Dienste → Integration hinzufügen**.
2. Suche nach **IKB Direkt Smart Meter**.
3. Gib deine Zugangsdaten für das IKB-Portal (`direkt.ikb.at`) ein:
   * **E-Mail / Benutzername**
   * **Passwort**
   * **Historischer Erstimport (Tage):** Standardmäßig 35 Tage (kann von 1 bis 365 Tagen frei gewählt werden).
4. Die Integration erkennt deine Zählpunkte automatisch und richtet die Sensoren und Statistiken ein.
5. Nach einem Passwortwechsel fordert Home Assistant zur erneuten Authentifizierung auf.
6. Klicke anschließend auf **Konfigurieren**, um dein **Tarifmodell** und deine Strompreise für das Kosten-Tracking zu hinterlegen.

---

## 💻 Standalone CLI-Nutzung (Ohne Home Assistant)

Das beiliegende Skript `cli.py` kann auf jedem System mit Python 3.10+ autark ausgeführt werden:

### 1. Abhängigkeiten installieren
```bash
pip install -r requirements.txt
```

### 2. Login testen

Ohne `-p` wird das Passwort verdeckt abgefragt. Das vermeidet Passwörter in der Shell-Historie.
```bash
python cli.py test-login -u "deine-email@example.com" -p "dein-passwort"
```
*(Alternativ über Umgebungsvariablen: `export IKB_USERNAME="..."` und `export IKB_PASSWORD="..."`)*

### 3. Zählpunkte auflisten
```bash
python cli.py meters
```

### 4. Lastprofil abrufen und als CSV exportieren
```bash
python cli.py fetch --start 01.10.2026 --end 31.10.2026 --output verbrauch_oktober.csv
```

### 5. Lastprofil als JSON exportieren
```bash
python cli.py fetch --start 01.10.2026 --end 31.10.2026 --output verbrauch.json --format json
```

---

## 🔬 Technische Details

Die IKB (Innsbrucker Kommunalbetriebe AG) betreibt ihr Kundenportal unter `direkt.ikb.at` mit einem Keycloak-basierten Identity Provider (`sso.ikb.at`). Das Kundenportal lädt Lastgangdaten über einen internen Service-Endpunkt:

```http
GET /index.php?ajaxService=getLoadProfile&ajax=y&startDate=DD.MM.YYYY&endDate=DD.MM.YYYY&resolution=ResOpt1&periods=[{...}]
```

* `resolution=ResOpt1`: 15-Minuten-Intervall
* `resolution=ResOpt2`: Stündlich
* `resolution=ResOpt3`: Täglich

Die Antwort liefert ein JSON-Array mit Zeitintervallen (Unix-Timestamps) und kWh-Werten.

---

## 🔒 Datenschutz & Sicherheit

* **Lokale Zugangsdaten:** Home Assistant speichert das Passwort in seiner lokalen Config-Entry-JSON-Datei (`.storage/core.config_entries`), ohne eigene Passwortverschlüsselung. Konfigurationsdateien und Backups müssen entsprechend geschützt werden.
* **Keine Cloud-Dienste Dritter:** Die Kommunikation erfolgt direkt und ausschließlich zwischen deiner Home Assistant Instanz und dem Portal der IKB AG.
* **Git-Historie:** Im aktuellen Quelltext werden synthetische Beispielkennungen verwendet. Ältere Commits enthalten ehemals veröffentlichte Zählerkennungen; ein normaler Änderungs-PR entfernt diese nicht aus der Historie.

---

## 📄 Lizenz

Dieses Projekt ist unter der [MIT Lizenz](LICENSE) lizenziert.

## Entwicklung und Tests

```bash
pip install -r requirements.txt pytest pytest-asyncio
pytest
```

Die CI prüft den eigenständigen API-Client unter Python 3.12/3.13 und zusätzlich Home Assistant 2026.11.0 unter Python 3.14 mit einer echten SQLite-Recorder-Datenbank. Geprüft werden u. a. wiederholte Importe, Korrekturen, Kosten-Neuberechnung, Session-Ablauf, Reauthentifizierung und Zählerauswahl. Ohne installiertes Home Assistant werden die HA-spezifischen Tests übersprungen. Der Login wird mit synthetischen Antworten getestet; echte IKB-Zugangsdaten gehören nicht in Tests oder CI.
