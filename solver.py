"""
Astro Plate Solver – Kern-Engine v3
- ASTAP-Integration fuer automatische Koordinatenerkennung (PNG/JPEG/FITS)
- Robusterer Katalog-Download mit Retry und Chunking
- PGC, Gaia DR3, Quasare, Tycho-2, NGC/IC, Messier
"""

import numpy as np
import sqlite3
import json
import os
import math
import time
import subprocess
import tempfile
import shutil
import urllib.request
import urllib.parse
import csv
import io
import re
from pathlib import Path
from typing import List, Dict, Tuple, Optional

try:
    from astropy.io import fits
    from astropy.wcs import WCS
    HAS_ASTROPY = True
except ImportError:
    HAS_ASTROPY = False

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    from scipy import ndimage
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False

DB_PATH = Path(__file__).parent / "catalog.db"

# Typische ASTAP-Pfade unter Windows
ASTAP_CANDIDATES = [
    r"C:\Program Files\astap\astap.exe",
    r"C:\Program Files (x86)\astap\astap.exe",
    r"C:\Programme\astap\astap.exe",
    r"C:\Programme (x86)\astap\astap.exe",
    r"C:\astap\astap.exe",
    r"D:\astap\astap.exe",
    str(Path.home() / "astap" / "astap.exe"),
]


# ══════════════════════════════════════════════════════════════════════════════
# ASTAP Plate Solver
# ══════════════════════════════════════════════════════════════════════════════

def find_astap() -> Optional[str]:
    """Sucht ASTAP-Executable auf dem System."""
    # 1. Im PATH
    found = shutil.which("astap") or shutil.which("astap.exe")
    if found:
        return found
    # 2. Typische Windows-Pfade
    for p in ASTAP_CANDIDATES:
        if Path(p).exists():
            return p
    return None


def astap_solve(img_path: str, astap_exe: str,
                ra_hint: float = None, dec_hint: float = None,
                fov_hint: float = None,
                progress_cb=None) -> Optional[Dict]:
    """
    Startet ASTAP als Subprocess und liest das WCS aus dem erzeugten FITS-Header.
    Gibt wcs_info-Dict zurueck oder None bei Fehler.

    astap Parameter:
      -f  Bilddatei
      -r  Suchradius in Grad (0 = Vollhimmel, langsamer)
      -fov Gesichtsfeld in Grad (0 = automatisch)
      -ra / -spd  Hinweis-Koordinaten (optional)
      -o  Output-Prefix
    """
    def prog(m):
        if progress_cb: progress_cb(m)
        else: print(m)

    img_path = Path(img_path)
    out_prefix = img_path.parent / img_path.stem

    # ASTAP arbeitet am besten mit FITS – PNG/JPEG vorher konvertieren
    tmp_fits = None
    solve_path = img_path

    if img_path.suffix.lower() not in (".fit", ".fits"):
        prog("  Bild wird fuer ASTAP nach FITS konvertiert...")
        try:
            if not HAS_PIL:
                raise ImportError("pillow benoetigt")
            img_pil = Image.open(str(img_path))
            # Preserve luminance range: convert to grayscale but keep 16-bit dynamic range
            if img_pil.mode in ("RGB", "RGBA"):
                # Use luminance-weighted conversion, then scale to 16-bit
                img_gray = img_pil.convert("L")
                arr = np.array(img_gray, dtype=np.float32)
                # Scale 8-bit (0-255) to 16-bit (0-65535) for ASTAP sensitivity
                arr = (arr / 255.0 * 65535.0).astype(np.uint16)
            elif img_pil.mode == "I":
                arr = np.array(img_pil, dtype=np.uint16)
            else:
                arr = np.array(img_pil.convert("L"), dtype=np.float32)
                arr = (arr / arr.max() * 65535).astype(np.uint16) if arr.max() > 0 else arr.astype(np.uint16)
            # FITS speichern
            tmp_fits = img_path.parent / (img_path.stem + "_tmp_astap.fits")
            if HAS_ASTROPY:
                hdu = fits.PrimaryHDU(arr)
                hdu.writeto(str(tmp_fits), overwrite=True)
            else:
                _write_simple_fits(arr, str(tmp_fits))
            solve_path = tmp_fits
        except Exception as e:
            prog(f"  Konvertierung fehlgeschlagen: {e}")
            return None

    astap_dir = Path(astap_exe).parent

    # Katalog-Erkennung (VOR dem cmd-Bau, damit Diagnose + Typ-Wahl stimmen)
    # ASTAP-DB-Typen:  d50 (mag5 tiny) / d80 (mag8 wide-field) /
    #                  v17-v50, g17, h17, h18 (tief fuer Narrow-Field)
    has_d80 = bool(list(astap_dir.glob("d80_*")))
    has_d50 = bool(list(astap_dir.glob("d50_*")))
    has_h18 = bool(list(astap_dir.glob("h18_*")))
    has_h17 = bool(list(astap_dir.glob("h17_*")))
    has_v17 = bool(list(astap_dir.glob("v17_*")))
    has_g17 = bool(list(astap_dir.glob("g17_*")))

    # Typ-Prioritaet: tiefer Katalog zuerst (besser fuer engere Felder)
    if   has_h18: db_type = "h18"
    elif has_h17: db_type = "h17"
    elif has_v17: db_type = "v17"
    elif has_g17: db_type = "g17"
    elif has_d80: db_type = "d80"
    elif has_d50: db_type = "d50"
    else:         db_type = None

    cmd = [astap_exe, "-f", str(solve_path), "-o", str(out_prefix)]
    # WICHTIG: -d erwartet den PFAD zum DB-Verzeichnis (nicht den Typ!)
    # Der DB-Typ wird von ASTAP automatisch aus den Dateinamen erkannt.
    cmd += ["-d", str(astap_dir)]

    if ra_hint is not None and dec_hint is not None:
        # ASTAP erwartet SPD (South Polar Distance) statt Dec
        spd = dec_hint + 90.0
        cmd += ["-ra", f"{ra_hint/15:.6f}", "-spd", f"{spd:.4f}", "-r", "30"]
    else:
        # Vollhimmelsuche: ASTAP -r 180 = kompletter Himmel
        cmd += ["-r", "180"]

    # FOV Hint: 0 = automatisch (ASTAP probiert mehrere)
    if fov_hint is not None and fov_hint > 0:
        cmd += ["-fov", f"{fov_hint:.3f}"]
    else:
        cmd += ["-fov", "0"]

    # -s: star limit fuer Matching (mehr Sterne = mehr Robustheit)
    cmd += ["-s", "1000"]

    prog(f"  ASTAP gestartet: {' '.join(cmd)}")
    prog(f"  Arbeitsverzeichnis: {astap_dir}")

    # Katalog-Diagnose
    all_catalog_files = (list(astap_dir.glob("*.290")) +
                         list(astap_dir.glob("*.1476")) +
                         list(astap_dir.glob("h1?_*")) +
                         list(astap_dir.glob("d??_*")) +
                         list(astap_dir.glob("v1?_*")) +
                         list(astap_dir.glob("g1?_*")))
    all_catalog_files = list({str(f): f for f in all_catalog_files}.values())
    if not all_catalog_files:
        prog(f"  WARNUNG: Keine Katalog-Dateien in {astap_dir} gefunden!")
        prog("  Bitte ASTAP -> File -> Preferences -> Star database path pruefen")
    else:
        exts = sorted({f.suffix for f in all_catalog_files})
        prog(f"  Katalog: {len(all_catalog_files)} Dateien ({', '.join(exts)}), Typ={db_type or 'auto'}")
        if db_type in ("d80", "d50"):
            prog(f"  HINWEIS: {db_type.upper()} ist flach (mag<={8 if db_type=='d80' else 5}).")
            prog("           Nur fuer wide-field (>>1 Grad) geeignet.")
            prog("           Fuer schmale Felder: H17/H18/V17 von ASTAP-Webseite laden.")

    def _run_astap(cmd_list, timeout=120):
        """
        Startet ASTAP als normalen Prozess mit eigenem Fenster.
        Pollt in 1s-Schritten auf Fertigstellung und gibt
        regelmaessig Lebenszeichen ins Log.
        """
        try:
            proc = subprocess.Popen(
                cmd_list,
                cwd=str(astap_dir),
                # KEIN stdout/stderr-Redirect: ASTAP zeigt sein eigenes Fenster
            )
        except FileNotFoundError:
            return None, "not_found"
        except Exception as e:
            return None, str(e)

        prog(f"  ASTAP laeuft (PID {proc.pid}) - eigenes Fenster oben links sollte Fortschritt zeigen")
        t0 = time.time()
        last_report = t0
        while True:
            rc = proc.poll()
            if rc is not None:
                prog(f"  ASTAP beendet nach {int(time.time()-t0)}s (rc={rc})")
                return "", None
            now = time.time()
            if now - t0 > timeout:
                prog(f"  ASTAP Timeout nach {timeout}s - wird beendet")
                try: proc.kill()
                except: pass
                try: proc.wait(5)
                except: pass
                return "", "timeout"
            if now - last_report >= 5:
                prog(f"  ... ASTAP noch aktiv ({int(now-t0)}s)")
                last_report = now
            time.sleep(0.5)

    stdout, err = _run_astap(cmd, timeout=1000)
    if err == "timeout":
        prog("  ASTAP Timeout (>1000s)")
        _cleanup_tmp(tmp_fits)
        return None
    elif err == "not_found":
        prog(f"  ASTAP nicht gefunden: {astap_exe}")
        _cleanup_tmp(tmp_fits)
        return None
    elif err:
        prog(f"  ASTAP Fehler: {err}")
        _cleanup_tmp(tmp_fits)
        return None

    # (Live-Streaming in _run_astap hat Output bereits ausgegeben)

    # ASTAP legt oft eine .log-Datei an — auch die mitlesen
    log_file = out_prefix.with_suffix(".log")
    if log_file.exists():
        try:
            ltxt = log_file.read_text(errors="ignore")
            for line in ltxt.strip().splitlines()[-20:]:
                if line.strip():
                    prog(f"  LOG: {line.strip()}")
        except Exception:
            pass

    # Retry-Kaskade: mit verschiedenen FOV-Bereichen wenn erster Versuch fehlschlug
    wcs_file = out_prefix.with_suffix(".wcs")
    ini_file = out_prefix.with_suffix(".ini")
    retries = []
    if not wcs_file.exists() and not ini_file.exists() and ra_hint is None:
        # Versuch 2: kleiner FOV (engere Felder, tiefe Kataloge)
        retries.append(("engeres Feld (FOV=2 Grad)", ["-fov", "2"]))
        # Versuch 3: mittleres FOV
        retries.append(("mittleres Feld (FOV=5 Grad)", ["-fov", "5"]))
        # Versuch 4: weites Feld
        retries.append(("weites Feld (FOV=15 Grad)", ["-fov", "15"]))
    for label, extra in retries:
        if wcs_file.exists() or ini_file.exists():
            break
        prog(f"  Retry: {label}")
        cmd2 = [astap_exe, "-f", str(solve_path), "-o", str(out_prefix),
                "-d", str(astap_dir), "-r", "180", "-s", "1000"] + extra
        stdout2, err2 = _run_astap(cmd2, timeout=1000)
        if log_file.exists():
            try:
                ltxt = log_file.read_text(errors="ignore")
                for line in ltxt.strip().splitlines()[-15:]:
                    if line.strip():
                        prog(f"  Retry-LOG: {line.strip()}")
            except Exception:
                pass

    # ASTAP erzeugt <stem>.wcs FITS-Datei mit dem Ergebnis
    # (wcs_file und ini_file bereits oben definiert fuer den Retry-Check)
    wcs_info = None

    # Methode 1: .wcs FITS-Datei – volle CD-Matrix extrahieren
    # ASTAP schreibt einen Header-only FITS-Stub (nicht 2880-aligned).
    # Astropy warnt laut, daher parsen wir den Header direkt als ASCII.
    if wcs_file.exists() and HAS_ASTROPY:
        try:
            raw_txt = wcs_file.read_bytes().decode("ascii", errors="ignore")
            hdr = fits.Header.fromstring(raw_txt, sep="")
            # Bildgroesse: aus originalem Bild holen (wcs hat oft NAXIS1=0)
            if HAS_PIL:
                img_orig = Image.open(str(img_path))
                img_w, img_h = img_orig.size
            else:
                img_w = int(hdr.get("NAXIS1", 3000))
                img_h = int(hdr.get("NAXIS2", 2000))

            crval1 = float(hdr["CRVAL1"])
            crval2 = float(hdr["CRVAL2"])
            crpix1 = float(hdr.get("CRPIX1", img_w/2 + 0.5))
            crpix2 = float(hdr.get("CRPIX2", img_h/2 + 0.5))

            # CD-Matrix bevorzugen, sonst CDELT+CROTA
            if "CD1_1" in hdr:
                cd11 = float(hdr["CD1_1"])
                cd12 = float(hdr.get("CD1_2", 0.0))
                cd21 = float(hdr.get("CD2_1", 0.0))
                cd22 = float(hdr["CD2_2"])
            elif "CDELT1" in hdr:
                cdelt1 = float(hdr["CDELT1"])
                cdelt2 = float(hdr.get("CDELT2", -abs(cdelt1)))
                crota  = float(hdr.get("CROTA2", 0.0))
                cos_r  = math.cos(math.radians(crota))
                sin_r  = math.sin(math.radians(crota))
                cd11, cd12 =  cdelt1*cos_r, -cdelt2*sin_r
                cd21, cd22 =  cdelt1*sin_r,  cdelt2*cos_r
            else:
                raise ValueError("Keine CD-Matrix und kein CDELT in .wcs")

            scale = math.sqrt(abs(cd11*cd22 - cd12*cd21))
            rot   = math.degrees(math.atan2(-cd12, cd11))
            # Bildmitte in FITS-Koordinaten (1-basiert, Y von unten)
            # FITS-Pixel der Bildmitte: (img_w/2+0.5, img_h/2+0.5)
            # Abstand vom Referenzpixel:
            dx =  (img_w/2.0 + 0.5) - crpix1   # X: gleiche Richtung
            dy = -(img_h/2.0 + 0.5 - crpix2)   # Y: FITS von unten, also negieren
            xi_c  = cd11*dx + cd12*dy
            eta_c = cd21*dx + cd22*dy
            ra_c, dec_c = _tan_deproject(crval1, crval2, xi_c, eta_c)

            wcs_info = {
                "ra_center": ra_c, "dec_center": dec_c,
                "scale_deg_per_px": scale, "rotation_deg": rot,
                "cd11": cd11, "cd12": cd12, "cd21": cd21, "cd22": cd22,
                "crval1": crval1, "crval2": crval2,
                "crpix1": crpix1, "crpix2": crpix2,
                "img_w": img_w, "img_h": img_h,
                "source": "ASTAP", "has_full_cd": True,
            }
            prog(f"  WCS: RA={ra_c:.4f} Dec={dec_c:.4f} "
                 f"Skala={scale*3600:.2f}\"/px Rot={rot:.1f}deg [CD-Matrix]")
        except Exception as e:
            prog(f"  .wcs lesen fehlgeschlagen: {e}")

    # Methode 2: .ini Datei
    if wcs_info is None and ini_file.exists():
        try:
            wcs_info = _parse_astap_ini(str(ini_file), str(img_path), prog)
        except Exception as e:
            prog(f"  .ini lesen fehlgeschlagen: {e}")

    # Aufraumen
    _cleanup_tmp(tmp_fits)
    for ext in (".wcs", ".ini", ".log"):
        f = out_prefix.with_suffix(ext)
        try:
            if f.exists(): f.unlink()
        except Exception:
            pass

    if wcs_info is None:
        prog("  ASTAP konnte das Bild nicht loesen")
        prog("  Tipp: ASTAP → File → Preferences → Star database path pruefen")
        prog("  D80/D50 Katalog muss im richtigen Pfad liegen (gleicher Ordner wie astap.exe empfohlen)")
        prog("  Katalog-Dateien: d80_*.290 oder h18_*.290 — falls vorhanden aber nicht gefunden: Pfad in ASTAP neu setzen")

    return wcs_info


def _parse_astap_ini(ini_path: str, img_path: str, prog) -> Optional[Dict]:
    """
    Liest ASTAP .ini Output-Datei.
    ASTAP schreibt eine vollstaendige FITS-WCS CD-Matrix:
      CRVAL1/2  = RA/Dec des Referenzpixels (Grad)
      CRPIX1/2  = Referenzpixel (1-basiert)
      CD1_1/1_2/2_1/2_2 = Transformationsmatrix (Grad/Pixel)
    """
    data = {}
    with open(ini_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if "=" in line:
                k, _, v = line.partition("=")
                # Kommentare nach Slash entfernen
                v = v.split("/")[0].strip()
                data[k.strip().upper()] = v

    solved = data.get("PLTSOLVD", "F").upper().strip("'\" ")
    if solved != "T":
        prog(f"  ASTAP: PLTSOLVD={solved} (nicht geloest)")
        return None

    try:
        # Bildgroesse bestimmen
        if HAS_PIL:
            img_pil = Image.open(img_path)
            img_w, img_h = img_pil.size
        else:
            img_w, img_h = 3000, 2000

        # Referenzpunkt
        crval1 = float(data["CRVAL1"])   # RA  des Referenzpixels (Grad)
        crval2 = float(data["CRVAL2"])   # Dec des Referenzpixels (Grad)

        # CRPIX: wo liegt der Referenzpunkt im Bild? (FITS: 1-basiert)
        crpix1 = float(data.get("CRPIX1", str(img_w / 2 + 0.5)))
        crpix2 = float(data.get("CRPIX2", str(img_h / 2 + 0.5)))

        # CD-Matrix (Grad pro Pixel)
        if "CD1_1" in data:
            cd11 = float(data["CD1_1"])
            cd12 = float(data.get("CD1_2", "0"))
            cd21 = float(data.get("CD2_1", "0"))
            cd22 = float(data["CD2_2"])
        elif "CDELT1" in data:
            cdelt1 = float(data["CDELT1"])
            cdelt2 = float(data.get("CDELT2", str(-abs(cdelt1))))
            crota  = float(data.get("CROTA2", "0"))
            cos_r  = math.cos(math.radians(crota))
            sin_r  = math.sin(math.radians(crota))
            cd11   =  cdelt1 * cos_r
            cd12   = -cdelt2 * sin_r
            cd21   =  cdelt1 * sin_r
            cd22   =  cdelt2 * cos_r
        else:
            prog("  Keine CD-Matrix und kein CDELT in INI")
            return None

        # Pixelskala = sqrt(|det(CD)|) in Grad/Pixel
        scale = math.sqrt(abs(cd11 * cd22 - cd12 * cd21))

        # Nord-Rotation aus CD-Matrix
        rot = math.degrees(math.atan2(cd12, cd22))

        # Bildmitte -> Weltkoordinaten
        # FITS-Koordinaten: X von links, Y von UNTEN (1-basiert)
        # Abstand der Bildmitte (FITS) vom Referenzpixel:
        dx =  (img_w / 2.0 + 0.5) - crpix1   # X gleiche Richtung
        dy = -(img_h / 2.0 + 0.5 - crpix2)   # Y: FITS zaehlt von unten, negieren
        xi_c  = cd11 * dx + cd12 * dy
        eta_c = cd21 * dx + cd22 * dy
        ra_c, dec_c = _tan_deproject(crval1, crval2, xi_c, eta_c)

        prog(f"  INI: CRVAL=({crval1:.4f},{crval2:.4f}) "
             f"CRPIX=({crpix1:.1f},{crpix2:.1f}) "
             f"Skala={scale*3600:.2f}\"/px Rot={rot:.2f}deg")
        prog(f"  Bildmitte: RA={ra_c:.4f} Dec={dec_c:.4f}")

        return {
            "ra_center":        ra_c,
            "dec_center":       dec_c,
            "scale_deg_per_px": scale,
            "rotation_deg":     rot,
            # Vollstaendige CD-Matrix fuer genaue world_to_pixel Umrechnung
            "cd11": cd11, "cd12": cd12,
            "cd21": cd21, "cd22": cd22,
            "crval1": crval1, "crval2": crval2,
            "crpix1": crpix1, "crpix2": crpix2,
            "img_w": img_w, "img_h": img_h,
            "source": "ASTAP",
            "has_full_cd": True,
        }
    except Exception as e:
        prog(f"  INI-Parse-Fehler: {e}")
        import traceback
        prog(traceback.format_exc()[:200])
        return None


def _tan_deproject(ra0: float, dec0: float, xi: float, eta: float):
    """TAN-Rueckprojektion: (xi, eta) in Grad -> (RA, Dec) in Grad."""
    xi_r  = math.radians(xi)
    eta_r = math.radians(eta)
    d0    = math.radians(dec0)
    denom = math.cos(d0) - eta_r * math.sin(d0)
    ra  = ra0 + math.degrees(math.atan2(xi_r, denom))
    dec = math.degrees(math.atan(
        (math.sin(d0) + eta_r * math.cos(d0)) /
        math.sqrt(xi_r**2 + denom**2)
    ))
    return ra % 360, dec


def _write_simple_fits(arr: np.ndarray, path: str):
    """Schreibt minimales FITS ohne astropy (Notfall-Fallback)."""
    h, w = arr.shape
    header = (
        f"SIMPLE  =                    T / file conforms to FITS standard\n"
        f"BITPIX  =                   16 / number of bits per data pixel\n"
        f"NAXIS   =                    2 / number of data axes\n"
        f"NAXIS1  =              {w:8d} / length of data axis 1\n"
        f"NAXIS2  =              {h:8d} / length of data axis 2\n"
        f"END\n"
    )
    # FITS Header muss 2880-Byte-Bloecke haben
    header_bytes = header.encode("ascii")
    pad = (2880 - len(header_bytes) % 2880) % 2880
    header_bytes += b" " * pad
    data_bytes = arr.astype(">i2").tobytes()
    pad2 = (2880 - len(data_bytes) % 2880) % 2880
    data_bytes += b"\x00" * pad2
    with open(path, "wb") as f:
        f.write(header_bytes)
        f.write(data_bytes)


def _cleanup_tmp(path):
    if path and Path(path).exists():
        try: Path(path).unlink()
        except: pass


# ══════════════════════════════════════════════════════════════════════════════
# Datenbank
# ══════════════════════════════════════════════════════════════════════════════

def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-64000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS objects (
            id          TEXT PRIMARY KEY,
            catalog     TEXT NOT NULL,
            ra          REAL NOT NULL,
            dec         REAL NOT NULL,
            magnitude   REAL,
            type        TEXT,
            name        TEXT,
            description TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dec ON objects(dec)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ra  ON objects(ra)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cat ON objects(catalog)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS catalog_meta (
            name TEXT PRIMARY KEY,
            downloaded_at TEXT,
            count INTEGER
        )""")
    conn.commit()
    return conn


def catalog_counts(conn) -> Dict[str, int]:
    rows = conn.execute(
        "SELECT catalog, COUNT(*) FROM objects GROUP BY catalog"
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def query_region(conn, ra_c, dec_c, radius_deg, mag_limit,
                 catalogs=None) -> List[Dict]:
    cos_dec = max(math.cos(math.radians(dec_c)), 0.017)
    d_ra    = radius_deg / cos_dec
    dec_min = max(dec_c - radius_deg, -90.0)
    dec_max = min(dec_c + radius_deg,  90.0)
    ra_min  = (ra_c - d_ra) % 360
    ra_max  = (ra_c + d_ra) % 360

    # Guard: empty list means "no catalogs selected" → treat as no filter
    cat_sql, cat_params = "", []
    if catalogs and len(catalogs) > 0:
        ph = ",".join("?"*len(catalogs))
        cat_sql = f" AND catalog IN ({ph})"
        cat_params = list(catalogs)

    if ra_min <= ra_max:
        sql = (f"SELECT id,catalog,ra,dec,magnitude,type,name,description "
               f"FROM objects WHERE ra BETWEEN ? AND ? AND dec BETWEEN ? AND ? "
               f"AND (magnitude IS NULL OR magnitude <= ?){cat_sql}")
        params = [ra_min, ra_max, dec_min, dec_max, mag_limit] + cat_params
    else:
        sql = (f"SELECT id,catalog,ra,dec,magnitude,type,name,description "
               f"FROM objects WHERE (ra >= ? OR ra <= ?) AND dec BETWEEN ? AND ? "
               f"AND (magnitude IS NULL OR magnitude <= ?){cat_sql}")
        params = [ra_min, ra_max, dec_min, dec_max, mag_limit] + cat_params

    result = []
    for row in conn.execute(sql, params).fetchall():
        sep = _angular_sep(ra_c, dec_c, row[2], row[3])
        if sep <= radius_deg:
            result.append({
                "id": row[0], "catalog": row[1],
                "ra": row[2], "dec": row[3],
                "magnitude": row[4], "type": row[5] or "unknown",
                "name": row[6] or row[0], "description": row[7] or "",
            })
    return result


def _angular_sep(ra1, dec1, ra2, dec2):
    ra1, dec1, ra2, dec2 = map(math.radians, [ra1, dec1, ra2, dec2])
    a = math.sin((dec2-dec1)/2)**2 + math.cos(dec1)*math.cos(dec2)*math.sin((ra2-ra1)/2)**2
    return math.degrees(2*math.asin(min(1.0, math.sqrt(a))))


# ══════════════════════════════════════════════════════════════════════════════
# Katalog-Downloads (mit Retry + Chunking)
# ══════════════════════════════════════════════════════════════════════════════

def download_catalogs(conn, progress_cb=None) -> Dict[str, int]:
    def prog(m):
        if progress_cb: progress_cb(m)
        else: print(m)

    totals = {}

    prog(">>> Messier (eingebettet)...")
    m = _messier_builtin()
    _bulk_insert(conn, m)
    totals["Messier"] = len(m)
    prog(f"    OK: {len(m)} Objekte")

    prog(">>> NGC/IC (OpenNGC via GitHub)...")
    try:
        ngcic = _retry(_dl_opengc, prog, retries=3)
        _bulk_insert(conn, ngcic)
        totals["NGC"] = sum(1 for o in ngcic if o["catalog"]=="NGC")
        totals["IC"]  = sum(1 for o in ngcic if o["catalog"]=="IC")
        prog(f"    OK: NGC {totals['NGC']}  IC {totals['IC']}")
    except Exception as e:
        prog(f"    FEHLER NGC/IC: {e}")

    prog(">>> PGC Hintergrundgalaxien (VizieR, bis mag 19)...")
    try:
        pgc = _retry(lambda p: _dl_pgc(p, mag_limit=19.0, top=200000), prog, retries=3, delay=5)
        _bulk_insert(conn, pgc)
        totals["PGC"] = len(pgc)
        prog(f"    OK: {len(pgc)} Galaxien")
    except Exception as e:
        prog(f"    FEHLER PGC: {e}")
        # Fallback: kleinere Abfrage
        prog("    Fallback: PGC bis mag 16...")
        try:
            pgc2 = _retry(lambda p: _dl_pgc(p, mag_limit=16.0, top=50000), prog, retries=2)
            _bulk_insert(conn, pgc2)
            totals["PGC"] = len(pgc2)
            prog(f"    Fallback OK: {len(pgc2)} Galaxien")
        except Exception as e2:
            prog(f"    PGC Fallback auch fehlgeschlagen: {e2}")

    prog(">>> Tycho-2 Sterne (mag < 10)...")
    try:
        tyc = _retry(lambda p: _dl_tycho(p, mag_limit=10.0, top=50000), prog, retries=3)
        _bulk_insert(conn, tyc)
        totals["Tycho-2"] = len(tyc)
        prog(f"    OK: {len(tyc)} Sterne")
    except Exception as e:
        prog(f"    FEHLER Tycho-2: {e}")

    prog(">>> Gaia DR3 (mag < 16)...")
    try:
        gaia = _retry(lambda p: _dl_gaia(p, mag_limit=16.0, top=500000), prog, retries=3, delay=5)
        _bulk_insert(conn, gaia)
        totals["Gaia DR3"] = len(gaia)
        prog(f"    OK: {len(gaia)} Quellen")
    except Exception as e:
        prog(f"    FEHLER Gaia DR3: {e}")

    prog(">>> Quasare (SDSS DR16 / VCV)...")
    try:
        qso = _retry(lambda p: _dl_quasars(p, top=50000), prog, retries=3, delay=5)
        _bulk_insert(conn, qso)
        totals["Quasar"] = len(qso)
        prog(f"    OK: {len(qso)} Quasare")
    except Exception as e:
        prog(f"    FEHLER Quasare: {e}")

    conn.commit()
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    for name, cnt in totals.items():
        conn.execute("INSERT OR REPLACE INTO catalog_meta VALUES (?,?,?)", (name, ts, cnt))
    conn.commit()
    total_all = sum(totals.values())
    prog(f">>> Download komplett: {total_all:,} Objekte gesamt")
    return totals


def _retry(fn, prog, retries=3, delay=3):
    last_err = None
    for attempt in range(1, retries+1):
        try:
            return fn(prog)
        except Exception as e:
            last_err = e
            if attempt < retries:
                prog(f"    Versuch {attempt} fehlgeschlagen: {e} – Retry in {delay}s...")
                time.sleep(delay)
    raise last_err


def refresh_messier(db_path):
    """Aktualisiert die eingebetteten Messier-Daten in einer bestehenden catalog.db
    (fehlende Objekte nachtragen, Beschreibungen erneuern) — ohne Komplett-Neuaufbau."""
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("DELETE FROM objects WHERE catalog='Messier'")
        _bulk_insert(conn, _messier_builtin())
        return conn.execute("SELECT COUNT(*) FROM objects WHERE catalog='Messier'").fetchone()[0]
    finally:
        conn.close()


def _bulk_insert(conn, objects):
    conn.executemany("""
        INSERT OR REPLACE INTO objects
            (id,catalog,ra,dec,magnitude,type,name,description)
        VALUES (:id,:catalog,:ra,:dec,:magnitude,:type,:name,:description)
    """, objects)
    conn.commit()


def _vizier_tap(query, timeout=150):
    url = "https://tapvizier.cds.unistra.fr/TAPVizieR/tap/sync"
    params = urllib.parse.urlencode({
        "REQUEST":"doQuery","LANG":"ADQL","FORMAT":"csv","QUERY":query
    })
    req = urllib.request.Request(
        url+"?"+params,
        headers={"User-Agent":"AstroSolver/3.0"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def _dl_opengc(prog):
    url = "https://raw.githubusercontent.com/mattiaverga/OpenNGC/master/database_files/NGC.csv"
    with urllib.request.urlopen(url, timeout=30) as r:
        raw = r.read().decode("utf-8")
    type_map = {
        "G":"galaxy","GGroup":"galaxy","GPair":"galaxy","GTrpl":"galaxy","GClstr":"cluster",
        "OC":"cluster","OCl":"cluster","GC":"cluster","*Ass":"cluster",
        "PN":"nebula","SNR":"nebula","EN":"nebula","RN":"nebula","HII":"nebula",
        "Dup":"star","*":"star","D*":"star","**":"star",
    }
    reader = csv.DictReader(io.StringIO(raw), delimiter=";")
    objects = []
    for row in reader:
        try:
            name = row.get("Name","").strip()
            if not name: continue
            rp = row["RA"].strip().split(":")
            dp_raw = row["Dec"].strip()
            sign = -1 if dp_raw.startswith("-") else 1
            dp = dp_raw.lstrip("+-").split(":")
            ra_deg  = (float(rp[0])+float(rp[1])/60+float(rp[2])/3600)*15
            dec_deg = sign*(float(dp[0])+float(dp[1])/60+float(dp[2])/3600)
            try: mag = float(row.get("V-Mag") or row.get("B-Mag") or "")
            except: mag = None
            obj_type = type_map.get(row.get("Type",""),"unknown")
            catalog  = "NGC" if name.startswith("N") else "IC"
            nice = name.replace("NGC","NGC ").replace("IC","IC ").strip()
            objects.append({
                "id":nice,"catalog":catalog,"ra":ra_deg,"dec":dec_deg,
                "magnitude":mag,"type":obj_type,"name":nice,
                "description":row.get("Common names","").strip() or obj_type,
            })
        except: continue
    return objects


def _dl_pgc(prog, mag_limit=19.0, top=200000):
    prog(f"    VizieR TAP (PGC TOP {top} mag<={mag_limit})...")
    raw = _vizier_tap(
        f'SELECT TOP {top} PGC,RAJ2000,DEJ2000,Bmag FROM "VII/237/pgc" WHERE Bmag <= {mag_limit}',
        timeout=300
    )
    reader = csv.DictReader(io.StringIO(raw))
    objects = []
    for row in reader:
        try:
            pid = "PGC "+row["PGC"].strip()
            objects.append({
                "id":pid,"catalog":"PGC",
                "ra":float(row["RAJ2000"]),"dec":float(row["DEJ2000"]),
                "magnitude":float(row["Bmag"]) if row["Bmag"].strip() else None,
                "type":"galaxy","name":pid,"description":"Hintergrundgalaxie (PGC)",
            })
        except: continue
    return objects


def _dl_tycho(prog, mag_limit=10.0, top=50000):
    prog(f"    VizieR TAP (Tycho-2 mag<={mag_limit})...")
    raw = _vizier_tap(
        f'SELECT TOP {top} TYC1,TYC2,TYC3,RAmdeg,DEmdeg,VTmag FROM "I/259/tyc2" WHERE VTmag <= {mag_limit}'
    )
    reader = csv.DictReader(io.StringIO(raw))
    objects = []
    for row in reader:
        try:
            tyc = f"TYC {row['TYC1'].strip()}-{row['TYC2'].strip()}-{row['TYC3'].strip()}"
            mag = float(row["VTmag"]) if row["VTmag"].strip() else None
            objects.append({
                "id":tyc,"catalog":"Tycho-2",
                "ra":float(row["RAmdeg"]),"dec":float(row["DEmdeg"]),
                "magnitude":mag,"type":"star","name":tyc,
                "description":f"Stern V={mag:.1f}" if mag else "Stern",
            })
        except: continue
    return objects


def _dl_gaia(prog, mag_limit=16.0, top=500000):
    prog(f"    VizieR TAP (Gaia DR3 mag<={mag_limit})...")
    raw = _vizier_tap(
        f'SELECT TOP {top} Source,RA_ICRS,DE_ICRS,Gmag FROM "I/355/gaiadr3" WHERE Gmag <= {mag_limit}',
        timeout=1000
    )
    reader = csv.DictReader(io.StringIO(raw))
    objects = []
    for row in reader:
        try:
            gid = "Gaia "+row["Source"].strip()
            mag = float(row["Gmag"]) if row["Gmag"].strip() else None
            objects.append({
                "id":gid,"catalog":"Gaia DR3",
                "ra":float(row["RA_ICRS"]),"dec":float(row["DE_ICRS"]),
                "magnitude":mag,"type":"star","name":gid,
                "description":f"Gaia G={mag:.2f}" if mag else "Gaia Stern",
            })
        except: continue
    return objects


def _dl_quasars(prog, top=50000):
    prog(f"    VizieR TAP (SDSS Quasare TOP {top})...")
    try:
        raw = _vizier_tap(
            f'SELECT TOP {top} SDSS,RA_ICRS,DE_ICRS,gmag,z FROM "VII/289/dr16q" WHERE gmag <= 21',
            timeout=1000
        )
        reader = csv.DictReader(io.StringIO(raw))
        objects = []
        for row in reader:
            try:
                qid = "QSO "+row["SDSS"].strip()
                mag = float(row["gmag"]) if row["gmag"].strip() else None
                z   = float(row["z"])    if row["z"].strip()    else None
                objects.append({
                    "id":qid,"catalog":"Quasar",
                    "ra":float(row["RA_ICRS"]),"dec":float(row["DE_ICRS"]),
                    "magnitude":mag,"type":"quasar","name":qid,
                    "description":f"Quasar z={z:.3f}" if z else "Quasar",
                })
            except: continue
        if objects: return objects
    except Exception as e:
        prog(f"    SDSS fehlgeschlagen: {e}, versuche VCV...")

    # Fallback: Veron-Cetty
    raw = _vizier_tap(
        f'SELECT TOP {top} Seq,RAJ2000,DEJ2000,Vmag,z FROM "VII/258/vv10" WHERE Vmag <= 20',
        timeout=1000
    )
    reader = csv.DictReader(io.StringIO(raw))
    objects = []
    for row in reader:
        try:
            qid = "QSO VV"+row["Seq"].strip()
            ra_s = row["RAJ2000"].strip().split()
            dec_s = row["DEJ2000"].strip().split()
            ra  = (float(ra_s[0])+float(ra_s[1])/60+float(ra_s[2])/3600)*15
            sgn = -1 if dec_s[0].startswith("-") else 1
            dec = sgn*(abs(float(dec_s[0]))+float(dec_s[1])/60+float(dec_s[2])/3600)
            mag = float(row["Vmag"]) if row["Vmag"].strip() else None
            z   = float(row["z"])   if row["z"].strip()   else None
            objects.append({
                "id":qid,"catalog":"Quasar",
                "ra":ra,"dec":dec,"magnitude":mag,"type":"quasar","name":qid,
                "description":f"Quasar z={z:.3f}" if z else "Quasar",
            })
        except: continue
    return objects


# ══════════════════════════════════════════════════════════════════════════════
# Messier eingebettet
# ══════════════════════════════════════════════════════════════════════════════

def _messier_builtin():
    # Vollstaendiger Messier-Katalog (110 Objekte) mit NGC/IC-Nummer,
    # Entfernung, Winkelgroesse und Besonderheiten. J2000-Koordinaten.
    rows = [
        ("M1",83.633,22.014,8.4,"nebula","Krebsnebel","Supernova-Ueberrest von SN 1054 · NGC 1952 · 6.500 Lj · 6'x4' · Pulsar im Zentrum (30 U/s)"),
        ("M2",323.363,-0.823,6.5,"cluster","Kugelsternhaufen","NGC 7089 · 55.000 Lj · 16' · ~150.000 Sterne, 13 Mrd Jahre alt · Wassermann"),
        ("M3",205.548,28.377,6.2,"cluster","Kugelsternhaufen","NGC 5272 · 34.000 Lj · 18' · ~500.000 Sterne, viele RR-Lyrae-Veraenderliche · Jagdhunde"),
        ("M4",245.897,-26.526,5.9,"cluster","Kugelsternhaufen","NGC 6121 · 7.200 Lj · naechster Kugelsternhaufen · 26' · Skorpion, nahe Antares"),
        ("M5",229.638,2.081,5.7,"cluster","Kugelsternhaufen","NGC 5904 · 25.000 Lj · 23' · einer der aeltesten (~13 Mrd Jahre) · Schlange"),
        ("M6",265.083,-32.259,4.2,"cluster","Schmetterlingshaufen","NGC 6405 · offener Haufen · 1.600 Lj · 25' · ~80 Sterne · Skorpion"),
        ("M7",268.460,-34.793,3.3,"cluster","Ptolemaeus-Haufen","NGC 6475 · offener Haufen · 980 Lj · 80' · schon von Ptolemaeus 130 n.Chr. erwaehnt"),
        ("M8",270.906,-24.387,6.0,"nebula","Lagunennebel","NGC 6523 · Emissionsnebel · 4.100 Lj · 90'x40' · aktive Sternentstehung · Schuetze"),
        ("M9",258.833,-18.516,7.8,"cluster","Kugelsternhaufen","NGC 6333 · 25.800 Lj · 12' · nahe am galaktischen Zentrum · Schlangentraeger"),
        ("M10",254.288,-4.100,6.6,"cluster","Kugelsternhaufen","NGC 6254 · 14.300 Lj · 20' · Schlangentraeger"),
        ("M11",282.766,-6.272,5.8,"cluster","Wildentenhaufen","NGC 6705 · offener Haufen · 6.200 Lj · ~2.900 Sterne, sehr dicht · Schild"),
        ("M12",251.809,-1.948,6.1,"cluster","Kugelsternhaufen","NGC 6218 · 15.700 Lj · 16' · locker konzentriert · Schlangentraeger"),
        ("M13",250.423,36.461,5.8,"cluster","Herkules-Haufen","NGC 6205 · 22.200 Lj · 20' · ~300.000 Sterne · Ziel der Arecibo-Botschaft 1974"),
        ("M14",264.401,-3.246,7.6,"cluster","Kugelsternhaufen","NGC 6402 · 30.000 Lj · 11' · Schlangentraeger"),
        ("M15",322.493,12.167,6.4,"cluster","Kugelsternhaufen","NGC 7078 · 33.600 Lj · 18' · kollabierter Kern, evtl. Schwarzes Loch · Pegasus"),
        ("M16",274.700,-13.809,6.4,"nebula","Adlernebel","NGC 6611 · Emissionsnebel+Haufen · 7.000 Lj · Saeulen der Schoepfung (Hubble 1995)"),
        ("M17",275.196,-16.171,6.0,"nebula","Omega-/Schwanennebel","NGC 6618 · Emissionsnebel · 5.500 Lj · 11' · eine der aktivsten Sternentstehungsregionen"),
        ("M18",274.318,-17.147,7.5,"cluster","Offener Haufen","NGC 6613 · 4.900 Lj · 9' · junger Haufen (~30 Mio Jahre) · Schuetze"),
        ("M19",255.659,-26.268,7.2,"cluster","Kugelsternhaufen","NGC 6273 · 28.700 Lj · 17' · am staerksten abgeplatteter Kugelsternhaufen"),
        ("M20",270.639,-23.023,6.3,"nebula","Trifidnebel","NGC 6514 · 5.200 Lj · 28' · Emissions- UND Reflexionsnebel, dreigeteilt durch Staubbaender"),
        ("M21",271.039,-22.500,5.9,"cluster","Offener Haufen","NGC 6531 · 4.250 Lj · 13' · sehr jung (~4,6 Mio Jahre) · Schuetze"),
        ("M22",279.100,-23.905,5.1,"cluster","Kugelsternhaufen","NGC 6656 · 10.600 Lj · 32' · einer der hellsten am Himmel · enthaelt planetarischen Nebel"),
        ("M23",269.239,-19.014,5.5,"cluster","Offener Haufen","NGC 6494 · 2.100 Lj · 27' · ~150 Sterne · Schuetze"),
        ("M24",274.988,-18.526,4.5,"cluster","Sagittarius-Sternwolke","IC 4715 · Milchstrassen-Sternwolke · 10.000 Lj tief · 90' · Fenster durch den Staub"),
        ("M25",277.944,-19.254,4.6,"cluster","Offener Haufen","IC 4725 · 2.000 Lj · 32' · enthaelt Cepheide U Sgr · Schuetze"),
        ("M26",281.320,-9.383,8.0,"cluster","Offener Haufen","NGC 6694 · 5.000 Lj · 15' · ~90 Sterne · Schild"),
        ("M27",299.901,22.721,7.6,"nebula","Hantelnebel","NGC 6853 · planetarischer Nebel · 1.360 Lj · 8'x6' · erster je entdeckter PN (1764)"),
        ("M28",276.137,-24.870,6.8,"cluster","Kugelsternhaufen","NGC 6626 · 18.000 Lj · 11' · erster Kugelhaufen mit Millisekundenpulsar · Schuetze"),
        ("M29",305.983,38.523,6.6,"cluster","Offener Haufen","NGC 6913 · 5.000 Lj · 7' · kleiner Haufen im Schwan"),
        ("M30",325.092,-23.180,7.1,"cluster","Kugelsternhaufen","NGC 7099 · 27.100 Lj · 12' · kollabierter Kern · retrograde Bahn · Steinbock"),
        ("M31",10.685,41.269,3.4,"galaxy","Andromeda-Galaxie","NGC 224 · Sb-Spirale · 2,5 Mio Lj · 3 Grad x1 Grad · ~1 Billion Sterne · kollidiert in ~4,5 Mrd Jahren mit der Milchstrasse"),
        ("M32",10.674,40.865,8.7,"galaxy","M31-Begleiter","NGC 221 · kompakte elliptische Zwerggalaxie (cE2) · 2,49 Mio Lj · vermutlich Rest einer groesseren Galaxie"),
        ("M33",23.462,30.660,5.7,"galaxy","Dreiecksgalaxie","NGC 598 · Sc-Spirale · 2,73 Mio Lj · 71'x42' · drittgroesste Galaxie der Lokalen Gruppe"),
        ("M34",40.518,42.747,5.5,"cluster","Offener Haufen","NGC 1039 · 1.400 Lj · 35' · ~400 Sterne, ~225 Mio Jahre · Perseus"),
        ("M35",92.264,24.333,5.3,"cluster","Offener Haufen","NGC 2168 · 2.800 Lj · 28' · daneben kompakter Haufen NGC 2158 · Zwillinge"),
        ("M36",82.136,34.133,6.3,"cluster","Offener Haufen","NGC 1960 · 4.100 Lj · 12' · junger Haufen (~25 Mio Jahre) · Fuhrmann"),
        ("M37",88.066,32.553,6.2,"cluster","Offener Haufen","NGC 2099 · 4.500 Lj · 24' · reichster Fuhrmann-Haufen, ~500 Sterne"),
        ("M38",82.184,35.833,7.4,"cluster","Offener Haufen","NGC 1912 · 4.200 Lj · 21' · Sterne in Pi-Form angeordnet · Fuhrmann"),
        ("M39",322.554,48.441,4.6,"cluster","Offener Haufen","NGC 7092 · 800 Lj · 32' · lockerer naher Haufen · Schwan"),
        ("M40",185.552,58.083,8.4,"double","Winnecke 4","Optischer Doppelstern (kein echtes Paar) · Messiers Verwechslung eines 'Nebels' · Grosser Baer"),
        ("M41",101.502,-20.757,4.5,"cluster","Offener Haufen","NGC 2287 · 2.300 Lj · 38' · ~100 Sterne · Grosser Hund, unter Sirius"),
        ("M42",83.822,-5.391,4.0,"nebula","Orionnebel","NGC 1976 · Emissionsnebel · 1.340 Lj · 85'x60' · naechste grosse Sternentstehungsregion · Trapez-Sterne"),
        ("M43",83.885,-5.267,9.0,"nebula","De-Mairan-Nebel","NGC 1982 · Teil des Orionnebel-Komplexes, durch Staubband getrennt"),
        ("M44",130.100,19.667,3.7,"cluster","Praesepe (Krippe)","NGC 2632 · offener Haufen · 580 Lj · 95' · ~1.000 Sterne · schon in der Antike bekannt · Krebs"),
        ("M45",56.750,24.117,1.6,"cluster","Plejaden","Sieben Schwestern · 440 Lj · 110' · ~1.000 Sterne, ~100 Mio Jahre · blaue Reflexionsnebel um die hellsten"),
        ("M46",115.441,-14.849,6.0,"cluster","Offener Haufen","NGC 2437 · 5.400 Lj · 27' · planetarischer Nebel NGC 2438 im Vordergrund · Puppis"),
        ("M47",114.147,-14.487,4.3,"cluster","Offener Haufen","NGC 2422 · 1.600 Lj · 30' · heller lockerer Haufen · Puppis"),
        ("M48",123.416,-5.800,5.5,"cluster","Offener Haufen","NGC 2548 · 2.500 Lj · 54' · Wasserschlange"),
        ("M49",187.445,7.999,8.4,"galaxy","Elliptische Galaxie","NGC 4472 · E4-Riesenelliptische · 56 Mio Lj · hellste Galaxie des Virgo-Haufens"),
        ("M50",105.699,-8.366,5.9,"cluster","Offener Haufen","NGC 2323 · 3.000 Lj · 16' · herzfoermige Anordnung · Einhorn"),
        ("M51",202.470,47.195,8.4,"galaxy","Strudelgalaxie","NGC 5194 · Sc-Spirale · 28 Mio Lj · 11'x7' · wechselwirkt mit NGC 5195 · erste Galaxie mit erkannter Spiralstruktur (1845)"),
        ("M52",351.196,61.593,7.3,"cluster","Offener Haufen","NGC 7654 · 4.600 Lj · 13' · nahe Blasennebel NGC 7635 · Kassiopeia"),
        ("M53",198.233,18.167,7.7,"cluster","Kugelsternhaufen","NGC 5024 · 58.000 Lj · 13' · weit im galaktischen Halo · Haar der Berenike"),
        ("M54",283.764,-30.480,7.7,"cluster","Kugelsternhaufen","NGC 6715 · 87.000 Lj · Kern der Sagittarius-Zwerggalaxie · erster extragalaktischer Kugelsternhaufen"),
        ("M55",294.997,-30.958,6.3,"cluster","Kugelsternhaufen","NGC 6809 · 17.600 Lj · 19' · locker aufgebaut · Schuetze"),
        ("M56",289.148,30.184,8.3,"cluster","Kugelsternhaufen","NGC 6779 · 32.900 Lj · 9' · metallarm, retrograde Bahn · Leier"),
        ("M57",283.396,33.029,9.0,"nebula","Ringnebel","NGC 6720 · planetarischer Nebel · 2.300 Lj · 1,4'x1' · Zentralstern Weisser Zwerg (15,8 mag) · Leier"),
        ("M58",189.431,11.818,9.8,"galaxy","Balkenspirale","NGC 4579 · SBb · 62 Mio Lj · eine der hellsten Balkenspiralen im Virgo-Haufen"),
        ("M59",190.508,11.647,9.8,"galaxy","Elliptische Galaxie","NGC 4621 · E5 · 60 Mio Lj · Virgo-Haufen"),
        ("M60",190.917,11.552,8.8,"galaxy","Elliptische Galaxie","NGC 4649 · E2 · 55 Mio Lj · SMBH mit ~4,5 Mrd Sonnenmassen · wechselwirkt mit NGC 4647"),
        ("M61",185.479,4.473,9.7,"galaxy","Spiralgalaxie","NGC 4303 · SABbc · 52 Mio Lj · viele Supernovae beobachtet (8+) · Virgo-Haufen"),
        ("M62",255.303,-30.114,6.5,"cluster","Kugelsternhaufen","NGC 6266 · 22.200 Lj · 15' · stark deformiert · viele Millisekundenpulsare · Schlangentraeger"),
        ("M63",198.956,42.029,8.6,"galaxy","Sonnenblumengalaxie","NGC 5055 · Sb-Flockulentspirale · 27 Mio Lj · 13'x7' · Jagdhunde"),
        ("M64",194.183,21.681,8.5,"galaxy","Schwarzauge-Galaxie","NGC 4826 · Sb · 24 Mio Lj · markantes Staubband · Gas rotiert aussen gegenlaeufig (Verschmelzungsrest)"),
        ("M65",169.733,13.092,9.3,"galaxy","Spiralgalaxie","NGC 3623 · Sa · 35 Mio Lj · Leo-Triplett mit M66 und NGC 3628"),
        ("M66",170.062,12.991,8.9,"galaxy","Spiralgalaxie","NGC 3627 · Sb · 36 Mio Lj · asymmetrische Arme durch Gezeitenwechselwirkung · Leo-Triplett"),
        ("M67",132.825,11.800,6.1,"cluster","Offener Haufen","NGC 2682 · 2.700 Lj · 30' · ~4 Mrd Jahre alt, einer der aeltesten offenen Haufen · Krebs"),
        ("M68",189.867,-26.744,7.8,"cluster","Kugelsternhaufen","NGC 4590 · 33.600 Lj · 12' · metallarm, im aeusseren Halo · Wasserschlange"),
        ("M69",277.846,-32.348,7.6,"cluster","Kugelsternhaufen","NGC 6637 · 29.700 Lj · 10' · ungewoehnlich metallreich · Schuetze"),
        ("M70",280.803,-32.292,7.9,"cluster","Kugelsternhaufen","NGC 6681 · 29.400 Lj · 8' · kollabierter Kern · Komet Hale-Bopp hier entdeckt (1995)"),
        ("M71",298.444,18.779,8.2,"cluster","Kugelsternhaufen","NGC 6838 · 13.000 Lj · 7' · lange als offener Haufen eingestuft (sehr locker) · Pfeil"),
        ("M72",313.365,-12.537,9.3,"cluster","Kugelsternhaufen","NGC 6981 · 55.400 Lj · 6' · einer der schwaechsten Messier-Kugelhaufen · Wassermann"),
        ("M73",314.750,-12.633,9.0,"cluster","Asterismus","NGC 6994 · nur 4 zufaellig nahe Sterne, kein echter Haufen · Wassermann"),
        ("M74",24.174,15.783,9.4,"galaxy","Phantom-Galaxie","NGC 628 · Sc face-on · 32 Mio Lj · 10'x9' · niedrigste Flaechenhelligkeit aller Messier-Objekte · Fische"),
        ("M75",301.520,-21.921,8.5,"cluster","Kugelsternhaufen","NGC 6864 · 67.500 Lj · 7' · sehr kompakt (Klasse I) · Schuetze"),
        ("M76",25.582,51.575,10.1,"nebula","Kleiner Hantelnebel","NGC 650/651 · planetarischer Nebel · 2.500 Lj · 3'x2' · schwaechstes Messier-Objekt · Perseus"),
        ("M77",40.670,-0.013,8.9,"galaxy","Seyfert-Galaxie","NGC 1068 · Sb · 47 Mio Lj · Prototyp der Seyfert-2-Galaxien · aktiver Kern mit SMBH (~15 Mio Sonnenmassen) · Walfisch"),
        ("M78",86.693,0.072,8.3,"nebula","Reflexionsnebel","NGC 2068 · 1.600 Lj · 8'x6' · hellster Reflexionsnebel am Himmel · Orion-Molekuelwolke"),
        ("M79",81.044,-24.524,7.7,"cluster","Kugelsternhaufen","NGC 1904 · 41.000 Lj · 9' · ungewoehnlich: am Winterhimmel, evtl. eingefangen von Zwerggalaxie · Hase"),
        ("M80",244.260,-22.976,7.3,"cluster","Kugelsternhaufen","NGC 6093 · 32.600 Lj · 10' · sehr dicht · Nova T Scorpii 1860 · Skorpion"),
        ("M81",148.888,69.065,6.9,"galaxy","Bode-Galaxie","NGC 3031 · Sb grand-design · 12 Mio Lj · 27'x14' · SMBH ~70 Mio Sonnenmassen · wechselwirkt mit M82"),
        ("M82",148.970,69.681,8.4,"galaxy","Zigarrengalaxie","NGC 3034 · Starburst edge-on · 12 Mio Lj · 5-fache Sternbildungsrate der Milchstrasse · Superwind aus dem Kern"),
        ("M83",204.254,-29.866,7.5,"galaxy","Suedliche Feuerradgalaxie","NGC 5236 · SBc face-on · 15 Mio Lj · 13'x12' · 6 beobachtete Supernovae · Wasserschlange"),
        ("M84",186.266,12.887,9.3,"galaxy","Linsengalaxie","NGC 4374 · E1/S0 · 60 Mio Lj · Teil der Markarian-Kette · Radiogalaxie mit Jets"),
        ("M85",182.072,18.191,9.2,"galaxy","Linsengalaxie","NGC 4382 · S0 · 60 Mio Lj · noerdlichstes Mitglied des Virgo-Haufens"),
        ("M86",186.550,12.946,9.2,"galaxy","Linsengalaxie","NGC 4406 · E3/S0 · 52 Mio Lj · blauverschoben (-244 km/s, faellt auf uns zu) · Markarian-Kette"),
        ("M87",187.706,12.391,8.6,"galaxy","Virgo A","NGC 4486 · cD-Riesenelliptische · 53,5 Mio Lj · SMBH M87* 6,5 Mrd Sonnenmassen (erstes Schwarzloch-Foto, EHT 2019) · 5.000-Lj-Jet · ~15.000 Kugelsternhaufen"),
        ("M88",187.996,14.420,9.6,"galaxy","Spiralgalaxie","NGC 4501 · Sb · 47 Mio Lj · eine der hellsten Virgo-Spiralen"),
        ("M89",188.916,12.556,9.8,"galaxy","Elliptische Galaxie","NGC 4552 · E0 · fast perfekt rund · 50 Mio Lj · Virgo-Haufen"),
        ("M90",189.208,13.163,9.5,"galaxy","Spiralgalaxie","NGC 4569 · Sab · 59 Mio Lj · blauverschoben · Gas durch Ram Pressure Stripping entzogen"),
        ("M91",188.862,14.497,10.2,"galaxy","Balkenspirale","NGC 4548 · SBb · 63 Mio Lj · lange 'verlorenes' Messier-Objekt (bis 1969) · Virgo-Haufen"),
        ("M92",259.281,43.136,6.4,"cluster","Kugelsternhaufen","NGC 6341 · 26.700 Lj · 14' · extrem metallarm, ~13 Mrd Jahre — fast so alt wie das Universum · Herkules"),
        ("M93",116.134,-23.856,6.0,"cluster","Offener Haufen","NGC 2447 · 3.600 Lj · 22' · Puppis"),
        ("M94",192.721,41.120,8.2,"galaxy","Krokodilsauge","NGC 4736 · Sab · 16 Mio Lj · heller Starburst-Ring um den Kern · Jagdhunde"),
        ("M95",160.990,11.704,9.7,"galaxy","Balkenspirale","NGC 3351 · SBb · 33 Mio Lj · Ringstruktur um den Balken · Leo-I-Gruppe"),
        ("M96",161.691,11.820,9.2,"galaxy","Spiralgalaxie","NGC 3368 · Sab · 31 Mio Lj · asymmetrisch, versetzter Kern · Leo-I-Gruppe"),
        ("M97",168.699,55.019,9.9,"nebula","Eulennebel","NGC 3587 · planetarischer Nebel · 2.000 Lj · 3' · zwei 'Augen' aus duennerem Gas · Grosser Baer"),
        ("M98",183.450,14.900,10.1,"galaxy","Spiralgalaxie","NGC 4192 · Sb fast edge-on · 44 Mio Lj · blauverschoben (-142 km/s) · Virgo-Haufen"),
        ("M99",184.707,14.416,9.9,"galaxy","Spiralgalaxie","NGC 4254 · Sc face-on · 50 Mio Lj · asymmetrischer Arm durch Begegnung · Virgo-Haufen"),
        ("M100",185.729,15.822,9.3,"galaxy","Spiralgalaxie","NGC 4321 · Sc grand-design face-on · 55 Mio Lj · 7'x6' · eine der hellsten Virgo-Spiralen · viele Supernovae"),
        ("M101",210.802,54.349,7.9,"galaxy","Feuerradgalaxie","NGC 5457 · Sc face-on · 21 Mio Lj · 29' · 170.000 Lj Durchmesser — fast doppelt so gross wie die Milchstrasse"),
        ("M102",226.623,55.763,9.9,"galaxy","Spindelgalaxie","NGC 5866 · S0 edge-on · 50 Mio Lj · markantes Staubband · Identitaet historisch umstritten (evtl. = M101) · Drache"),
        ("M103",23.337,60.655,7.4,"cluster","Offener Haufen","NGC 581 · 8.500 Lj · 6' · letztes von Messier selbst katalogisiertes Objekt · Kassiopeia"),
        ("M104",189.998,-11.623,8.0,"galaxy","Sombrero-Galaxie","NGC 4594 · Sa edge-on · 31 Mio Lj · 9'x4' · markantes Staubband · SMBH ~1 Mrd Sonnenmassen · ~2.000 Kugelsternhaufen"),
        ("M105",161.957,12.582,9.3,"galaxy","Elliptische Galaxie","NGC 3379 · E1 · 32 Mio Lj · Standardobjekt fuer Ellipsen-Photometrie · Leo-I-Gruppe"),
        ("M106",184.740,47.304,8.4,"galaxy","Spiralgalaxie","NGC 4258 · Sb · 24 Mio Lj · Wasser-Maser im Kern → praezise Distanzmessung · aktiver Kern (Seyfert 1.9) · anomale Arme"),
        ("M107",248.133,-13.054,7.8,"cluster","Kugelsternhaufen","NGC 6171 · 20.900 Lj · 13' · locker, mit Staubbaendern · Schlangentraeger"),
        ("M108",167.879,55.674,10.0,"galaxy","Spiralgalaxie","NGC 3556 · Sc edge-on · 46 Mio Lj · staubreich, kein klarer Kern sichtbar · nahe Eulennebel M97"),
        ("M109",179.400,53.374,9.8,"galaxy","Balkenspirale","NGC 3992 · SBc · 84 Mio Lj · entferntestes Messier-Objekt · Grosser Baer"),
        ("M110",10.092,41.685,8.9,"galaxy","M31-Begleiter","NGC 205 · elliptische Zwerggalaxie (dE5) · 2,7 Mio Lj · ungewoehnlich: junge blaue Sterne im Zentrum"),
    ]
    return [{"id":r[0],"catalog":"Messier","ra":r[1],"dec":r[2],"magnitude":r[3],
             "type":r[4],"name":r[5],"description":r[6]} for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
# Bildverarbeitung
# ══════════════════════════════════════════════════════════════════════════════

def load_image(path):
    p = str(path).lower()
    wcs_info = None

    if p.endswith((".fit",".fits")):
        if not HAS_ASTROPY:
            raise ImportError("pip install astropy benoetigt fuer FITS")
        hdul = fits.open(str(path), memmap=False)
        img_hdu = next((h for h in hdul if h.data is not None and len(np.shape(h.data))>=2), None)
        if img_hdu is None:
            hdul.close(); raise ValueError("Kein Bilddaten-HDU gefunden")
        data = np.array(img_hdu.data, dtype=np.float32)
        if data.ndim == 3: data = data.mean(axis=0)
        elif data.ndim > 3: data = data[0,0]
        try:
            wcs_obj = WCS(img_hdu.header, naxis=2)
            h2, w2 = data.shape
            sky_c = wcs_obj.pixel_to_world(w2/2, h2/2)
            sky_r = wcs_obj.pixel_to_world(w2/2+1, h2/2)
            sky_n = wcs_obj.pixel_to_world(w2/2, h2/2+1)
            dra  = float(sky_r.ra.deg)-float(sky_c.ra.deg)
            ddec = float(sky_r.dec.deg)-float(sky_c.dec.deg)
            scale = math.sqrt((dra*math.cos(math.radians(float(sky_c.dec.deg))))**2+ddec**2)
            d_ra_n  = float(sky_n.ra.deg)-float(sky_c.ra.deg)
            d_dec_n = float(sky_n.dec.deg)-float(sky_c.dec.deg)
            rot = math.degrees(math.atan2(-d_ra_n*math.cos(math.radians(float(sky_c.dec.deg))),d_dec_n))
            wcs_info = {"ra_center":float(sky_c.ra.deg),"dec_center":float(sky_c.dec.deg),
                        "scale_deg_per_px":scale,"rotation_deg":rot,"img_w":w2,"img_h":h2,"source":"FITS"}
        except: pass
        hdul.close()
    else:
        if not HAS_PIL: raise ImportError("pip install pillow benoetigt")
        img = Image.open(str(path)).convert("L")
        data = np.array(img, dtype=np.float32)

    p_lo, p_hi = np.percentile(data, 0.5), np.percentile(data, 99.5)
    if p_hi > p_lo: data = np.clip((data-p_lo)/(p_hi-p_lo), 0.0, 1.0)
    return data, wcs_info


def extract_stars(img, max_stars=300):
    if HAS_SCIPY:
        sm = ndimage.gaussian_filter(img, sigma=2.0)
    else:
        sm = img
    bg = _tile_bg(sm)
    res = np.clip(sm-bg, 0, None)
    vals = res[res>0]
    if len(vals)==0: return []
    noise = np.median(np.abs(vals-np.median(vals)))*1.4826
    thr = max(noise*5.0, res.max()*0.005)
    if HAS_SCIPY:
        lmax = ndimage.maximum_filter(res, size=9)
        ys, xs = np.where((res==lmax)&(res>thr))
    else:
        ys, xs = _simple_peaks(res, thr)
    if len(xs)==0: return []
    fl = res[ys,xs]
    order = np.argsort(fl)[::-1]
    xs,ys,fl = xs[order],ys[order],fl[order]
    h,w = img.shape
    m = max(5, min(15, min(w,h)//50))  # adaptive margin for small images
    mask = (xs>m)&(xs<w-m)&(ys>m)&(ys<h-m)
    xs,ys,fl = xs[mask],ys[mask],fl[mask]
    # Tighter separation for dense fields (globular clusters)
    n_raw = len(xs)
    density = n_raw / max((w-2*m)*(h-2*m), 1)
    min_dist = 6 if density > 1.0/400 else 10
    kept = _remove_close(xs,ys,fl,min_dist=min_dist)[:max_stars]
    return [{"x":_centroid(res,int(x),int(y),9)[0],"y":_centroid(res,int(x),int(y),9)[1],"flux":float(f)}
            for x,y,f in kept]


def _tile_bg(img, tile=64):
    h,w = img.shape; bg=np.empty_like(img)
    for y0 in range(0,h,tile):
        for x0 in range(0,w,tile):
            bg[y0:y0+tile,x0:x0+tile]=np.percentile(img[y0:y0+tile,x0:x0+tile],25)
    return bg

def _simple_peaks(img, thr):
    h,w=img.shape; ys,xs=[],[]
    for y in range(5,h-5,5):
        for x in range(5,w-5,5):
            v=img[y,x]
            if v>=thr and v>=img[max(0,y-5):y+6,max(0,x-5):x+6].max():
                ys.append(y);xs.append(x)
    return np.array(ys,dtype=int),np.array(xs,dtype=int)

def _remove_close(xs,ys,fl,min_dist):
    kept,used=[],np.zeros(len(xs),dtype=bool)
    for i in range(len(xs)):
        if used[i]: continue
        kept.append((xs[i],ys[i],fl[i]))
        dx=(xs[i+1:]-xs[i]).astype(float); dy=(ys[i+1:]-ys[i]).astype(float)
        used[i+1:][dx*dx+dy*dy<min_dist*min_dist]=True
    return kept

def _centroid(img,x,y,box=9):
    h,w=img.shape; b=box//2
    x0,x1=max(0,x-b),min(w,x+b+1); y0,y1=max(0,y-b),min(h,y+b+1)
    patch=img[y0:y1,x0:x1]; total=patch.sum()
    if total==0: return float(x),float(y)
    cx=(patch.sum(axis=0)@np.arange(x0,x1,dtype=float))/total
    cy=(patch.sum(axis=1)@np.arange(y0,y1,dtype=float))/total
    return float(cx),float(cy)


# ══════════════════════════════════════════════════════════════════════════════
# TAN-Projektion WCS
# ══════════════════════════════════════════════════════════════════════════════

class TanWCS:
    """
    Vollstaendige TAN-WCS Implementierung.
    Unterstuetzt sowohl einfache (Skala+Rotation) als auch
    vollstaendige CD-Matrix-Darstellung (wie ASTAP sie liefert).
    """
    def __init__(self, ra0, dec0, scale, rot, cx, cy,
                 cd11=None, cd12=None, cd21=None, cd22=None,
                 crval1=None, crval2=None, crpix1=None, crpix2=None):
        self.ra0   = ra0
        self.dec0  = math.radians(dec0)
        self.scale = scale
        self.rot   = math.radians(rot)
        self.cx    = cx
        self.cy    = cy
        # Vollstaendige CD-Matrix (bevorzugt, genauer)
        self.has_cd = (cd11 is not None)
        if self.has_cd:
            self.cd11, self.cd12 = cd11, cd12
            self.cd21, self.cd22 = cd21, cd22
            # Inverse CD-Matrix fuer world->pixel
            det = cd11 * cd22 - cd12 * cd21
            self.icd11 =  cd22 / det
            self.icd12 = -cd12 / det
            self.icd21 = -cd21 / det
            self.icd22 =  cd11 / det
            # Referenzpunkt (FITS 1-basiert -> 0-basiert)
            self.crval1 = crval1
            self.crval2 = crval2
            self.crpix1_0 = crpix1 - 1.0
            self.crpix2_0 = crpix2 - 1.0

    def world_to_pixel(self, ra: float, dec: float):
        """RA/Dec (Grad) -> Pixelkoordinaten (0-basiert)."""
        if self.has_cd:
            return self._world_to_pixel_cd(ra, dec)
        return self._world_to_pixel_simple(ra, dec)

    def _world_to_pixel_cd(self, ra: float, dec: float):
        """Exakte Umrechnung mit CD-Matrix und CRPIX."""
        # 1. Weltkoordinaten -> TAN-Projektionsebene (xi, eta) in Grad
        dra   = math.radians(ra - self.crval1)
        dec_r = math.radians(dec)
        d0    = math.radians(self.crval2)
        denom = math.sin(dec_r)*math.sin(d0) + math.cos(dec_r)*math.cos(d0)*math.cos(dra)
        if abs(denom) < 1e-12:
            return -99999., -99999.
        xi  = math.degrees(math.cos(dec_r) * math.sin(dra) / denom)
        eta = math.degrees(
            (math.sin(dec_r)*math.cos(d0) - math.cos(dec_r)*math.sin(d0)*math.cos(dra)) / denom
        )
        # 2. (xi, eta) -> Pixel via inverse CD-Matrix
        dx = self.icd11 * xi + self.icd12 * eta
        dy = self.icd21 * xi + self.icd22 * eta
        # 3. Referenzpixel addieren (0-basiert)
        # WICHTIG: CD2_2 ist in FITS-Konvention bereits negativ (Y von unten),
        # dy ist daher schon invertiert -> direkt addieren, NICHT nochmal invertieren
        px = self.crpix1_0 + dx
        py = self.crpix2_0 + dy
        return px, py

    def _world_to_pixel_simple(self, ra: float, dec: float):
        """Vereinfachte Umrechnung mit Skala+Rotation (fuer FITS-Header)."""
        dra = math.radians(ra - self.ra0)
        dr  = math.radians(dec)
        d0  = self.dec0
        denom = math.sin(dr)*math.sin(d0) + math.cos(dr)*math.cos(d0)*math.cos(dra)
        if abs(denom) < 1e-12:
            return -99999., -99999.
        xi  = math.degrees(math.cos(dr)*math.sin(dra) / denom)
        eta = math.degrees(
            (math.sin(dr)*math.cos(d0) - math.cos(dr)*math.sin(d0)*math.cos(dra)) / denom
        )
        cr, sr = math.cos(self.rot), math.sin(self.rot)
        dx = xi * cr + eta * sr
        dy = -xi * sr + eta * cr
        return self.cx + dx / self.scale, self.cy - dy / self.scale


# ══════════════════════════════════════════════════════════════════════════════
# Haupt-Solver
# ══════════════════════════════════════════════════════════════════════════════

def solve_field(img_path, conn, mag_limit=19.0, active_catalogs=None,
                manual_wcs=None, astap_exe=None, progress_cb=None,
                ra_hint=None, dec_hint=None) -> Dict:
    def prog(m):
        if progress_cb: progress_cb(m)
        else: print(m)

    prog("Bild laden...")
    img, wcs_info = load_image(img_path)
    h, w = img.shape
    prog(f"Groesse: {w}x{h} px")

    if manual_wcs:
        wcs_info = manual_wcs
        prog("Manuelle WCS-Koordinaten verwendet")

    # ASTAP plate solving (wenn kein WCS und ASTAP verfuegbar).
    # Mit Ziel-Hint (z.B. "M8") sucht ASTAP nur im 30-Grad-Umkreis statt am
    # ganzen Himmel — rettet dichte Milchstrassenfelder, wo Blind-Solve scheitert.
    if not wcs_info and astap_exe:
        if ra_hint is not None and dec_hint is not None:
            prog(f"ASTAP plate solving mit Positions-Hint RA={ra_hint:.3f} Dec={dec_hint:.3f} ...")
        else:
            prog(f"ASTAP plate solving gestartet ({astap_exe})...")
        wcs_info = astap_solve(img_path, astap_exe,
                               ra_hint=ra_hint, dec_hint=dec_hint, progress_cb=prog)

    if not wcs_info:
        return {
            "status": "no_wcs",
            "message": "Keine Koordinaten gefunden. Optionen:\n"
                       "1. ASTAP installieren (automatische Erkennung)\n"
                       "2. Koordinaten manuell eingeben\n"
                       "3. FITS-Datei mit WCS-Header verwenden",
            "image_size_px": {"w":w,"h":h}, "objects": [],
        }

    prog(f"WCS: RA={wcs_info['ra_center']:.4f} Dec={wcs_info['dec_center']:.4f} "
         f"Skala={wcs_info['scale_deg_per_px']*3600:.2f}\"/px Quelle={wcs_info.get('source','?')}")

    # TanWCS mit voller CD-Matrix wenn vorhanden (z.B. von ASTAP)
    if wcs_info.get("has_full_cd"):
        wcs = TanWCS(
            ra0=wcs_info["ra_center"], dec0=wcs_info["dec_center"],
            scale=wcs_info["scale_deg_per_px"], rot=wcs_info.get("rotation_deg", 0.),
            cx=w/2, cy=h/2,
            cd11=wcs_info["cd11"], cd12=wcs_info["cd12"],
            cd21=wcs_info["cd21"], cd22=wcs_info["cd22"],
            crval1=wcs_info["crval1"], crval2=wcs_info["crval2"],
            crpix1=wcs_info["crpix1"], crpix2=wcs_info["crpix2"],
        )
        prog("WCS: CD-Matrix Modus (ASTAP, pixelgenaue Positionierung)")
    else:
        wcs = TanWCS(wcs_info["ra_center"], wcs_info["dec_center"],
                     wcs_info["scale_deg_per_px"], wcs_info.get("rotation_deg", 0.),
                     w/2, h/2)

    fw = wcs_info["scale_deg_per_px"]*w
    fh = wcs_info["scale_deg_per_px"]*h
    radius = math.sqrt(fw**2+fh**2)/2*1.05

    prog(f"Sterne extrahieren...")
    stars = extract_stars(img, max_stars=300)
    prog(f"{len(stars)} Quellen")

    prog(f"Katalog abfragen (Radius {radius:.2f} Grad, mag <= {mag_limit})...")
    cat_objs = query_region(conn, wcs_info["ra_center"], wcs_info["dec_center"],
                            radius, mag_limit, active_catalogs)
    prog(f"{len(cat_objs)} Treffer")

    result_objs = []
    for obj in cat_objs:
        px, py = wcs.world_to_pixel(obj["ra"], obj["dec"])
        if -20<=px<=w+20 and -20<=py<=h+20:
            result_objs.append({
                "id":obj["id"],"catalog":obj["catalog"],"type":obj["type"],
                "ra":round(obj["ra"],5),"dec":round(obj["dec"],5),
                "magnitude":obj["magnitude"],"description":obj["description"],
                "x":round(px,1),"y":round(py,1),
                "x_frac":round(px/w,4),"y_frac":round(py/h,4),
            })

    prog(f"{len(result_objs)} Objekte im Bildfeld")

    def fmt_ra(d):
        h_=d/15; hh=int(h_); mm=int((h_-hh)*60); ss=((h_-hh)*60-mm)*60
        return f"{hh:02d}h {mm:02d}m {ss:05.2f}s"
    def fmt_dec(d):
        s='+' if d>=0 else '-'; d=abs(d)
        dd=int(d); mm=int((d-dd)*60); ss=((d-dd)*60-mm)*60
        return f"{s}{dd:02d} {mm:02d}' {ss:04.1f}\""

    return {
        "status": "solved",
        "wcs_info": wcs_info,
        "wcs_source": wcs_info.get("source","?"),
        "field_center": {"ra":fmt_ra(wcs_info["ra_center"]),"dec":fmt_dec(wcs_info["dec_center"]),
                         "ra_deg":round(wcs_info["ra_center"],5),"dec_deg":round(wcs_info["dec_center"],5)},
        "field_size_arcmin": {"w":round(fw*60,1),"h":round(fh*60,1)},
        "scale_arcsec_per_px": round(wcs_info["scale_deg_per_px"]*3600,3),
        "rotation_deg": round(wcs_info.get("rotation_deg",0.),2),
        "image_size_px": {"w":w,"h":h},
        "stars_found": len(stars),
        "stars": [{"x": round(s["x"], 1), "y": round(s["y"], 1),
                   "flux": round(float(s["flux"]), 4),
                   "x_frac": round(s["x"]/w, 4), "y_frac": round(s["y"]/h, 4)}
                  for s in stars],
        "objects": result_objs,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 3D-Export: Online-Anreicherung (SIMBAD TAP + VizieR Gaia DR3)
# ══════════════════════════════════════════════════════════════════════════════

def _comoving_dist_mpc(z: float) -> Optional[float]:
    """Komovierender Abstand in Mpc. Flaches ΛCDM (H0=67.74, Ωm=0.30, ΩΛ=0.70)."""
    if not z or z <= 0:
        return None
    DH = 299792.458 / 67.74          # Hubble-Distanz in Mpc
    Om, Ol, n = 0.30, 0.70, 200
    dz = z / n
    s  = sum(
        (0.5 if (i == 0 or i == n) else 1.0)
        / math.sqrt(Om * (1.0 + i * dz) ** 3 + Ol)
        for i in range(n + 1)
    )
    return DH * s * dz


def _simbad_batch(pairs: list, progress_cb=None) -> dict:
    """
    SIMBAD TAP (POST): Rotverschiebung, Radialgeschwindigkeit, Morphologie,
    Spektraltyp und Parallaxe für eine Liste von (simbad_name, obj_id) Paaren.
    Gibt {obj_id: {redshift_z, distance_mpc, distance_ly, morph_type, ...}} zurück.
    """
    def prog(m):
        if progress_cb: progress_cb(m)

    URL      = "https://simbad.u-strasbg.fr/simbad/sim-tap/sync"
    MPC2LY   = 3_261_563.8
    BATCH    = 50
    results  = {}

    for idx in range(0, len(pairs), BATCH):
        chunk  = pairs[idx : idx + BATCH]
        id_map = {}
        for sname, oid in chunk:
            id_map[sname.strip().upper()] = oid

        id_list = ", ".join(
            "'{}'".format(s.replace("'", "''")) for s in id_map
        )
        adql = (
            "SELECT i.id, b.rvz_redshift, b.rvz_radvel, "
            "b.morph_type, b.sp_type, b.plx_value, b.plx_err "
            "FROM basic AS b JOIN ident AS i ON b.oid = i.oidref "
            "WHERE i.id IN ({})".format(id_list)
        )
        data = urllib.parse.urlencode({
            "REQUEST": "doQuery", "LANG": "ADQL",
            "FORMAT": "csv",      "QUERY": adql,
        }).encode("utf-8")
        req = urllib.request.Request(
            URL, data=data,
            headers={"User-Agent": "AstroPlateSolver/1.0",
                     "Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                raw = r.read().decode("utf-8", errors="ignore")
        except Exception as e:
            prog("[Enrich] SIMBAD Batch {} Fehler: {}".format(idx // BATCH + 1, e))
            continue

        reader = csv.DictReader(io.StringIO(raw))
        for row in reader:
            sid = row.get("id", "").strip().upper()
            oid = id_map.get(sid)
            if not oid or oid in results:
                continue

            def _f(col, r=row):
                v = r.get(col, "").strip()
                try:   return float(v) if v else None
                except: return None

            entry = {}

            z  = _f("rvz_redshift")
            rv = _f("rvz_radvel")

            if z is not None and z > 0:
                entry["redshift_z"]      = round(z, 6)
                entry["distance_source"] = "simbad_redshift"
                dc = _comoving_dist_mpc(z)
                if dc:
                    entry["distance_mpc"] = round(dc, 4)
                    entry["distance_ly"]  = round(dc * MPC2LY)
            elif rv is not None and rv != 0:
                # Radialgeschwindigkeit → z (relativistisch: z = sqrt((1+β)/(1-β)) - 1)
                beta = rv / 299792.458
                z2 = math.sqrt((1 + beta) / (1 - beta)) - 1 if abs(beta) < 1 else None
                if z2 is None: z2 = 0
                if z2 > 0:
                    entry["redshift_z"]      = round(z2, 6)
                    entry["distance_source"] = "simbad_radvel"
                    dc = _comoving_dist_mpc(z2)
                    if dc:
                        entry["distance_mpc"] = round(dc, 4)
                        entry["distance_ly"]  = round(dc * MPC2LY)
                entry["radvel_kms"] = round(rv, 2)
            elif rv is not None:
                entry["radvel_kms"] = round(rv, 2)

            for col, key in [("morph_type", "morph_type"), ("sp_type", "spectral_type")]:
                v = row.get(col, "").strip()
                if v:
                    entry[key] = v

            plx = _f("plx_value")
            if plx and plx > 0:
                entry["parallax_mas"] = round(plx, 4)
                plx_e = _f("plx_err")
                if plx_e:
                    entry["parallax_err_mas"] = round(plx_e, 4)
                # Nur zuverlässige Parallaxen für Distanzberechnung
                if plx > 0.01:
                    dist_pc = 1000.0 / plx
                    entry["distance_pc"]     = round(dist_pc, 2)
                    entry["distance_ly"]     = round(dist_pc * 3.26156, 2)
                    entry["distance_source"] = "parallax_simbad"

            if entry:
                results[oid] = entry

        done = min(idx + BATCH, len(pairs))
        prog("[Enrich] SIMBAD {}/{} ({} Treffer)".format(done, len(pairs), len(results)))

    return results


def _gaia_dr3_batch(gaia_objects: list, progress_cb=None) -> dict:
    """
    VizieR TAP – Gaia DR3: Parallaxe, Eigenbewegung, Radialgeschwindigkeit,
    effektive Temperatur für eine Liste von Gaia-Objekten.
    Gibt {obj_id: {parallax_mas, distance_ly, pmra_mas_yr, ...}} zurück.
    """
    def prog(m):
        if progress_cb: progress_cb(m)

    BATCH   = 150
    results = {}

    for idx in range(0, len(gaia_objects), BATCH):
        chunk  = gaia_objects[idx : idx + BATCH]
        id_map = {}
        for o in chunk:
            m = re.match(r"Gaia\s+(\d+)", o.get("id", ""))
            if m:
                id_map[m.group(1)] = o["id"]
        if not id_map:
            continue

        ids_sql = ",".join(id_map.keys())
        adql = (
            'SELECT Source, Plx, e_Plx, pmRA, e_pmRA, pmDE, e_pmDE, RV, e_RV, Teff, BPmag, RPmag '
            'FROM "I/355/gaiadr3" WHERE Source IN ({})'.format(ids_sql)
        )
        try:
            raw = _vizier_tap(adql, timeout=90)
        except Exception as e:
            prog("[Enrich] Gaia Batch {} Fehler: {}".format(idx // BATCH + 1, e))
            continue

        reader = csv.DictReader(io.StringIO(raw))
        for row in reader:
            sid = row.get("Source", "").strip()
            oid = id_map.get(sid)
            if not oid:
                continue

            def _f(col, r=row):
                v = r.get(col, "").strip()
                try:   return float(v) if v else None
                except: return None

            entry = {}
            plx = _f("Plx")
            if plx is not None:
                entry["parallax_mas"] = round(plx, 4)
                plx_e = _f("e_Plx")
                if plx_e is not None:
                    entry["parallax_err_mas"] = round(plx_e, 4)
                # Parallaxe > 0.05 mas → Distanz zuverlässig (< ~20 kpc)
                if plx > 0.05:
                    dist_pc = 1000.0 / plx
                    entry["distance_pc"]     = round(dist_pc, 2)
                    entry["distance_ly"]     = round(dist_pc * 3.26156, 2)
                    entry["distance_source"] = "parallax_gaia_dr3"

            for col, key in [
                ("pmRA",  "pmra_mas_yr"),  ("e_pmRA",  "pmra_err_mas_yr"),
                ("pmDE",  "pmdec_mas_yr"), ("e_pmDE",  "pmdec_err_mas_yr"),
            ]:
                v = _f(col)
                if v is not None:
                    entry[key] = round(v, 4)

            rv = _f("RV")
            if rv is not None:
                entry["radvel_kms"] = round(rv, 2)

            teff = _f("Teff")
            if teff:
                entry["teff_k"] = round(teff, 1)

            bp = _f("BPmag")
            rp = _f("RPmag")
            if bp is not None:
                entry["bp_mag"] = round(bp, 4)
            if rp is not None:
                entry["rp_mag"] = round(rp, 4)
            if bp is not None and rp is not None:
                entry["bp_rp"] = round(bp - rp, 4)

            if entry:
                results[oid] = entry

        done = min(idx + BATCH, len(gaia_objects))
        prog("[Enrich] Gaia DR3 {}/{} ({} Parallaxen)".format(
            done, len(gaia_objects), len(results)))

    return results


def enrich_for_3d(objects: list, progress_cb=None) -> dict:
    """
    Reichert Objekte mit Online-Katalogdaten für den 3D-Export an.
    Quellen: SIMBAD TAP (NGC/IC/Messier/PGC/Tycho-2/Quasare),
             VizieR Gaia DR3 (Parallaxe, Kinematik).
    Gibt {obj_id: {redshift_z, distance_ly, parallax_mas, morph_type, ...}} zurück.
    """
    def prog(m):
        if progress_cb: progress_cb(m)
        else: print(m)

    results    = {}
    named_objs = []   # NGC, IC, Messier, PGC, Tycho-2
    gaia_objs  = []   # Gaia DR3 → VizieR Parallaxen
    qso_objs   = []   # Quasare ohne z → SIMBAD

    for o in objects:
        cat = o.get("catalog", "")
        if cat in ("NGC", "IC", "Messier", "PGC", "Tycho-2"):
            named_objs.append(o)
        elif cat == "Gaia DR3":
            gaia_objs.append(o)
        elif cat == "Quasar":
            desc = o.get("description", "")
            # Quasare ohne bekannte z auch via SIMBAD suchen
            if not re.search(r"z\s*=\s*[\d.]+", desc):
                qso_objs.append(o)

    prog("[Enrich] Start: {} benannte, {} Gaia, {} Quasare ohne z".format(
        len(named_objs), len(gaia_objs), len(qso_objs)))

    # ── 1. SIMBAD: benannte Deep-Sky-Objekte + Quasare ohne z ─────────────────
    simbad_pairs = []
    for o in named_objs:
        cat, oid = o.get("catalog", ""), o.get("id", "")
        sname = oid
        if cat == "Messier":
            m = re.match(r"M(\d+)", oid)
            if m:
                sname = "M {}".format(m.group(1))
        simbad_pairs.append((sname, oid))

    for o in qso_objs:
        oid   = o.get("id", "")
        # SDSS-Format: "QSO J123456.78+654321.0" → SIMBAD kennt "SDSS J123456.78+654321.0"
        sname = re.sub(r"^QSO\s+", "SDSS ", oid) if oid.startswith("QSO J") else oid
        simbad_pairs.append((sname, oid))

    if simbad_pairs:
        prog("[Enrich] SIMBAD: {} Objekte…".format(len(simbad_pairs)))
        try:
            sres = _simbad_batch(simbad_pairs, prog)
            results.update(sres)
            prog("[Enrich] SIMBAD fertig: {} Ergebnisse".format(len(sres)))
        except Exception as e:
            prog("[Enrich] SIMBAD Fehler: {}".format(e))

    # ── 2. VizieR Gaia DR3: Parallaxen (max. 500 hellste Sterne) ─────────────
    if gaia_objs:
        sorted_gaia = sorted(gaia_objs, key=lambda x: (x.get("magnitude") or 99))[:500]
        prog("[Enrich] Gaia DR3: {} Sterne (von {})…".format(
            len(sorted_gaia), len(gaia_objs)))
        try:
            gres = _gaia_dr3_batch(sorted_gaia, prog)
            results.update(gres)
            prog("[Enrich] Gaia fertig: {} Parallaxen".format(len(gres)))
        except Exception as e:
            prog("[Enrich] Gaia Fehler: {}".format(e))

    prog("[Enrich] Komplett: {} Objekte angereichert".format(len(results)))
    return results
