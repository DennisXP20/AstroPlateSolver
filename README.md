# Astro Plate Solver

Ein lokaler astrometrischer Plate Solver mit Browser-Oberfläche: Bild laden,
solven, und das Tool identifiziert alle Katalogobjekte im Feld — von Sternen
über Galaxien und Quasare bis zu Galaxienhaufen — mit umfangreicher
wissenschaftlicher Auswertung. Läuft komplett lokal; nach dem einmaligen
Katalog-Download funktioniert die Objekterkennung offline.

## Features

**Astrometrie & Erkennung**
- Plate Solving via ASTAP (oder vorhandener FITS-WCS-Header)
- Objektabgleich gegen lokale Kataloge: Messier (vollständig, 110 Objekte mit
  Detaildaten), NGC/IC, Caldwell, PGC/HyperLEDA (~1 Mio. Galaxien inkl.
  Morphologie-Typ), Tycho-2, Gaia DR3, SDSS DR16Q-Quasare (750.000+ mit
  GALEX-UV/WISE-IR-Photometrie), Abell/WHL/redMaPPer-Galaxienhaufen
- SDSS DR17-Online-Abfrage (Galaxien/Sterne bis mag ~22 mit ugriz + Photo-z)
- Asteroiden-Suche im Feld via SkyBoT (IMCCE)
- Interaktive Bildansicht: Pan/Zoom, Labels, Suche, Filter, Messwerkzeug,
  Minimap, Koordinatenanzeige (selbstkalibrierend über Katalog-Positionen)

**Wissenschaftliche Auswertung**
- Aperturphotometrie mit Zeropoint-Kalibrierung
- Photometrische Rotverschiebung (SDSS-Katalog-Photo-z + SED-Template-Fitting)
- Hertzsprung-Russell-Diagramm mit Gaia-DR3-Online-Abfrage (BP/RP + Parallaxe)
- Statistik-Suite: Entfernungsverteilung, Hubble-Diagramm, N(z),
  Vollständigkeitsanalyse, Farb-Farb-Diagramm, Galaxien-Bimodalität (u−r),
  absolute Magnituden, Lichtalter-Verteilung, räumliches Clustering
  (Clark-Evans), galaktischer Kontext, abgetastetes komovierendes Volumen
- Quasar-Physik: bolometrische Leuchtkraft, Eddington-Mindestmasse des
  Schwarzen Lochs, echte Virialmassen via Shen et al. (2011)
- Hubble-Sequenz-Klassifikation (Katalog + bildbasierte CAS-Schätzung)
- Gravitationslinsen-Kandidaten-Suche um massereiche Haufen
- Zwerggalaxien-Kandidatenliste, 3D-Universum-Ansicht

**Sammeln & Exportieren**
- „Mein Katalog": persistenter, deduplizierter DSO-Katalog über alle Solves
  (IndexedDB) mit photometrischer Sichtbarkeitsprüfung und
  Beobachtungsatlas (Mollweide-Himmelskarte der eigenen Abdeckung)
- Exporte: annotiertes Bild, JSON, CSV, Word-Bericht, annotiertes FITS mit
  WCS-Headern (für Siril/DS9/TOPCAT), 3D-Karte, interaktiver
  Standalone-HTML-Viewer (eine Datei, ohne Software teilbar)

## Systemvoraussetzungen

- **Windows 10/11** (primär getestet; Linux/macOS: Server läuft mit
  `python server.py`, nur die .bat-Skripte sind Windows-spezifisch)
- **Python 3.10+** — bei der Installation *„Add Python to PATH"* anhaken
- **ASTAP** (empfohlen): kostenlos von <https://www.hnsky.org/astap.htm>,
  dazu mindestens einen Sternkatalog in den ASTAP-Ordner:
  - **D80** — Allround, wide-field-tauglich
  - **H17/H18** — tief (mag 17/18), nötig für schmale Felder (< 1°)
- **Festplatte**: ~1,5 GB für die lokalen Kataloge (catalog.db + Rohdaten)
- **RAM**: 4 GB+, mehr für große FITS/TIFF-Dateien (1–2 GB Bilder werden
  unterstützt)

## Installation

1. **Python installieren** (falls nicht vorhanden):
   <https://www.python.org/downloads/> — *„Add Python to PATH"* ankreuzen.
2. **`install.bat` doppelklicken** — installiert die Python-Pakete
   (`numpy`, `scipy`, `astropy`, `pillow`, `certifi`, `python-docx`).
   Manuell: `python -m pip install numpy scipy astropy pillow certifi python-docx`
3. **ASTAP installieren** (empfohlen) + Sternkatalog (D80 oder H17/H18)
   in den `astap.exe`-Ordner.
4. **`start.bat` doppelklicken** — Server startet auf
   <http://localhost:8743>, der Browser öffnet sich automatisch.
5. **Beim ersten Start**: den Katalog-Download in der Oberfläche starten.
   Der Downloader holt alle Kataloge automatisch — inklusive des
   vollständigen PGC/HyperLEDA (~80 MB) und aller SDSS-DR16Q-Quasare
   (~100–350 MB). Je nach Verbindung 10–40 Minuten, einmalig.

## Schnellstart

1. Bild per Drag-and-Drop laden (JPEG, PNG, TIFF, FITS — auch 1–2 GB)
2. *Plate Solve* klicken (ASTAP: 20 s – 3 min je nach Bildgröße)
3. Objekte erscheinen im Bild — anklicken für Details, SIMBAD/NED/Aladin-Links
4. Tabs: *Statistik* (wissenschaftliche Auswertung), *Fotometrie*, *3D*
5. Toolbar: 📊 öffnet „Mein Katalog" mit allen je gesolvten Objekten

## Dateistruktur

```
server.py            HTTP-Server und API-Endpunkte (bindet nur localhost)
solver.py            Plate-Solver, WCS, Sternextraktion, Katalogabgleich
catalog_dl.py        Katalog-Downloader (VizieR/ESA/SIMBAD, alle Mirror)
import_catalogs.py   CSV-Parser für lokale Katalogdateien
sdss_query.py        SDSS-DR17-Online-Abfrage mit Cache
photometry.py        Aperturphotometrie und Zeropoint-Kalibrierung
galaxy_morph.py      SIMBAD-Morphologie-Lookup
morph_estimate.py    Bildbasierte Morphologie-Schätzung (CAS)
export_docx.py       Word-Berichtserstellung
index.html           Web-Oberfläche (Single-Page-App)
catalog.db           Lokale Objektdatenbank (wird beim Download erzeugt)
pgc.csv, quasars.csv Katalog-Rohdaten (werden automatisch heruntergeladen)
install.bat          Abhängigkeiten installieren
start.bat            Server starten
```

## Fehlersuche

- **„ASTAP nicht gefunden"** — ASTAP installieren oder Pfad im Solve-Dialog
  eintragen (typisch `C:\Program Files\astap\astap.exe`).
- **Solving läuft endlos** — meist fehlen die ASTAP-Sternkataloge (D80/H17)
  im `astap.exe`-Ordner.
- **Port 8743 belegt** — `start.bat` beendet alte Instanzen automatisch;
  sonst `PORT` in `server.py` ändern.
- **Katalog-Download bricht ab** — einfach erneut starten; bereits geladene
  Kataloge werden übersprungen. Der Downloader probiert mehrere
  VizieR-Mirror durch.
- **Photometrie „zu wenig Referenzsterne"** — mag-Limit anheben (17–19) und
  Gaia DR3 zusätzlich aktivieren.

## Wissenschaftlicher Hinweis

Katalogdaten (Positionen, Magnituden, Rotverschiebungen, Parallaxen,
Schwarzloch-Massen) stammen unverändert aus professionellen Quellen und sind
so verlässlich wie deren Originale — Fehlzuordnungen beim Positionsabgleich
sind selten, aber möglich. **Eigene Berechnungen des Tools** (SED-Photo-z,
Lensing-Score, bildbasierte Morphologie, Eddington-Abschätzungen,
Sichtbarkeitsprüfung) sind explorative Schätzungen mit dokumentierten
Einschränkungen (siehe ▸?-Info-Boxen in der App) und ersetzen keine
begutachtete Analyse. Für belastbare Ergebnisse: Gegenprobe über die
eingebauten SIMBAD/NED-Links, FITS-Export nach DS9/TOPCAT, Kosmologie über
Ned Wrights CosmoCalc.

## Daten-Quellen und Danksagungen

Diese Software nutzt Daten der folgenden Dienste und Kataloge. Bei
Weiterverwendung der Daten gelten deren Bedingungen:

- **CDS, Straßburg**: VizieR-Katalogdienst und SIMBAD-Datenbank
  (Wenger et al. 2000)
- **SDSS** (Sloan Digital Sky Survey): DR17-Photometrie und -Spektroskopie,
  DR16Q-Quasarkatalog (Lyke et al. 2020) — <https://www.sdss.org>
- **ESA Gaia** DR3 (Gaia Collaboration 2023), Daten CC BY-SA 3.0 IGO —
  <https://www.cosmos.esa.int/gaia>
- **HyperLEDA** / PGC (Paturel et al. 2003)
- **OpenNGC** (NGC/IC-Daten)
- **Shen et al. 2011** (ApJS 194, 45): Quasar-Virialmassen
- **IMCCE SkyBoT** (Berthier et al. 2006): Asteroiden-Ephemeriden
- **ASTAP** (Han Kleijn): astrometrische Lösung — separate Software,
  <https://www.hnsky.org/astap.htm>

Die Software selbst ist ein Hobby-Projekt ohne Gewährleistung. Der Server
bindet ausschließlich an `localhost` — außer den Katalog-Abfragen an die
genannten Dienste verlassen keine Daten den Rechner.
