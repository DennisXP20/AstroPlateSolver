"""
SDSS DR17 – Feldabfrage mit lokalem HEALPix-Tile-Cache
=======================================================

Architektur
-----------
  Schicht 1 – SDSS SkyServer TAP (live)
      Fragt den SDSS CasJobs / SkyServer für einen konkreten Himmelsausschnitt
      ab. Gibt JEDEN PhotoObj-Eintrag zurück bis zur eingestellten Magnitude.
      SDSS-Grenzgröße: r ≈ 22.2 mag (tiefer Stack bis 22.5 in manchen Feldern).

  Schicht 2 – Lokaler HEALPix-Tile-Cache (sdss_cache.db)
      HEALPix Nside=64 → ~55.000 Tiles à 0.84 deg² (≈ 58' × 58')
      Bereits abgefragte Tiles werden lokal gecacht und nicht erneut abgefragt.
      Wächst organisch: nur was du tatsächlich beobachtet hast.

Integration in solve_field()
-----------------------------
  query_sdss_for_field(ra, dec, radius_deg, mag_limit)
      Gibt eine Liste von Objekt-Dicts zurück, die 1:1 zu den Einträgen in
      solver.query_region() kompatibel sind (id, catalog, ra, dec,
      magnitude, type, name, description).

      Unbekannte Punkte (Pixel-Quellen ohne irgendeinen Katalog-Match) werden
      in solver.solve_field() bereits als "unknown" geflaggt – dieser Mechanismus
      greift dann auch für SDSS-Lücken.

Magnitude-Steuerung (Nutzer)
-----------------------------
  Der Nutzer stellt die Magnitude über den bestehenden mag_limit-Slider in
  index.html ein. sdss_query nimmt diesen Wert als `mag_limit` entgegen.
  Typische Sinnwerte:
    17.0  →  Nur relativ helle Objekte   (~wenige Tausend pro Grad²)
    20.0  →  Tief, aber noch handhabbar  (~10.000–50.000 pro Grad²)
    22.2  →  SDSS-Volltiefe              (~100.000+ pro Grad²)

  Die Abfrage begrenzt außerdem die Anzahl der zurückgegebenen Objekte
  pro Tile auf MAX_PER_TILE (Standard: 50.000), um den Browser nicht zu
  überfordern.  Wenn du 22.2 mag mit 50.000-Limit kombiniertst, bekommst du
  eine repräsentative Stichprobe tiefer Felder; für helle Quellen (< 19 mag)
  ist das Limit ohnehin nie relevant.

Abhängigkeiten
--------------
  Nur Python-Stdlib + (optional) numpy für die HEALPix-Näherung.
  Kein astropy-HEALPix benötigt – wir implementieren die Tile-ID-Berechnung
  direkt (Ring-Schema, Nside=64, ausreichend für 58'-Tiles).
"""

import math
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import csv
import io
import json
import threading
from pathlib import Path
from typing import List, Dict, Optional, Tuple

# ──────────────────────────────────────────────────────────────────────────────
# Konfiguration
# ──────────────────────────────────────────────────────────────────────────────

CACHE_DB   = Path(__file__).parent / "sdss_cache.db"
NSIDE      = 64          # HEALPix-Auflösung: ~0.84 deg² pro Tile (≈ 58')
MAX_PER_TILE = 50_000    # Max. Objekte pro Tile-Download (verhindert Browser-Überlastung)
TILE_TTL_DAYS = 30       # Wie lange ein gecachter Tile als frisch gilt

# SDSS SkyServer TAP-Endpunkte (primär + Fallback)
_SDSS_TAP = [
    "https://skyserver.sdss.org/dr17/SkyServerWS/SearchTools/SqlSearch",
    # Hinweis: cas.sdss.org/.../x_sql.aspx wurde von SDSS eingestellt (404) und
    # ist als Endpunkt entfernt. Wenn der SkyServer nicht erreichbar ist, greift
    # stattdessen der VizieR-Spiegel (V/154, SDSS DR16) — siehe
    # _fetch_tile_from_vizier().
]

# VizieR-TAP als unabhängiger Spiegel (CDS Straßburg): Katalog V/154 = SDSS DR16.
# Photometrie identisch zu DR17; es fehlen nur die jüngsten eBOSS-Spektren.
_VIZIER_TAP = "https://tapvizier.cds.unistra.fr/TAPVizieR/tap/sync"

# Tabellenspalten die wir aus PhotoObj holen
# objID, ra, dec, r (Petrosian r-Band, am stabilsten), type (3=Galaxie, 6=Stern),
# psfMag_r (PSF-Magnitude für Punkt-Quellen), petroMag_r (für ausgedehnte Objekte)
_SDSS_COLS = "objID,ra,dec,type,psfMag_r,petroMag_r,u,g,r,i,z"

_USER_AGENT = "AstroPlateSolver/1.0 (SDSS DR17 cone search)"

# Schreib-Lock für die Cache-DB (mehrere Threads möglich bei Batch)
_db_lock = threading.Lock()


# ──────────────────────────────────────────────────────────────────────────────
# Einfaches Himmels-Tiling (Grad-Raster statt HEALPix)
# Tile-Größe: TILE_DEG × TILE_DEG Grad. Einfach, robust, kein math.sqrt-Problem.
# Tile-ID: eindeutige Ganzzahl aus (ra_bin, dec_bin).
# ──────────────────────────────────────────────────────────────────────────────

TILE_DEG = 0.9   # Tile-Größe in Grad (~54 Bogenminuten, ähnlich HEALPix Nside=64)

def _tile_id(ra_deg: float, dec_deg: float) -> int:
    """Gibt eine eindeutige Tile-ID für (ra, dec) zurück."""
    ra_bin  = int((ra_deg % 360.0) / TILE_DEG)
    dec_bin = int((dec_deg + 90.0) / TILE_DEG)
    return dec_bin * 400 + ra_bin   # 400 > 360/0.9 = 400, also eindeutig

def _tile_center(tile_id: int) -> Tuple[float, float]:
    """Gibt Mittelpunkt eines Tiles zurück."""
    dec_bin = tile_id // 400
    ra_bin  = tile_id % 400
    ra  = (ra_bin  + 0.5) * TILE_DEG
    dec = (dec_bin + 0.5) * TILE_DEG - 90.0
    return ra % 360.0, max(-89.9, min(89.9, dec))

def _tile_ids_for_cone(ra_deg: float, dec_deg: float, radius_deg: float) -> List[int]:
    """Gibt alle Tile-IDs zurück, die einen Kegel überlappen."""
    seen = set()
    cos_dec = max(math.cos(math.radians(dec_deg)), 0.017)

    dec_min = max(dec_deg - radius_deg, -89.9)
    dec_max = min(dec_deg + radius_deg,  89.9)

    d_dec = dec_min
    while d_dec <= dec_max + TILE_DEG:
        # RA-Ausdehnung breiter wegen Kosinus-Verkleinerung am Pol
        cos_d = max(math.cos(math.radians(min(max(d_dec, -89.9), 89.9))), 0.017)
        ra_span = radius_deg / cos_d + TILE_DEG
        ra = ra_deg - ra_span
        while ra <= ra_deg + ra_span + TILE_DEG:
            seen.add(_tile_id(ra % 360.0, d_dec))
            ra += TILE_DEG * 0.85
        d_dec += TILE_DEG * 0.85

    return list(seen)


def _angular_sep_deg(ra1, dec1, ra2, dec2) -> float:
    """Winkelabstand zweier Himmelspunkte in Grad (Haversine)."""
    r1, d1, r2, d2 = map(math.radians, [ra1, dec1, ra2, dec2])
    a = (math.sin((d2 - d1) / 2) ** 2
         + math.cos(d1) * math.cos(d2) * math.sin((r2 - r1) / 2) ** 2)
    return math.degrees(2 * math.asin(min(1.0, math.sqrt(max(0.0, a)))))


# ──────────────────────────────────────────────────────────────────────────────
# Cache-DB
# ──────────────────────────────────────────────────────────────────────────────

def _cache_conn() -> sqlite3.Connection:
    """Öffnet (und initialisiert bei Bedarf) die SDSS-Cache-Datenbank."""
    conn = sqlite3.connect(str(CACHE_DB), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sdss_objects (
            obj_id   TEXT PRIMARY KEY,
            tile_id  INTEGER NOT NULL,
            ra       REAL NOT NULL,
            dec      REAL NOT NULL,
            mag_r    REAL,
            mag_u    REAL,
            mag_g    REAL,
            mag_i    REAL,
            mag_z    REAL,
            obj_type INTEGER,
            specz    REAL,
            specz_err REAL,
            spec_class TEXT,
            ts       INTEGER DEFAULT (strftime('%s','now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sdss_tiles (
            tile_id   INTEGER PRIMARY KEY,
            mag_limit REAL NOT NULL,   -- tiefste abgefragte Magnitude für diesen Tile
            n_objects INTEGER NOT NULL,
            queried_at INTEGER NOT NULL,
            nside     INTEGER NOT NULL DEFAULT 64
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_tile ON sdss_objects(tile_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_radec ON sdss_objects(ra, dec)")
    # Migration: Rotverschiebungs-Spalten nachrüsten falls DB aus alter Version stammt
    for col, typ in [("specz","REAL"),("specz_err","REAL"),("spec_class","TEXT"),
                     ("photoz","REAL"),("photoz_err","REAL")]:
        try:
            conn.execute(f"ALTER TABLE sdss_objects ADD COLUMN {col} {typ}")
        except Exception:
            pass
    conn.commit()
    return conn


def _tile_is_cached(conn: sqlite3.Connection,
                    tile_id: int, mag_limit: float) -> bool:
    row = conn.execute(
        "SELECT mag_limit, queried_at FROM sdss_tiles WHERE tile_id=?",
        (tile_id,)
    ).fetchone()
    if row is None:
        return False
    cached_mag, queried_at = row
    age_days = (time.time() - queried_at) / 86400.0
    if cached_mag < mag_limit or age_days >= TILE_TTL_DAYS:
        return False
    # Prüfe ob Tile noch aus alter Version ohne specz-Daten stammt
    # (alle specz NULL → Tile neu laden damit Rotverschiebung verfügbar wird)
    has_specz = conn.execute(
        "SELECT 1 FROM sdss_objects WHERE tile_id=? AND specz IS NOT NULL LIMIT 1",
        (tile_id,)
    ).fetchone()
    n_objs = conn.execute(
        "SELECT COUNT(*) FROM sdss_objects WHERE tile_id=?", (tile_id,)
    ).fetchone()[0]
    # Wenn Tile Objekte hat aber keines davon specz hat → veraltet
    # Ausnahme: echte Felder ohne Spektren (dann ist das OK)
    # Heuristik: wenn > 10 Objekte aber kein einziges specz → re-download
    if n_objs > 10 and not has_specz:
        return False
    return True


def _mark_tile_cached(conn: sqlite3.Connection,
                      tile_id: int, mag_limit: float, n_objects: int):
    conn.execute("""
        INSERT OR REPLACE INTO sdss_tiles (tile_id, mag_limit, n_objects, queried_at, nside)
        VALUES (?, ?, ?, ?, ?)
    """, (tile_id, mag_limit, n_objects, int(time.time()), 0))
    conn.commit()


def _insert_objects(conn: sqlite3.Connection, objects: List[Dict]):
    """Fügt SDSS-Objekte in den Cache ein (schnell, bulk)."""
    if not objects:
        return
    # Redshift-Spalten nachrüsten falls DB alt ist (Migration)
    try:
        conn.execute("ALTER TABLE sdss_objects ADD COLUMN specz REAL")
        conn.execute("ALTER TABLE sdss_objects ADD COLUMN specz_err REAL")
        conn.execute("ALTER TABLE sdss_objects ADD COLUMN spec_class TEXT")
        conn.commit()
    except Exception:
        pass  # Spalten existieren bereits

    conn.executemany("""
        INSERT OR IGNORE INTO sdss_objects
            (obj_id, tile_id, ra, dec, mag_r, mag_u, mag_g, mag_i, mag_z,
             obj_type, specz, specz_err, spec_class, photoz, photoz_err)
        VALUES
            (:obj_id, :tile_id, :ra, :dec, :mag_r, :mag_u, :mag_g, :mag_i, :mag_z,
             :obj_type, :specz, :specz_err, :spec_class, :photoz, :photoz_err)
    """, objects)
    conn.commit()


def _query_cache(conn: sqlite3.Connection,
                 ra: float, dec: float,
                 radius_deg: float,
                 mag_limit: float) -> List[Dict]:
    """Liest Objekte aus dem lokalen Cache für einen Kegel."""
    cos_dec = max(math.cos(math.radians(dec)), 0.017)
    d_ra    = radius_deg / cos_dec
    dec_min = max(dec - radius_deg, -90.0)
    dec_max = min(dec + radius_deg,  90.0)
    ra_min  = (ra - d_ra) % 360.0
    ra_max  = (ra + d_ra) % 360.0

    if ra_min <= ra_max:
        sql = ("SELECT obj_id,ra,dec,mag_r,mag_u,mag_g,mag_i,mag_z,obj_type "
               "FROM sdss_objects "
               "WHERE ra BETWEEN ? AND ? AND dec BETWEEN ? AND ? "
               "AND (mag_r IS NULL OR mag_r <= ?)")
        params = [ra_min, ra_max, dec_min, dec_max, mag_limit]
    else:
        sql = ("SELECT obj_id,ra,dec,mag_r,mag_u,mag_g,mag_i,mag_z,obj_type "
               "FROM sdss_objects "
               "WHERE (ra >= ? OR ra <= ?) AND dec BETWEEN ? AND ? "
               "AND (mag_r IS NULL OR mag_r <= ?)")
        params = [ra_min, ra_max, dec_min, dec_max, mag_limit]

    if ra_min <= ra_max:
        sql = ("SELECT obj_id,ra,dec,mag_r,mag_u,mag_g,mag_i,mag_z,obj_type,"
               "specz,specz_err,spec_class,photoz,photoz_err "
               "FROM sdss_objects "
               "WHERE ra BETWEEN ? AND ? AND dec BETWEEN ? AND ? "
               "AND (mag_r IS NULL OR mag_r <= ?)")
        params = [ra_min, ra_max, dec_min, dec_max, mag_limit]
    else:
        sql = ("SELECT obj_id,ra,dec,mag_r,mag_u,mag_g,mag_i,mag_z,obj_type,"
               "specz,specz_err,spec_class,photoz,photoz_err "
               "FROM sdss_objects "
               "WHERE (ra >= ? OR ra <= ?) AND dec BETWEEN ? AND ? "
               "AND (mag_r IS NULL OR mag_r <= ?)")
        params = [ra_min, ra_max, dec_min, dec_max, mag_limit]

    rows = conn.execute(sql, params).fetchall()
    result = []
    for row in rows:
        obj_id, ra_o, dec_o, mr, mu, mg, mi, mz, otype = row[:9]
        specz      = row[9]  if len(row) > 9  else None
        specz_err  = row[10] if len(row) > 10 else None
        spec_class = row[11] if len(row) > 11 else None
        photoz     = row[12] if len(row) > 12 else None
        photoz_err = row[13] if len(row) > 13 else None
        sep = _angular_sep_deg(ra, dec, ra_o, dec_o)
        if sep <= radius_deg:
            result.append({
                "obj_id": obj_id, "ra": ra_o, "dec": dec_o,
                "mag_r": mr, "mag_u": mu, "mag_g": mg, "mag_i": mi, "mag_z": mz,
                "obj_type": otype,
                "specz": specz, "specz_err": specz_err, "spec_class": spec_class,
                "photoz": photoz, "photoz_err": photoz_err,
            })
    return result


# ──────────────────────────────────────────────────────────────────────────────
# SDSS SkyServer TAP-Abfrage
# ──────────────────────────────────────────────────────────────────────────────

def _sdss_sql_query(sql: str, timeout: int = 45) -> str:
    """
    Führt eine SQL-Abfrage gegen den SDSS SkyServer aus.
    Probiert mehrere Endpunkte. Gibt CSV-Text zurück oder wirft RuntimeError.

    timeout gilt PRO Endpunkt-Versuch.
    """
    errors = []
    for base_url in _SDSS_TAP:
        try:
            params = urllib.parse.urlencode({"cmd": sql.strip(), "format": "csv"})
            url    = base_url + "?" + params
            req    = urllib.request.Request(
                url, headers={
                    "User-Agent": _USER_AGENT,
                    "Accept":     "text/csv,text/plain,*/*",
                    "Referer":    "https://skyserver.sdss.org/",
                }
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")

            stripped = raw.strip()
            # SDSS-Fehlerformate erkennen
            if stripped.startswith("#Error") or stripped.upper().startswith("ERROR"):
                errors.append(f"{base_url}: SDSS-Fehler – {stripped[:300]}")
                continue
            if "<html" in stripped[:200].lower():
                errors.append(f"{base_url}: HTML statt CSV ({len(stripped)} Bytes)")
                continue
            # Leere Antwort (nur Header oder wirklich leer) ist OK – kein Fehler
            return raw

        except urllib.error.HTTPError as e:
            errors.append(f"{base_url}: HTTP {e.code} {e.reason}")
        except urllib.error.URLError as e:
            errors.append(f"{base_url}: URL-Fehler {e.reason}")
        except Exception as e:
            errors.append(f"{base_url}: {type(e).__name__}: {e}")

    raise RuntimeError("SDSS nicht erreichbar: " + " | ".join(errors))


def _build_sdss_sql(ra_c: float, dec_c: float, radius_arcmin: float,
                    mag_limit: float, strategy: int = 0) -> str:
    safe_mag = min(float(mag_limit), 22.5)
    r_deg    = radius_arcmin / 60.0
    ra_min   = ra_c - r_deg
    ra_max   = ra_c + r_deg
    dec_min  = dec_c - r_deg
    dec_max  = dec_c + r_deg

    if strategy == 0:
        return f"""SELECT TOP {MAX_PER_TILE}
    p.objID, p.ra, p.dec, p.type,
    p.psfMag_r, p.petroMag_r,
    p.modelMag_u AS u,
    p.modelMag_g AS g,
    p.modelMag_r AS r,
    p.modelMag_i AS i,
    p.modelMag_z AS z,
    pz.z         AS photoz,
    pz.zerr      AS photoz_err,
    s.z          AS specz,
    s.zErr       AS specz_err,
    s.class      AS spec_class
FROM PhotoObj p
LEFT JOIN Photoz  pz ON p.objID = pz.objID
LEFT JOIN SpecObj s  ON p.specObjID = s.specObjID
WHERE
    p.ra  BETWEEN {ra_min:.6f} AND {ra_max:.6f}
    AND p.dec BETWEEN {dec_min:.6f} AND {dec_max:.6f}
    AND p.mode = 1
    AND p.psfMag_r BETWEEN 1.0 AND {safe_mag:.2f}
    AND p.clean = 1"""

    elif strategy == 1:
        return f"""SELECT TOP {MAX_PER_TILE}
    p.objID, p.ra, p.dec, p.type,
    p.psfMag_r, p.petroMag_r,
    p.modelMag_u AS u,
    p.modelMag_g AS g,
    p.modelMag_r AS r,
    p.modelMag_i AS i,
    p.modelMag_z AS z,
    pz.z         AS photoz,
    pz.zerr      AS photoz_err,
    s.z          AS specz,
    s.zErr       AS specz_err,
    s.class      AS spec_class
FROM fGetNearbyObjEq({ra_c:.6f}, {dec_c:.6f}, {radius_arcmin:.3f}) AS n
JOIN PhotoObj AS p ON n.objID = p.objID
LEFT JOIN Photoz  pz ON p.objID = pz.objID
LEFT JOIN SpecObj s  ON p.specObjID = s.specObjID
WHERE p.mode = 1
    AND p.psfMag_r BETWEEN 1.0 AND {safe_mag:.2f}"""

    else:
        return f"""SELECT TOP {MAX_PER_TILE}
    p.objID, p.ra, p.dec, p.type,
    p.psfMag_r, p.petroMag_r,
    p.modelMag_u AS u,
    p.modelMag_g AS g,
    p.modelMag_r AS r,
    p.modelMag_i AS i,
    p.modelMag_z AS z,
    pz.z         AS photoz,
    pz.zerr      AS photoz_err,
    s.z          AS specz,
    s.zErr       AS specz_err,
    s.class      AS spec_class
FROM PhotoPrimary p
LEFT JOIN Photoz  pz ON p.objID = pz.objID
LEFT JOIN SpecObj s  ON p.specObjID = s.specObjID
WHERE
    p.ra  BETWEEN {ra_min:.6f} AND {ra_max:.6f}
    AND p.dec BETWEEN {dec_min:.6f} AND {dec_max:.6f}
    AND p.psfMag_r BETWEEN 1.0 AND {safe_mag:.2f}"""


def _fetch_tile_from_sdss(tile_id: int,
                           mag_limit: float,
                           progress_cb=None) -> List[Dict]:
    """
    Lädt alle SDSS DR17 PhotoObj für einen Tile.
    Probiert mehrere SQL-Strategien und Endpunkte der Reihe nach.

    Timeout-Strategie: 20s pro Versuch (statt 90s) – SDSS antwortet bei
    funktionierender Verbindung in 2-10s. Bei einem reinen Netzwerkfehler
    (TimeoutError, nicht erreichbar) werden NICHT alle 3 Strategien probiert,
    da das Problem auf Verbindungsebene liegt und sich durch eine andere
    SQL-Strategie nicht löst – das hätte sonst bis zu 3×2×20s = 120s pro
    Tile gekostet, bei vielen Tiles im Bildfeld ein spürbares Einfrieren.
    """
    def prog(m):
        if progress_cb: progress_cb(m)

    ra_c, dec_c = _tile_center(tile_id)
    radius_arcmin = TILE_DEG * 60 * 0.75

    prog(f"[SDSS] Tile {tile_id} (RA={ra_c:.2f} Dec={dec_c:.2f}) mag<={mag_limit:.1f} ...")

    TILE_TIMEOUT = 45  # Sekunden pro Endpunkt-Versuch (auf Nutzerwunsch erhöht)
    had_network_failure = False

    for strategy in [0, 1, 2]:
        sql = _build_sdss_sql(ra_c, dec_c, radius_arcmin, mag_limit, strategy)
        try:
            raw = _sdss_sql_query(sql, timeout=TILE_TIMEOUT)
        except RuntimeError as e:
            had_network_failure = True
            prog(f"[SDSS] Tile {tile_id} Strategie {strategy} Netzwerkfehler: {e}")
            # Nach dem ERSTEN Netzwerkfehler (alle Endpunkte nicht erreichbar)
            # macht es keinen Sinn, weitere Strategien zu probieren – das
            # Problem liegt nicht an der SQL-Syntax sondern an der Verbindung.
            if "nicht erreichbar" in str(e) or "TimeoutError" in str(e):
                prog(f"[SDSS] Tile {tile_id}: Verbindungsproblem, überspringe "
                     f"verbleibende Strategien (spart Zeit)")
                break
            continue

        objects = _parse_sdss_csv(raw, tile_id)

        if objects:
            prog(f"[SDSS] Tile {tile_id}: {len(objects)} Objekte (Strategie {strategy})")
            return objects

        if len(raw.strip()) < 50:
            prog(f"[SDSS] Tile {tile_id} Strategie {strategy}: leere/ungültige Antwort")
            continue

        prog(f"[SDSS] Tile {tile_id}: 0 Objekte (Strategie {strategy}, Antwort {len(raw)} Bytes)")
        return []

    if had_network_failure:
        # SkyServer nicht erreichbar (Wartung/Rate-Limit kommt regelmäßig vor) →
        # unabhängiger Spiegel: VizieR V/154 (SDSS DR16, CDS Straßburg)
        prog(f"[SDSS] Tile {tile_id}: SkyServer nicht erreichbar – "
             f"versuche VizieR-Spiegel (SDSS DR16)...")
        try:
            objs = _fetch_tile_from_vizier(tile_id, mag_limit, progress_cb)
            if objs is not None:
                prog(f"[SDSS] Tile {tile_id}: {len(objs)} Objekte via VizieR (DR16-Spiegel)")
                return objs
        except Exception as e:
            prog(f"[SDSS] Tile {tile_id}: VizieR-Fallback fehlgeschlagen: {str(e)[:200]}")
        prog(f"[SDSS] Tile {tile_id}: konnte weder SDSS noch VizieR erreichen "
             f"(Verbindungsproblem, nicht zwingend leeres Tile)")
        return None  # Signalisiert: NICHT cachen, war kein echtes "leer"

    prog(f"[SDSS] Tile {tile_id}: 0 Objekte (alle SQL-Strategien erschöpft)")
    return []


def _parse_sdss_csv(raw: str, tile_id: int) -> List[Dict]:
    """
    Parst SDSS CSV-Antwort robust.
    Behandelt: Kommentarzeilen mit '#', BOM, Leerzeilen, Lowercase-Spaltennamen.
    """
    # BOM entfernen
    raw = raw.lstrip("\ufeff\ufffe")

    # Zeilen filtern: Kommentare (#) und Leerzeilen raus
    lines = [l for l in raw.splitlines() if l.strip() and not l.strip().startswith("#")]
    if not lines:
        return []

    clean = "\n".join(lines)
    objects = []
    try:
        reader = csv.DictReader(io.StringIO(clean))
    except Exception:
        return []

    # Spaltennamen normalisieren (SDSS gibt manchmal Lowercase zurück)
    def _get(row, *keys):
        for k in keys:
            v = row.get(k) or row.get(k.lower()) or row.get(k.upper())
            if v is not None:
                return str(v).strip()
        return ""

    for row in reader:
        try:
            obj_id = _get(row, "objID", "objid")
            if not obj_id or obj_id.lower() in ("objid", ""):
                continue
            ra_s  = _get(row, "ra")
            dec_s = _get(row, "dec")
            if not ra_s or not dec_s:
                continue
            ra_o  = float(ra_s)
            dec_o = float(dec_s)
            otype = int(_get(row, "type") or "0")

            def _f(*keys):
                v = _get(row, *keys)
                try:
                    f = float(v)
                    return f if 1.0 < f < 99.0 else None
                except Exception:
                    return None

            psf_r   = _f("psfMag_r", "psfmag_r")
            petro_r = _f("petroMag_r", "petromag_r")
            if otype == 6:
                mag_r = psf_r
            elif otype == 3:
                mag_r = petro_r if petro_r is not None else psf_r
            else:
                mag_r = psf_r if psf_r is not None else petro_r

            # Rotverschiebung aus SpecObj (NULL wenn kein Spektrum vorhanden)
            def _fz(*keys):
                v = _get(row, *keys)
                try:
                    f = float(v)
                    return f if -0.1 < f < 10.0 else None
                except Exception:
                    return None

            specz      = _fz("specz", "z")
            specz_err  = _fz("specz_err", "zErr")
            spec_class = (_get(row, "spec_class", "class") or "").strip().upper() or None
            photoz     = _fz("photoz")
            photoz_err = _fz("photoz_err", "zerr")

            objects.append({
                "obj_id":     f"SDSS {obj_id}",
                "tile_id":    tile_id,
                "ra":         ra_o,
                "dec":        dec_o,
                "mag_r":      mag_r,
                "mag_u":      _f("u", "modelMag_u"),
                "mag_g":      _f("g", "modelMag_g"),
                "mag_i":      _f("i", "modelMag_i"),
                "mag_z":      _f("z", "modelMag_z"),
                "obj_type":   otype,
                "specz":      specz,
                "specz_err":  specz_err,
                "spec_class": spec_class,
                "photoz":     photoz,
                "photoz_err": photoz_err,
            })
        except Exception:
            continue
    return objects


def _fetch_tile_from_vizier(tile_id: int, mag_limit: float,
                            progress_cb=None) -> List[Dict]:
    """
    Fallback-Quelle: SDSS DR16 aus dem VizieR-Spiegel (Katalog V/154) via TAP.
    Liefert dieselbe Objektstruktur wie _parse_sdss_csv. Spalten-Mapping:
      class (3=Galaxie, 6=Stern) ≙ SDSS type · zsp/e_zsp = spektroskopisches z
      zph/e_zph = Photo-z · spCl = Spektralklasse (GALAXY/QSO/STAR)
    Kein mode-Filter: die mode-Kodierung in V/154 weicht von SkyServer ab;
    Duplikate werden ohnehin über obj_id dedupliziert.
    """
    ra_c, dec_c = _tile_center(tile_id)
    r_deg = TILE_DEG * 0.75
    safe_mag = min(float(mag_limit), 22.5)
    adql = (
        f'SELECT TOP {MAX_PER_TILE} objID, RA_ICRS, DE_ICRS, class, '
        f'umag, gmag, rmag, imag, zmag, zsp, e_zsp, spCl, zph, e_zph '
        f'FROM "V/154/sdss16" '
        f'WHERE RA_ICRS BETWEEN {ra_c - r_deg:.6f} AND {ra_c + r_deg:.6f} '
        f'AND DE_ICRS BETWEEN {dec_c - r_deg:.6f} AND {dec_c + r_deg:.6f} '
        f'AND rmag BETWEEN 1.0 AND {safe_mag:.2f}'
    )
    data = urllib.parse.urlencode({
        "REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "csv", "QUERY": adql
    }).encode("utf-8")
    req = urllib.request.Request(_VIZIER_TAP, data=data, headers={
        "User-Agent": _USER_AGENT,
        "Content-Type": "application/x-www-form-urlencoded",
    })
    with urllib.request.urlopen(req, timeout=90) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    if "<html" in raw[:300].lower():
        raise RuntimeError("VizieR lieferte HTML statt CSV")

    objects = []
    for row in csv.DictReader(io.StringIO(raw)):
        try:
            obj_id = str(row.get("objID", "")).strip()
            if not obj_id:
                continue
            ra_o  = float(row["RA_ICRS"])
            dec_o = float(row["DE_ICRS"])
            try: otype = int(str(row.get("class", "")).strip() or "0")
            except ValueError: otype = 0

            def _f(key, lo=1.0, hi=99.0):
                v = str(row.get(key, "")).strip()
                try:
                    f = float(v)
                    return f if lo < f < hi else None
                except ValueError:
                    return None

            spec_class = (str(row.get("spCl", "")).strip().upper() or None)
            objects.append({
                "obj_id":     f"SDSS {obj_id}",
                "tile_id":    tile_id,
                "ra":         ra_o,
                "dec":        dec_o,
                "mag_r":      _f("rmag"),
                "mag_u":      _f("umag"),
                "mag_g":      _f("gmag"),
                "mag_i":      _f("imag"),
                "mag_z":      _f("zmag"),
                "obj_type":   otype,
                "specz":      _f("zsp", -0.1, 10.0),
                "specz_err":  _f("e_zsp", -0.1, 10.0),
                "spec_class": spec_class,
                "photoz":     _f("zph", -0.1, 10.0),
                "photoz_err": _f("e_zph", -0.1, 10.0),
            })
        except Exception:
            continue
    return objects


# ──────────────────────────────────────────────────────────────────────────────
# Öffentliche API
# ──────────────────────────────────────────────────────────────────────────────

def _in_sdss_footprint(ra_deg: float, dec_deg: float) -> bool:
    """
    Grobe Prüfung ob (ra, dec) im SDSS DR17 Imaging-Footprint liegt.

    Der SDSS-Footprint ist komplex und unregelmäßig. Diese Funktion ist eine
    konservative Näherung basierend auf den bekannten Survey-Grenzen.
    Felder die hier als "im Footprint" gelten aber tatsächlich nicht abgedeckt
    sind, liefern einfach 0 Objekte – kein Schaden.

    Wichtige Lücken im SDSS-Footprint (RA in Grad):
      - RA 170°–210°, Dec > 30°: Galaktische Ebene + Vermeidungszonen
        (M51-Region RA≈198°, Dec≈+37° ist NICHT im SDSS)
      - RA < 100° oder > 270°, Dec > 20°: Nicht beobachtet (außer Stripe 82)
      - Dec < -20° generell sehr lückenhaft
    """
    ra  = ra_deg % 360.0
    dec = dec_deg

    # Zuerst grob ausschließen
    if dec < -20 or dec > 75:
        return False

    # ── Galaktische Vermeidungszone (grob) ───────────────────────────────────
    # SDSS meidet die galaktische Ebene (b < ~30°). Das betrifft grob:
    # RA 160°–230°, Dec 20°–60° (Virgo/Coma-Bereich ist drin, aber M51-Region nicht)
    # M51: RA=198.9°, Dec=47.2° → liegt im galaktischen Norden, aber
    # außerhalb des tatsächlichen SDSS-Footprints (zu wenig Beobachtungen)
    # Anhand der SDSS DR7 Footprint-Karte: RA 185°–215°, Dec > 33° ist LÜCKE
    if 180 <= ra <= 220 and dec > 30:
        return False

    # ── Südliche Streifen (Stripe 82) ────────────────────────────────────────
    if (ra >= 310 or ra <= 60) and -2 <= dec <= 2:
        return True

    # ── Nördlicher Hauptsurvey ────────────────────────────────────────────────
    # Kernbereich: RA 120°–175° und 215°–260°, Dec 0°–65°
    if 120 <= ra <= 175 and 0 <= dec <= 65:
        return True
    if 215 <= ra <= 260 and 0 <= dec <= 65:
        return True

    # ── Äquatorialer Bereich ──────────────────────────────────────────────────
    if 100 <= ra <= 260 and -5 <= dec <= 15:
        return True

    # ── Weitere sichere Bereiche ──────────────────────────────────────────────
    if 130 <= ra <= 165 and 15 <= dec <= 70:
        return True
    if 220 <= ra <= 255 and 15 <= dec <= 65:
        return True

    return False


def query_sdss_for_field(ra_deg: float,
                          dec_deg: float,
                          radius_deg: float,
                          mag_limit: float,
                          progress_cb=None) -> List[Dict]:
    """
    Hauptfunktion. Gibt alle SDSS DR17 Objekte im Kegel zurück,
    kompatibel zum Format von solver.query_region().

    Parameter
    ---------
    ra_deg, dec_deg : Bildfeld-Mittelpunkt (Grad)
    radius_deg      : Suchradius (typisch: halbe Bilddiagonale)
    mag_limit       : Maximale r-Band-Magnitude (vom Nutzer eingestellt)
    progress_cb     : Optionale Callback-Funktion für Fortschrittsmeldungen

    Rückgabe
    --------
    Liste von Dicts mit Feldern:
        id, catalog, ra, dec, magnitude, type, name, description
    (identisch zu solver.query_region()-Ausgabe, direkt zusammenführbar)
    """
    def prog(m):
        if progress_cb: progress_cb(m)

    mag_limit = max(10.0, min(float(mag_limit), 22.5))

    # ── Footprint-Check ───────────────────────────────────────────────────────
    # SDSS deckt nur ~1/3 des Himmels ab. Felder außerhalb liefern 0 Objekte
    # und verschwenden Zeit. Wir prüfen GROB vor dem Download.
    in_fp = _in_sdss_footprint(ra_deg, dec_deg)
    if not in_fp:
        prog(f"[SDSS] ⚠ RA={ra_deg:.2f} Dec={dec_deg:.2f} liegt wahrscheinlich "
             f"AUSSERHALB des SDSS-Footprints. SDSS deckt nur ~1/3 des Himmels ab "
             f"(hauptsächlich Dec 0°–70°, RA 100°–270°). Abfrage wird trotzdem versucht.")
        # Wir versuchen es trotzdem – vielleicht liegt der Punkt am Rand.
        # Aber wir cachen das Ergebnis NICHT wenn 0 Objekte kommen,
        # damit zukünftige tiefere Queries nicht blockiert werden.

    with _db_lock:
        conn = _cache_conn()

    # 1. Welche Tiles werden benötigt?
    needed_tiles = _tile_ids_for_cone(ra_deg, dec_deg, radius_deg)
    prog(f"[SDSS] {len(needed_tiles)} Tiles für Kegel "
         f"RA={ra_deg:.3f} Dec={dec_deg:.3f} r={radius_deg:.2f}°")

    # 2. Uncached Tiles vom SkyServer holen
    # Zwei Schutzmechanismen gegen lange Hänger:
    #   a) Zeitbudget: harte Obergrenze für die Gesamtabfrage
    #   b) Circuit Breaker: wenn mehrere Tiles HINTEREINANDER an einem reinen
    #      Verbindungsproblem scheitern, ist der SDSS-Server gerade insgesamt
    #      down/überlastet – weitere Versuche für andere Tiles sind dann
    #      genauso aussichtslos und reine Zeitverschwendung. Wir brechen dann
    #      sofort ab, statt jedes einzelne Tile erst sein eigenes Timeout
    #      durchlaufen zu lassen.
    MAX_QUERY_SECONDS = 300
    MAX_CONSECUTIVE_FAILURES = 8
    query_start = time.time()
    fresh = 0
    consecutive_failures = 0
    abort_reason = None
    failed_tiles = []  # Tiles die an Netzwerkfehlern gescheitert sind → Retry-Liste

    with _db_lock:
        for tile_id in needed_tiles:
            if _tile_is_cached(conn, tile_id, mag_limit):
                continue
            if time.time() - query_start > MAX_QUERY_SECONDS:
                abort_reason = f"Zeitbudget ({MAX_QUERY_SECONDS}s) erreicht"
                break
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                abort_reason = (f"{MAX_CONSECUTIVE_FAILURES} Tiles in Folge "
                                f"mit Verbindungsfehler – SDSS-Server scheint "
                                f"gerade nicht erreichbar/überlastet")
                break

            objs = _fetch_tile_from_sdss(tile_id, mag_limit, prog)
            if objs is None:
                # Reiner Netzwerkfehler – Tile NICHT cachen (war kein
                # bestätigtes "leer"), für Retry-Runde merken
                consecutive_failures += 1
                failed_tiles.append(tile_id)
                continue
            consecutive_failures = 0  # Erfolg (auch 0 Objekte zählt als Erfolg) → Reset
            _insert_objects(conn, objs)
            # Leere Tiles außerhalb des Footprints NICHT dauerhaft cachen
            # (sonst blockieren sie tiefere Queries oder korrektere Footprint-Checks)
            if objs or in_fp:
                _mark_tile_cached(conn, tile_id, mag_limit, len(objs))
            fresh += 1

        # ── Retry-Runde ──────────────────────────────────────────────────────
        # SDSS-Ausfälle sind oft kurzlebig (einzelne überlastete Worker-Prozesse).
        # Eine zweite Runde nach kurzer Pause holt oft die Hälfte der zuvor
        # gescheiterten Tiles nach, ohne dass der Nutzer manuell neu lösen muss.
        if failed_tiles and not abort_reason and time.time() - query_start < MAX_QUERY_SECONDS - 15:
            prog(f"[SDSS] {len(failed_tiles)} Tile(s) fehlgeschlagen – "
                 f"versuche Retry-Runde nach kurzer Pause ...")
            time.sleep(3)
            retry_recovered = 0
            consecutive_failures = 0  # Für Retry-Runde zurücksetzen
            for tile_id in failed_tiles:
                if time.time() - query_start > MAX_QUERY_SECONDS:
                    prog(f"[SDSS] Zeitbudget während Retry-Runde erreicht")
                    break
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    prog(f"[SDSS] Retry-Runde ebenfalls erfolglos – SDSS bleibt instabil")
                    break
                objs = _fetch_tile_from_sdss(tile_id, mag_limit, prog)
                if objs is None:
                    consecutive_failures += 1
                    continue
                consecutive_failures = 0
                _insert_objects(conn, objs)
                if objs or in_fp:
                    _mark_tile_cached(conn, tile_id, mag_limit, len(objs))
                fresh += 1
                retry_recovered += 1
            if retry_recovered:
                prog(f"[SDSS] Retry-Runde: {retry_recovered}/{len(failed_tiles)} "
                     f"Tiles nachträglich erfolgreich geladen")

    if fresh:
        prog(f"[SDSS] {fresh} Tiles neu geladen und gecacht")
    if abort_reason:
        prog(f"[SDSS] Abgebrochen: {abort_reason}. "
             f"Bisher geladene Objekte werden trotzdem angezeigt. "
             f"Fehlende Tiles werden beim nächsten Solve erneut versucht.")

    # 3. Aus Cache lesen
    with _db_lock:
        cached = _query_cache(conn, ra_deg, dec_deg, radius_deg, mag_limit)
        conn.close()

    if not in_fp and len(cached) == 0:
        prog(f"[SDSS] ✗ 0 Objekte – Feld liegt außerhalb des SDSS-Footprints "
             f"(RA={ra_deg:.1f}° Dec={dec_deg:.1f}°). "
             f"SDSS deckt diesen Bereich nicht ab.")
    else:
        prog(f"[SDSS] {len(cached)} Objekte im Bildfeld (mag_r <= {mag_limit:.1f})")

    # 4. In solver-kompatibles Format umwandeln
    result = [_to_solver_format(o) for o in cached]
    # Footprint-Info als Metadaten anhängen (wird von server.py ausgewertet)
    for o in result:
        o["_sdss_in_footprint"] = in_fp
    return result


def _to_solver_format(o: Dict) -> Dict:
    """
    Wandelt ein SDSS-Cache-Objekt in das Format von solver.query_region() um.
    Catalog-Name: "SDSS DR17"
    Type: "galaxy" / "star" / "unknown"
    Magnitude: r-Band (beste verfügbare)
    Description: Farb-Info wenn vorhanden
    """
    otype_int = o.get("obj_type", 0)
    if otype_int == 6:
        obj_type = "star"
    elif otype_int == 3:
        obj_type = "galaxy"
    else:
        obj_type = "unknown"

    mag_r = o.get("mag_r")
    mag_g = o.get("mag_g")
    mag_i = o.get("mag_i")

    specz      = o.get("specz")
    specz_err  = o.get("specz_err")
    spec_class = o.get("spec_class")
    photoz     = o.get("photoz")
    photoz_err = o.get("photoz_err")

    desc_parts = []
    if mag_r is not None: desc_parts.append(f"r={mag_r:.2f}")
    if mag_g is not None: desc_parts.append(f"g={mag_g:.2f}")
    if mag_i is not None: desc_parts.append(f"i={mag_i:.2f}")
    if mag_g is not None and mag_r is not None:
        gr = mag_g - mag_r
        desc_parts.append("rot" if gr > 0.6 else "blau" if gr < 0.3 else "")
    if specz  is not None: desc_parts.append(f"z={specz:.4f}(spec)")
    elif photoz is not None: desc_parts.append(f"z≈{photoz:.3f}(photo)")
    description = " | ".join(p for p in desc_parts if p) or "SDSS PhotoObj"

    obj_id = o["obj_id"]
    return {
        "id":            obj_id,
        "catalog":       "SDSS DR17",
        "ra":            o["ra"],
        "dec":           o["dec"],
        "magnitude":     mag_r,
        "type":          obj_type,
        "name":          obj_id,
        "description":   description,
        "redshift_z":    round(specz,  6) if specz  is not None else None,
        "redshift_err":  round(specz_err, 6) if specz_err is not None else None,
        "spec_class":    spec_class,
        "photoz":        round(photoz, 4) if photoz is not None else None,
        "photoz_err":    round(photoz_err, 4) if photoz_err is not None else None,
        "sdss_mag_u":    o.get("mag_u"),
        "sdss_mag_g":    mag_g,
        "sdss_mag_r":    mag_r,
        "sdss_mag_i":    mag_i,
        "sdss_mag_z":    o.get("mag_z"),
        "sdss_type":     otype_int,
    }


def cache_stats() -> Dict:
    """Gibt Statistiken über den lokalen SDSS-Cache zurück."""
    try:
        conn = _cache_conn()
        n_tiles = conn.execute(
            "SELECT COUNT(*) FROM sdss_tiles"
        ).fetchone()[0]
        n_objs = conn.execute(
            "SELECT COUNT(*) FROM sdss_objects"
        ).fetchone()[0]
        deepest = conn.execute(
            "SELECT MAX(mag_limit) FROM sdss_tiles"
        ).fetchone()[0]
        oldest = conn.execute(
            "SELECT MIN(queried_at) FROM sdss_tiles"
        ).fetchone()[0]
        db_mb = round(CACHE_DB.stat().st_size / 1_048_576, 1) if CACHE_DB.exists() else 0
        conn.close()
        return {
            "tiles_cached": n_tiles,
            "objects_cached": n_objs,
            "deepest_mag": deepest,
            "oldest_tile_ts": oldest,
            "cache_db_mb": db_mb,
        }
    except Exception as e:
        return {"error": str(e)}


def invalidate_cache(older_than_days: Optional[int] = None) -> dict:
    """
    Leert den Cache (ganz oder nur veraltete Tiles).
    older_than_days=None → komplett löschen
    older_than_days=N    → nur Tiles älter als N Tage

    WICHTIG: SQLite gibt durch DELETE allein keinen Plattenplatz frei – die
    Datei behält ihre Größe, gelöschte Zeilen werden nur intern als "frei"
    markiert und für künftige INSERTs wiederverwendet. Um die Datei auch
    auf der Festplatte wirklich kleiner zu machen, ist ein VACUUM nötig.
    Das kann bei großen Caches (>100MB) einige Sekunden dauern, läuft aber
    nur einmal beim expliziten "Cache leeren" – nicht bei jeder Abfrage.

    Rückgabe: {"size_before_mb":..., "size_after_mb":...} zur Anzeige im UI.
    """
    size_before = CACHE_DB.stat().st_size if CACHE_DB.exists() else 0

    conn = _cache_conn()
    if older_than_days is None:
        conn.execute("DELETE FROM sdss_objects")
        conn.execute("DELETE FROM sdss_tiles")
        conn.commit()
    else:
        cutoff = int(time.time()) - older_than_days * 86400
        old_tiles = [r[0] for r in conn.execute(
            "SELECT tile_id FROM sdss_tiles WHERE queried_at < ?", (cutoff,)
        ).fetchall()]
        if old_tiles:
            ph = ",".join("?" * len(old_tiles))
            conn.execute(f"DELETE FROM sdss_objects WHERE tile_id IN ({ph})", old_tiles)
            conn.execute(f"DELETE FROM sdss_tiles  WHERE tile_id IN ({ph})", old_tiles)
            conn.commit()

    # VACUUM gibt den freigewordenen Platz tatsächlich an das Dateisystem
    # zurück. Muss außerhalb einer Transaktion laufen, daher hier separat.
    # WAL-Checkpoint zuerst, damit auch die -wal/-shm Hilfsdateien geleert werden.
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
    except Exception:
        pass  # VACUUM kann fehlschlagen wenn andere Verbindungen offen sind – nicht kritisch
    conn.close()

    size_after = CACHE_DB.stat().st_size if CACHE_DB.exists() else 0
    return {
        "size_before_mb": round(size_before / 1_048_576, 1),
        "size_after_mb":  round(size_after  / 1_048_576, 1),
    }


def diagnose(ra_deg: float = 186.5, dec_deg: float = 33.5) -> Dict:
    """
    Diagnosefunktion: testet SDSS-Konnektivität und gibt Rohtext zurück.
    Aufruf über /api/sdss_diagnose (wird in server.py registriert).
    """
    results = {}

    # Teste alle Endpunkte mit minimalem SQL
    for base_url in _SDSS_TAP:
        for strategy in [0, 1, 2]:
            sql = _build_sdss_sql(ra_deg, dec_deg, 10.0, 18.0, strategy)
            key = f"{base_url.split('/')[2]}_s{strategy}"
            try:
                params = urllib.parse.urlencode({"cmd": sql.strip(), "format": "csv"})
                url    = base_url + "?" + params
                req    = urllib.request.Request(url, headers={
                    "User-Agent": _USER_AGENT,
                    "Accept": "text/csv,text/plain,*/*",
                })
                with urllib.request.urlopen(req, timeout=20) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                objs = _parse_sdss_csv(raw, 0)
                results[key] = {
                    "ok": True,
                    "bytes": len(raw),
                    "objects": len(objs),
                    "preview": raw[:300],
                }
            except Exception as e:
                results[key] = {
                    "ok": False,
                    "error": f"{type(e).__name__}: {e}",
                }

    return results
    """
    Leert den Cache (ganz oder nur veraltete Tiles).
    Nützlich wenn der Nutzer eine tiefere Magnitude abfragen möchte
    als bisher gecacht.
    """
    conn = _cache_conn()
    if older_than_days is None:
        conn.execute("DELETE FROM sdss_objects")
        conn.execute("DELETE FROM sdss_tiles")
        conn.commit()
    else:
        cutoff = int(time.time()) - older_than_days * 86400
        old_tiles = [r[0] for r in conn.execute(
            "SELECT tile_id FROM sdss_tiles WHERE queried_at < ?", (cutoff,)
        ).fetchall()]
        if old_tiles:
            ph = ",".join("?" * len(old_tiles))
            conn.execute(f"DELETE FROM sdss_objects WHERE tile_id IN ({ph})", old_tiles)
            conn.execute(f"DELETE FROM sdss_tiles WHERE tile_id IN ({ph})", old_tiles)
            conn.commit()
    conn.close()
