"""
Online-Lookup fuer Galaxien-Morphologie (Hubble-Typen).
Quellen: SIMBAD (CDS) via sim-script. Lokaler Cache in sqlite.
"""
import json
import re
import ssl
import sqlite3
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

CACHE_DB = Path(__file__).parent / "morph_cache.db"

_USER_AGENT = "AstroPlateSolver/1.0 (morph lookup)"

# ── SSL-Kontext (gleiches Problem/Loesung wie in catalog_dl.py) ─────────────
# Ohne expliziten SSL-Kontext schlaegt urlopen() auf manchen Windows-Python-
# Installationen mit "CERTIFICATE_VERIFY_FAILED" fehl. Das wurde bisher in
# _simbad_query() durch ein stilles "except Exception: return ''" verschluckt
# UND zusaetzlich permanent als "kein Treffer" gecacht -> einmal aufgetreten,
# blieb JEDE Galaxie fuer immer "0 Treffer", auch nach einem Fix, weil der
# negative Cache-Eintrag nie erneut geprueft wurde.
def _build_ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    try:
        return ssl.create_default_context()
    except Exception:
        return None

_SSL_CTX = _build_ssl_context()


def _cache_conn():
    c = sqlite3.connect(str(CACHE_DB))
    c.execute("""CREATE TABLE IF NOT EXISTS morph(
        name TEXT PRIMARY KEY,
        morph TEXT,
        source TEXT,
        ts INTEGER DEFAULT (strftime('%s','now'))
    )""")
    c.commit()
    return c


def _normalize(name: str) -> str:
    """Normalisiere fuer Cache-Key."""
    return re.sub(r"\s+", " ", name.strip().upper())


def _simbad_query(name: str, timeout=12) -> tuple:
    """
    Fragt SIMBAD via TAP (ADQL) ab. Robust und standardkonform.
    Rueckgabe: (morph_type_string, error_message_or_None)
    Ein leerer morph_type_string MIT error=None bedeutet "SIMBAD erreicht,
    aber kein Morphologie-Typ hinterlegt" (legitim leer).
    Ein error != None bedeutet "Anfrage ist fehlgeschlagen" (Netzwerk/SSL/
    Timeout) - das darf NICHT permanent gecacht werden, da es beim naechsten
    Versuch (z.B. nach einem Fix oder wenn SIMBAD wieder erreichbar ist)
    funktionieren koennte.
    """
    adql = (
        "SELECT TOP 1 basic.morph_type "
        "FROM basic JOIN ident ON basic.oid = ident.oidref "
        f"WHERE id = '{name.replace(chr(39), chr(39)*2)}'"
    )
    params = {
        "REQUEST": "doQuery",
        "LANG":    "ADQL",
        "FORMAT":  "csv",
        "QUERY":   adql,
    }
    # Zwei Endpunkte als Fallback, wie bei catalog_dl.py
    endpoints = [
        "https://simbad.cds.unistra.fr/simbad/sim-tap/sync",
        "https://simbad.u-strasbg.fr/simbad/sim-tap/sync",
    ]
    last_err = None
    for base_url in endpoints:
        url = base_url + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
                text = resp.read().decode("utf-8", errors="ignore")
        except urllib.error.URLError as e:
            # Bei Zertifikatsproblemen: letzter Ausweg ohne Verifizierung
            # (oeffentliche, rein lesende SIMBAD-Abfrage - unkritisch).
            if "CERTIFICATE_VERIFY_FAILED" in str(e):
                try:
                    ctx_insecure = ssl.create_default_context()
                    ctx_insecure.check_hostname = False
                    ctx_insecure.verify_mode = ssl.CERT_NONE
                    with urllib.request.urlopen(req, timeout=timeout, context=ctx_insecure) as resp:
                        text = resp.read().decode("utf-8", errors="ignore")
                except Exception as e2:
                    last_err = f"{base_url}: {e2}"
                    continue
            else:
                last_err = f"{base_url}: {e}"
                continue
        except Exception as e:
            last_err = f"{base_url}: {type(e).__name__}: {e}"
            continue

        # Erfolgreich verbunden -> Antwort parsen
        lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
        if len(lines) < 2:
            return "", None  # erreicht, aber kein Eintrag fuer dieses Objekt
        val = lines[1].strip().strip('"').strip()
        if val in ("", "~", "NULL"):
            return "", None
        return val, None

    # Alle Endpunkte fehlgeschlagen -> echter Fehler, NICHT als "kein Typ" werten
    return "", (last_err or "SIMBAD nicht erreichbar")


def _ra_dec_to_sdss_name(ra_deg: float, dec_deg: float) -> str:
    """
    Konvertiert RA/Dec (Grad) in das SIMBAD-verstaendliche SDSS-J-Format:
    'SDSS Jhhmmss.ss+ddmmss.s'
    Das ist das einzige Format mit dem SIMBAD SDSS-Objekte kennt.
    Die numerische SDSS ObjID ('SDSS 1237661...') ist KEIN Himmelsname
    und wird von SIMBAD nicht erkannt.
    """
    ra_h = ra_deg / 15.0
    ra_hh = int(ra_h)
    ra_mm = int((ra_h - ra_hh) * 60)
    ra_ss = ((ra_h - ra_hh) * 60 - ra_mm) * 60
    sign = '+' if dec_deg >= 0 else '-'
    d = abs(dec_deg)
    dec_dd = int(d)
    dec_mm = int((d - dec_dd) * 60)
    dec_ss = ((d - dec_dd) * 60 - dec_mm) * 60
    return (f"SDSS J{ra_hh:02d}{ra_mm:02d}{ra_ss:05.2f}"
            f"{sign}{dec_dd:02d}{dec_mm:02d}{dec_ss:04.1f}")


def _name_variants(name: str, ra: float = None, dec: float = None):
    """
    Erzeugt SIMBAD-verstaendliche Varianten fuer einen Objektnamen.
    ra, dec (in Grad): falls angegeben, wird ein SDSS-J-Name berechnet,
    der von SIMBAD tatsaechlich erkannt wird. Unverzichtbar fuer SDSS-
    Objekte, da SIMBAD die numerische SDSS-ObjID nicht kennt.
    """
    n = name.strip()
    out = []

    # SDSS numerische ObjID -> in SDSS-J-Namen umwandeln (hoechste Prioritaet)
    if re.match(r"^SDSS\s+\d{10,}$", n, re.I):
        if ra is not None and dec is not None:
            out.append(_ra_dec_to_sdss_name(ra, dec))
        # Numerische ID selbst ist bei SIMBAD unbekannt -> nicht hinzufuegen
    else:
        out.append(n)

    # Messier
    m = re.match(r"^M\s*(\d+)$", n, re.I)
    if m:
        out.append(f"M {m.group(1)}")
        out.append(f"NGC {m.group(1)}")
    # NGC / IC
    m = re.match(r"^(NGC|IC)\s*0*(\d+.*)$", n, re.I)
    if m:
        out.append(f"{m.group(1).upper()} {m.group(2)}")
    # PGC
    m = re.match(r"^PGC\s*0*(\d+)$", n, re.I)
    if m:
        out.append(f"PGC {m.group(1)}")
        out.append(f"LEDA {m.group(1)}")
    # QSO / quasar IDs
    m = re.match(r"^QSO\s+(.+)$", n, re.I)
    if m:
        out.append(f"QSO {m.group(1)}")

    # Fallback: SDSS-J aus RA/Dec auch fuer andere Objekte ohne klaren Namen
    if ra is not None and dec is not None and not any('J' in v for v in out):
        out.append(_ra_dec_to_sdss_name(ra, dec))

    return list(dict.fromkeys(v for v in out if v))  # de-dupe, kein Leerstring


def lookup(name: str, conn=None, ra: float = None, dec: float = None) -> dict:
    """
    Liefert {'name', 'morph', 'source', 'cached', 'error'} fuer einen Namen.
    ra, dec (Grad): falls angegeben, wird fuer SDSS-Objekte ein SDSS-J-Name
    generiert, den SIMBAD tatsaechlich kennt.
    """
    close_after = False
    if conn is None:
        conn = _cache_conn()
        close_after = True
    key = _normalize(name)
    row = conn.execute("SELECT morph, source FROM morph WHERE name=?", (key,)).fetchone()
    if row is not None:
        morph, source = row
        if close_after:
            conn.close()
        return {"name": name, "morph": morph or "", "source": source or "cache",
                "cached": True, "error": None}

    morph = ""
    source = ""
    last_error = None
    for v in _name_variants(name, ra=ra, dec=dec):
        r, err = _simbad_query(v)
        if err:
            last_error = err
            continue
        if r:
            morph = r
            source = "SIMBAD"
            break

    if morph == "" and last_error is not None and source == "":
        if close_after:
            conn.close()
        return {"name": name, "morph": "", "source": "error", "cached": False,
                "error": last_error}

    conn.execute("INSERT OR REPLACE INTO morph(name,morph,source) VALUES(?,?,?)",
                 (key, morph, source or "none"))
    conn.commit()
    if close_after:
        conn.close()
    return {"name": name, "morph": morph, "source": source or "none",
            "cached": False, "error": None}


def clear_error_cache():
    """
    Entfernt alle Cache-Eintraege mit source='none', die VOR dem SSL-Fix
    faelschlich als 'kein Treffer' gespeichert wurden, obwohl die Anfrage
    eigentlich an einem Verbindungsfehler gescheitert war (alte Versionen
    cachten Fehler permanent). Nach diesem Aufruf werden diese Namen beim
    naechsten Lookup erneut versucht statt fuer immer leer zu bleiben.
    Echte SIMBAD-Treffer (source='SIMBAD') bleiben unberuehrt.
    """
    conn = _cache_conn()
    cur = conn.execute("DELETE FROM morph WHERE source='none' OR source=''")
    n = cur.rowcount
    conn.commit()
    conn.close()
    return n


def lookup_batch(names, progress_cb=None, max_queries=60):
    """
    Fragt mehrere Namen ab. Respektiert Cache. Begrenzt Online-Anfragen pro Aufruf.
    names: Liste von Strings ODER Dicts {"name": ..., "ra": ..., "dec": ...}
    """
    conn = _cache_conn()
    out = []
    queries = 0
    n_errors = 0
    for i, entry in enumerate(names):
        if isinstance(entry, dict):
            n = str(entry.get("name", ""))
            ra  = entry.get("ra")
            dec = entry.get("dec")
        else:
            n = str(entry)
            ra = dec = None

        key = _normalize(n)
        row = conn.execute("SELECT morph, source FROM morph WHERE name=?", (key,)).fetchone()
        if row is not None:
            m, s = row
            out.append({"name": n, "morph": m or "", "source": s or "cache", "cached": True})
            continue
        if queries >= max_queries:
            out.append({"name": n, "morph": "", "source": "skipped", "cached": False})
            continue
        queries += 1
        if progress_cb:
            progress_cb(f"SIMBAD {queries}/{min(max_queries,len(names))}: {n}")
        res = lookup(n, conn, ra=ra, dec=dec)
        if res.get("error"):
            n_errors += 1
            if progress_cb and n_errors <= 3:
                progress_cb(f"  Fehler bei '{n}': {res['error']}")
        out.append(res)
    conn.close()
    if n_errors and progress_cb:
        progress_cb(f"SIMBAD: {n_errors}/{queries} Anfragen fehlgeschlagen "
                     f"(Netzwerk/SSL) - diese werden beim naechsten Versuch erneut geprueft")
    return out
