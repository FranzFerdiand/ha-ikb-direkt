# Home Assistant IKB Direkt Smart Meter Integration

[![HACS Validation](https://github.com/FranzFerdiand/ha-ikb-direkt/actions/workflows/validate.yml/badge.svg)](https://github.com/FranzFerdiand/ha-ikb-direkt/actions/workflows/validate.yml)
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/default)


Home Assistant Integration und Python-CLI zum automatischen Abruf der Smart-Meter-Verbrauchsdaten (15-Minuten-Lastprofile) aus dem Kundenportal der **Innsbrucker Kommunalbetriebe AG (IKB)** ([direkt.ikb.at](https://direkt.ikb.at)).

Die Daten werden als offizielle **Home Assistant Long-Term Statistics** eingepflegt und stehen direkt im **Energy Dashboard** mit historischem 15-Minuten-Lastgang zur Verfügung.

---

## Funktionen

* ⚡ **15-Minuten-Auflösung:** Importiert den hochauflösenden Lastgang des Smart Meters.
* 📊 **Energy Dashboard Integration:** Schreibt direkt in die Home Assistant Recorder-Statistikdatenbank (`async_add_external_statistics`). Wählbar unter *Energie-Dashboard → Stromnetz → Netzverbrauch*.
* 🔄 **Automatischer Abgleich:** Periodischer Abruf (stündlich) neuer Daten vom Netzbetreiber.
* 🛡️ **Sichere Authentifizierung:** Unterstützt den IKB Keycloak OpenID Connect SSO-Flow ohne manuelle Session-Cookies.
* 💻 **Standalone CLI Tool:** Enthält ein eigenständiges Befehlszeilenwerkzeug (`cli.py`) zum Exportieren von Messdaten nach CSV oder JSON – auch ganz ohne Home Assistant nutzbar.
* 🔢 **Diagnose-Sensoren:**
  * *Verbrauch Gestern (kWh)*
  * *Letzter 15-Min-Wert (kWh)*
  * *Zeitpunkt letzter Messwert*
  * *Gesamter importierter Zählerstand (kWh)*

---

## Installation

### Option 1: Über HACS (Empfohlen)

1. Öffne **HACS** in Home Assistant.
2. Klicke oben rechts auf die drei Punkte `...` → **Benutzerdefinierte Repositories** (*Custom repositories*).
3. Füge die Repository-URL ein:
   ```text
   https://github.com/FranzFerdiand/ha-ikb-direkt
   ```
   Wähle als Kategorie: **Integration**.
4. Klicke auf **Hinzufügen** und installiere die Integration **IKB Direkt Smart Meter**.
5. Starte Home Assistant neu.

### Option 2: Manuelle Installation

1. Lade das Repository als ZIP herunter oder clone es:
   ```bash
   git clone https://github.com/FranzFerdiand/ha-ikb-direkt.git
   ```

2. Kopiere den Ordner `custom_components/ikb_direkt` in dein Home Assistant Konfigurationsverzeichnis unter:
   ```text
   config/custom_components/ikb_direkt
   ```
3. Starte Home Assistant neu.

---

## Konfiguration

1. Gehe in Home Assistant zu **Einstellungen → Geräte & Dienste → Integration hinzufügen**.
2. Suche nach **IKB Direkt Smart Meter**.
3. Gib deine Zugangsdaten für das IKB-Portal (`direkt.ikb.at`) ein:
   * **E-Mail / Benutzername**
   * **Passwort**
   * **Historischer Erstimport (Tage):** Standardmäßig 30 Tage (kann bis zu 365 Tage betragen).
4. Die Integration erkennt deine Zählpunkte automatisch und richtet die Sensoren ein.

---

## Energy Dashboard einrichten

1. Gehe zu **Einstellungen → Dashboards → Energie**.
2. Klicke bei **Stromnetz** auf **Verbrauch hinzufügen** (*Add consumption*).
3. Wähle im Dropdown-Menü deine IKB-Statistik aus (z. B. `IKB Direkt [Zählernummer] (Verbrauch)` bzw. `ikb_direkt:...`).
4. Klicke auf **Speichern**. Die historischen 15-Minuten-Verbrauchsdaten erscheinen im Energy Dashboard.

---

## Standalone CLI-Nutzung (Ohne Home Assistant)

Das beiliegende Skript `cli.py` kann auf jedem Rechner mit Python 3.10+ ausgeführt werden:

### 1. Abhängigkeiten installieren
```bash
pip install -r requirements.txt
```

### 2. Login testen
```bash
python cli.py test-login -u "deine-email@example.com" -p "dein-passwort"
```
*(Alternativ können Umgebungsvariablen gesetzt werden: `export IKB_USERNAME="..."` und `export IKB_PASSWORD="..."`)*

### 3. Zählpunkte auflisten
```bash
python cli.py meters
```

### 4. Lastprofil abrufen und als CSV exportieren
```bash
python cli.py fetch --start 01.10.2026 --end 31.10.2026 --output verbrauch_oktober.csv
```

### 5. Als JSON exportieren
```bash
python cli.py fetch --start 01.10.2026 --end 31.10.2026 --output verbrauch.json --format json
```

---

## Technische Details

Die IKB (Innsbrucker Kommunalbetriebe AG) betreibt ihr Kundenportal unter `direkt.ikb.at` mit einem Keycloak-basierten Identity Provider (`sso.ikb.at`). Das Kundenportal lädt Lastgangdaten über einen internen Endpunkt:

```http
GET /index.php?ajaxService=getLoadProfile&ajax=y&startDate=DD.MM.YYYY&endDate=DD.MM.YYYY&resolution=ResOpt1&periods=[{...}]
```

* `resolution=ResOpt1`: 15-Minuten-Intervall
* `resolution=ResOpt2`: Stündlich
* `resolution=ResOpt3`: Täglich

Die Antwort liefert ein JSON-Array mit Zeitintervallen (Unix-Timestamps) und kWh-Werten.

---

## Datenschutz & Sicherheit

* Zugangsdaten werden ausschließlich lokal in der Home Assistant Konfiguration (verschlüsselt im Storage) gespeichert.
* Es werden keine Daten an Dritte übermittelt.
* Keine sensiblen Kundendaten oder Passwörter sind im Quellcode hinterlegt.

---

## Lizenz

Dieses Projekt ist unter der [MIT Lizenz](LICENSE) lizenziert.
