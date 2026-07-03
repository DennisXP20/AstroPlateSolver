"""Katalog-Downloader v11 - asu-csv mit 5 VizieR Mirrors + SSL-Fix"""
import urllib.request, urllib.parse, urllib.error
import csv, io, gzip, time, math, ssl
from typing import List, Dict

HEADERS = {"User-Agent":"Mozilla/5.0 AstroSolver/10","Accept":"*/*"}
SIMBAD_TAP = [
    "https://simbad.cds.unistra.fr/simbad/sim-tap/sync",
    "https://simbad.u-strasbg.fr/simbad/sim-tap/sync",
]
VIZIER_MIRRORS = [
    "vizier.cds.unistra.fr",   # Primär: Straßburg (CDS)
    "vizier.u-strasbg.fr",     # Alias des gleichen Hosts, oft identischer Erfolg/Misserfolg
    "vizier.nao.ac.jp",        # Japan – Fallback
    "vizier.hia.nrc.ca",       # Kanada – Fallback
    "vizier.iucaa.in",         # Indien – oft am unzuverlässigsten, daher zuletzt
]

# ── SSL-Kontext ──────────────────────────────────────────────────────────────
# Auf manchen Windows-Python-Installationen (insbesondere portable/embedded
# Varianten ohne System-Zertifikatsspeicher) schlägt die TLS-Verifizierung
# mit "CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate"
# fehl, obwohl die Verbindung an sich funktioniert. Lösung: zuerst certifi's
# eigenes, mitgeliefertes CA-Bundle verwenden (zuverlässigste Methode), als
# Fallback das System-Zertifikatsdepot via ssl.create_default_context().
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


def _http(url, data=None, timeout=90):
    req = urllib.request.Request(url, data=data, headers=HEADERS)
    if data: req.add_header("Content-Type","application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
            raw = r.read()
    except urllib.error.URLError as e:
        # Letzter Ausweg bei hartnäckigen Zertifikatsproblemen: Verifizierung
        # deaktivieren. Das ist weniger sicher, aber für öffentliche,
        # rein lesende astronomische Kataloge (keine sensiblen Daten, keine
        # Anmeldedaten) ein akzeptabler Kompromiss, wenn sonst gar nichts
        # funktioniert (z.B. fehlendes/veraltetes CA-Bundle auf dem System).
        if "CERTIFICATE_VERIFY_FAILED" in str(e):
            ctx_insecure = ssl.create_default_context()
            ctx_insecure.check_hostname = False
            ctx_insecure.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, timeout=timeout, context=ctx_insecure) as r:
                raw = r.read()
        else:
            raise
    if raw[:2] == b'\x1f\x8b': raw = gzip.decompress(raw)
    return raw

def _text(url, data=None, timeout=90):
    return _http(url, data, timeout).decode("utf-8", errors="replace")

def _is_csv(t):
    h = t.strip()[:300].lower()
    return "<html" not in h and "<!doc" not in h and len(t.strip()) > 50

def _asu(catalog, cols, filt_col, filt_val, max_rows=50000, timeout=25):
    """VizieR asu-csv GET - versucht alle Mirror, mit kurzem Timeout pro Mirror
    damit ein einzelner nicht erreichbarer Mirror nicht alles blockiert."""
    params = urllib.parse.urlencode({
        "-source":  catalog,
        "-out":     ",".join(cols),
        "-out.max": str(max_rows),
        "-oc.form": "dec",
        filt_col:   filt_val,
    })
    errs = []
    for host in VIZIER_MIRRORS:
        url = f"https://{host}/viz-bin/asu-csv?{params}"
        try:
            raw = _text(url, timeout=timeout)
            if _is_csv(raw): return raw, host
            errs.append(f"{host}: kein CSV ({len(raw)}B)")
        except Exception as e:
            errs.append(f"{host}: {e}")
    raise RuntimeError("Alle Mirror fehlgeschlagen: " + " | ".join(errs))

def _load_import_catalogs():
    """import_catalogs.py als Modul laden (gemeinsame CSV-Parser für lokale Dateien)."""
    import importlib.util
    from pathlib import Path
    p = Path(__file__).parent / "import_catalogs.py"
    spec = importlib.util.spec_from_file_location("import_catalogs", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _download_vizier_bulk(source, cols, dest, prog=None, timeout=1800, min_bytes=1_000_000):
    """Lädt eine komplette VizieR-Tabelle (asu-tsv, -out.max=999999) streamend in
    eine Datei — identisches Format wie ein manueller VizieR-Browser-Export,
    sodass import_catalogs sie unverändert parsen kann. Probiert alle Mirror.
    Rückgabe: True bei Erfolg."""
    import os
    params = [("-oc.form", "dec"), ("-out.max", "999999"), ("-out.add", "_RAJ,_DEJ"),
              ("-source", source)] + [("-out", c) for c in cols]
    qs = urllib.parse.urlencode(params)
    errs = []
    for host in VIZIER_MIRRORS:
        url = f"https://{host}/viz-bin/asu-tsv?{qs}"
        tmp = str(dest) + ".part"
        try:
            if prog: prog(f"    {host}: lade {source} komplett (das kann einige Minuten dauern)...")
            req = urllib.request.Request(url, headers=HEADERS)
            ctx = _SSL_CTX
            try:
                resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
            except urllib.error.URLError as e:
                if "CERTIFICATE_VERIFY_FAILED" in str(e):
                    ctx = ssl.create_default_context()
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                    resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
                else:
                    raise
            got, last = 0, 0
            with resp, open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk: break
                    f.write(chunk); got += len(chunk)
                    if prog and got - last > 20 * 1024 * 1024:
                        last = got
                        prog(f"      ... {got // 1048576} MB")
            size = os.path.getsize(tmp)
            with open(tmp, "rb") as f:
                head = f.read(400).decode("utf-8", "replace").lower()
            if size >= min_bytes and "<html" not in head and "<!doc" not in head:
                os.replace(tmp, str(dest))
                if prog: prog(f"    OK: {size // 1048576} MB -> {os.path.basename(str(dest))}")
                return True
            errs.append(f"{host}: ungueltige Antwort ({size} B)")
            try: os.remove(tmp)
            except OSError: pass
        except Exception as e:
            errs.append(f"{host}: {e}")
            try: os.remove(tmp)
            except OSError: pass
    if prog: prog("    Bulk-Download fehlgeschlagen: " + " | ".join(errs[:3]))
    return False


def _tap(url, adql, timeout=60):
    data = urllib.parse.urlencode({"REQUEST":"doQuery","LANG":"ADQL","FORMAT":"csv","QUERY":adql}).encode()
    raw = _text(url, data=data, timeout=timeout)
    if not _is_csv(raw): raise RuntimeError(f"Kein CSV: {raw[:200]}")
    return raw

def _simbad_tap(adql, timeout=60):
    """Versucht alle SIMBAD-TAP-Mirror der Reihe nach.
    Gibt CSV-Text zurück. Leere Ergebnismenge (nur Header) → leerer String ''.
    Echte Fehler (HTTP-Fehler, kein CSV) → RuntimeError."""
    errs = []
    for url in SIMBAD_TAP:
        try:
            data = urllib.parse.urlencode({"REQUEST":"doQuery","LANG":"ADQL","FORMAT":"csv","QUERY":adql}).encode()
            raw = _text(url, data=data, timeout=timeout)
            # SIMBAD gibt bei 0 Ergebnissen nur den Header zurück (<50 Zeichen)
            # Das ist kein Fehler — wir geben den Text trotzdem zurück (leere CSV).
            h = raw.strip()[:300].lower()
            if "<html" in h or "<!doc" in h or "error" in h[:80]:
                errs.append(f"{url}: HTML/Fehler-Antwort ({len(raw)}B)")
                continue
            return raw  # Auch leere CSV (nur Header) ist gültig
        except Exception as e:
            errs.append(f"{url}: {e}")
    raise RuntimeError("SIMBAD TAP fehlgeschlagen: " + " | ".join(errs))

def _retry(fn, n=3, delay=4, prog=None):
    last=None
    for i in range(n):
        try: return fn()
        except Exception as e:
            last=e
            if prog and i<n-1: prog(f"    Retry {i+2}/{n}: {e}")
            if i<n-1: time.sleep(delay)
    raise last

def _insert(conn, objects):
    conn.executemany("INSERT OR REPLACE INTO objects (id,catalog,ra,dec,magnitude,type,name,description) VALUES (:id,:catalog,:ra,:dec,:magnitude,:type,:name,:description)", objects)
    conn.commit()

# ── NGC/IC ─────────────────────────────────────────────────────────────────────
def download_ngcic(conn, prog=None):
    for url in ["https://raw.githubusercontent.com/mattiaverga/OpenNGC/master/database_files/NGC.csv",
                "https://github.com/mattiaverga/OpenNGC/raw/master/database_files/NGC.csv"]:
        try:
            raw = _text(url, timeout=30)
            if _is_csv(raw): break
        except: pass
    else: raise RuntimeError("OpenNGC nicht erreichbar")
    tm={"G":"galaxy","GGroup":"galaxy","GPair":"galaxy","GTrpl":"galaxy","GClstr":"cluster",
        "OC":"cluster","OCl":"cluster","GC":"cluster","*Ass":"cluster",
        "PN":"nebula","SNR":"nebula","EN":"nebula","RN":"nebula","HII":"nebula",
        "Dup":"star","*":"star","D*":"star","**":"star"}
    objs=[]
    for row in csv.DictReader(io.StringIO(raw), delimiter=";"):
        try:
            name=row.get("Name","").strip()
            if not name: continue
            rp=row["RA"].strip().split(":")
            dp=row["Dec"].strip(); sign=-1 if dp.startswith("-") else 1
            dp=dp.lstrip("+-").split(":")
            ra=(float(rp[0])+float(rp[1])/60+float(rp[2])/3600)*15
            dec=sign*(float(dp[0])+float(dp[1])/60+float(dp[2])/3600)
            try: mag=float(row.get("V-Mag") or row.get("B-Mag") or "x")
            except: mag=None
            t=tm.get(row.get("Type",""),"unknown")
            cat="NGC" if name.startswith("N") else "IC"
            nice=name.replace("NGC","NGC ").replace("IC","IC ").strip()
            objs.append({"id":nice,"catalog":cat,"ra":ra,"dec":dec,"magnitude":mag,"type":t,"name":nice,"description":row.get("Common names","").strip() or t})
        except: pass
    _insert(conn, objs); return len(objs)

# ── Sterne ─────────────────────────────────────────────────────────────────────
_BS=[(-1.46,"Sirius","HR 2491",101.2875,-16.7161),(-0.72,"Canopus","HR 2326",95.988,-52.6957),
     (-0.01,"Rigil Kent","HR 5459",219.9007,-60.835),(-0.04,"Arcturus","HR 5340",213.9154,19.1822),
     (0.03,"Vega","HR 7001",279.2347,38.7836),(0.08,"Capella","HR 1708",79.1725,45.998),
     (0.12,"Rigel","HR 1713",78.6344,-8.2016),(0.34,"Procyon","HR 2943",114.825,5.2275),
     (0.42,"Betelgeuse","HR 2061",84.0534,-1.2019),(0.50,"Achernar","HR 472",24.4284,-57.2367),
     (0.61,"Hadar","HR 5267",206.8853,-60.373),(0.71,"Altair","HR 7557",297.6958,8.8683),
     (0.77,"Acrux","HR 4730",186.6496,-63.0991),(0.77,"Aldebaran","HR 1457",68.98,16.5093),
     (0.85,"Antares","HR 6134",247.3519,-26.432),(0.96,"Spica","HR 5056",201.2983,-11.1614),
     (1.04,"Pollux","HR 2990",116.3289,28.0264),(1.14,"Fomalhaut","HR 8728",344.4126,-29.6223),
     (1.16,"Mimosa","HR 4853",191.9305,-59.6884),(1.25,"Deneb","HR 7924",310.3579,45.2803),
     (1.25,"Regulus","HR 3982",152.0929,11.9672),(1.36,"Adhara","HR 2618",111.0238,-28.9722),
     (1.50,"Castor","HR 2891",113.6494,31.8886),(1.58,"Shaula","HR 6527",263.4022,-37.1038),
     (1.62,"Bellatrix","HR 1790",81.2828,6.3497),(1.64,"Elnath","HR 1791",81.5731,28.6074),
     (1.70,"Alnilam","HR 1903",84.0534,-1.2019),(1.75,"Alnair","HR 8425",332.0583,-46.961),
     (1.78,"Alioth","HR 4905",193.5073,55.9598),(1.79,"Alnitak","HR 1948",85.1897,-1.9426),
     (1.80,"Mirfak","HR 1017",51.0808,49.8612),(1.81,"Dubhe","HR 4301",165.932,61.7508),
     (1.84,"Wezen","HR 2693",107.0977,-26.3932),(1.86,"Avior","HR 3307",125.6283,-59.5092),
     (1.88,"Alkaid","HR 5191",206.8853,49.3133),(1.93,"Menkalinan","HR 2088",89.8821,44.9474),
     (1.97,"Atria","HR 6217",247.3519,-68.6822),(1.97,"Polaris","HR 424",37.9546,89.2642),
     (1.98,"Alhena","HR 2421",99.4278,16.3993),(1.99,"Peacock","HR 7790",306.4119,-56.735),
     (2.00,"Mirzam","HR 2294",95.6752,-17.9559),(2.02,"Alphard","HR 3748",141.8968,-8.6586),
     (2.04,"Hamal","HR 617",31.7933,23.4625),(2.06,"Nunki","HR 7121",283.8163,-26.2967),
     (2.06,"Menkent","HR 5288",211.5935,-36.3697),(2.14,"Denebola","HR 4534",177.2649,14.572),
     (2.20,"Sadr","HR 7796",305.5572,40.2567),(2.21,"Naos","HR 3165",120.8963,-40.0031),
     (2.23,"Mirach","HR 337",17.4331,35.6205),(2.23,"Caph","HR 21",2.2945,59.1498),
     (2.25,"Mintaka","HR 1851",83.0017,0.2981),(2.28,"Kaus Aust","HR 6879",276.043,-34.3843),
     (2.29,"Alpheratz","HR 15",2.0963,29.0903),(2.30,"Saiph","HR 2004",86.9391,-9.6697),
     (2.37,"Merak","HR 4295",165.4601,56.3824),(2.38,"Enif","HR 8308",326.0464,9.875),
     (2.39,"Kappa Sco","HR 6580",265.6213,-39.0258),(2.40,"Phecda","HR 4554",178.4579,53.6948),
     (2.42,"Scheat","HR 8775",345.9437,28.0826),(2.43,"Sabik","HR 6378",257.5946,-15.7249),
     (2.47,"Zosma","HR 4357",168.527,20.5237),(2.54,"Muphrid","HR 5235",208.6713,18.3976),
     (2.55,"Zuben El","HR 5531",222.7197,-16.0416),(2.56,"Porrima","HR 4825",190.4153,-1.4494),
     (2.62,"Almach","HR 603",30.9749,42.3297),(2.64,"Sheratan","HR 553",28.6604,20.8081),
     (2.69,"Kaus Med","HR 6859",274.4088,-29.8281),(2.74,"Mira","HR 1488",34.8368,-2.9778),
     (2.75,"Kaus Bor","HR 6913",276.9927,-25.4217),(2.80,"Gienah","HR 4662",183.7863,-17.542),
     (2.83,"Sadalsuud","HR 8232",322.8897,-5.5712),(2.84,"Sadalmelik","HR 7990",311.5534,-0.3198),
     (2.87,"Alcyone","HR 1165",56.8711,24.1052),(2.89,"Alderamin","HR 8162",319.6445,62.5856),
     (2.95,"Algieba","HR 3981",154.9932,19.8415),(2.97,"Aspidiske","HR 3699",145.2883,-59.2758),
     (3.00,"Ankaa","HR 99",6.5708,-42.3062),(3.03,"Iota Sco","HR 6615",264.8625,-34.2927),
     (3.05,"Cor Caroli","HR 4915",194.0065,38.3183),(3.12,"Zuben Esc","HR 5685",229.252,-9.3828),
     (3.33,"Meissa","HR 2159",83.7846,9.9341),(3.34,"Albireo","HR 7417",292.6803,27.9597),
     (3.44,"Alphecca","HR 5793",233.6720,26.7146),(3.47,"Sabik","HR 6378",257.5946,-15.7249),
     (3.49,"Rasalhague","HR 6556",263.7336,12.5600),(3.52,"Eltanin","HR 6705",269.1517,51.4889),
     (3.57,"Kochab","HR 5563",222.6762,74.1555),(3.60,"Pherkad","HR 5735",230.1823,71.8340),
     (3.65,"Alderamin","HR 8162",319.6445,62.5856),(3.73,"Alfirk","HR 8238",323.2980,70.5607),
     (3.77,"Caph","HR 21",2.2945,59.1498),(3.80,"Ruchbah","HR 542",27.3662,60.2353),
     (3.97,"Segin","HR 1022",51.5408,63.6701),(3.99,"Achird","HR 219",12.2768,57.8150),
]

def _builtin_stars():
    return [{"id":hr,"catalog":"Tycho-2","ra":ra,"dec":dec,"magnitude":mag,
             "type":"star","name":name,"description":f"Stern V={mag:.2f}"}
            for mag,name,hr,ra,dec in _BS]

def download_stars(conn, prog=None, mag_limit=9.0):
    bi=_builtin_stars(); _insert(conn,bi)
    if prog: prog(f"    Eingebettete helle Sterne: {len(bi)}")
    extra=[]
    for cat,cols,cc,label,parser in [
        ("I/239/hip_main",["HIP","RArad","DErad","Vmag"],"Vmag","Hipparcos","hip"),
        ("V/50/catalog",["HR","RAJ2000","DEJ2000","Vmag","Name"],"Vmag","Yale BSC","yale"),
    ]:
        if extra: break
        try:
            if prog: prog(f"    VizieR asu-csv {label}...")
            raw,host=_asu(cat,cols,cc,f"..{mag_limit}",max_rows=30000)
            if prog: prog(f"    {host} OK")
            for row in csv.DictReader(io.StringIO(raw)):
                try:
                    vs=str(row.get("Vmag","")).strip()
                    try: vmag=float(vs)
                    except: continue
                    if vmag>mag_limit: continue
                    if parser=="hip":
                        sid=f"HIP {str(row.get('HIP','')).strip()}"
                        ra=math.degrees(float(row["RArad"])); dec=math.degrees(float(row["DErad"])); nm=sid
                    else:
                        hr=str(row.get("HR","")).strip(); sid=f"HR {hr}"
                        rs=str(row.get("RAJ2000","")).strip().split()
                        ds=str(row.get("DEJ2000","")).strip().split()
                        ra=(float(rs[0])+float(rs[1])/60+float(rs[2])/3600)*15 if len(rs)==3 else float(rs[0])*15
                        sgn=-1 if ds[0].startswith("-") else 1
                        dec=sgn*(abs(float(ds[0]))+float(ds[1])/60+float(ds[2])/3600) if len(ds)==3 else float(ds[0])
                        nm=str(row.get("Name","")).strip() or sid
                    extra.append({"id":sid,"catalog":"Tycho-2","ra":ra,"dec":dec,"magnitude":vmag,"type":"star","name":nm,"description":f"Stern V={vmag:.1f}"})
                except: pass
            if prog: prog(f"    {label}: {len(extra)}")
        except Exception as e:
            if prog: prog(f"    {label} fehlgeschlagen: {e}")
    if extra: _insert(conn,extra)
    return conn.execute("SELECT COUNT(*) FROM objects WHERE catalog='Tycho-2'").fetchone()[0]

# ── PGC ────────────────────────────────────────────────────────────────────────
def download_pgc(conn, prog=None, mag_limit=22.0):
    """
    PGC/HyperLEDA: vollstaendiger Katalog (VII/237, ~983.000 Galaxien).
    Wichtig: VII/237 hat KEINE Magnitude-Spalte ('Bmag' existiert dort nicht —
    genau daran scheiterte der fruehere Download). Stattdessen wird der komplette
    Katalog als VizieR-Export in pgc.csv geladen (inkl. Morphologie-Typ MType
    und Groesse logD25) und ueber import_catalogs.import_pgc importiert.
    Eine bereits vorhandene pgc.csv (z.B. manueller Download) wird direkt genutzt.
    """
    from pathlib import Path
    base = Path(__file__).parent
    f = base / "pgc.csv"

    # 1) pgc.csv beschaffen (vorhandene Datei hat Vorrang)
    if not f.exists() or f.stat().st_size < 1_000_000:
        if prog: prog("    Keine lokale pgc.csv – lade VII/237 (HyperLEDA) komplett...")
        _download_vizier_bulk("VII/237/pgc",
                              ["PGC", "OType", "MType", "logD25", "logR25"],
                              f, prog=prog, timeout=1800, min_bytes=5_000_000)
    else:
        if prog: prog(f"    Nutze vorhandene pgc.csv ({f.stat().st_size // 1048576} MB)")

    # 2) Import über den gemeinsamen Parser
    if f.exists() and f.stat().st_size >= 1_000_000:
        try:
            ic = _load_import_catalogs()
            n = ic.import_pgc(conn)
            if n > 0:
                return n
        except Exception as e:
            if prog: prog(f"    pgc.csv-Import fehlgeschlagen: {e}")

    # 3) Notfall-Fallback: kleiner SIMBAD-Satz (besser als gar nichts)
    objs=[]
    try:
        if prog: prog("    Fallback: SIMBAD TAP Galaxien...")
        raw=_simbad_tap(
            f"SELECT TOP 50000 main_id,ra,dec,V FROM basic JOIN flux ON oidref=oid WHERE otype='G' AND filter='V' AND flux < {mag_limit}",60)
        for row in csv.DictReader(io.StringIO(raw)):
            try:
                nm=str(row.get("main_id","")).strip()
                if not nm: continue
                try: mag=float(str(row.get("V","")).strip())
                except: mag=None
                objs.append({"id":nm,"catalog":"PGC","ra":float(row["ra"]),"dec":float(row["dec"]),"magnitude":mag,"type":"galaxy","name":nm,"description":"SIMBAD Galaxie"})
            except: pass
        if prog: prog(f"    SIMBAD: {len(objs)}")
    except Exception as e:
        if prog: prog(f"    SIMBAD fehlgeschlagen: {e}")
    if objs: _insert(conn,objs)
    return len(objs)

# ── Gaia DR3 ───────────────────────────────────────────────────────────────────
def download_gaia(conn, prog=None, mag_limit=16.0):
    objs=[]
    # Gaia DR3: mag<=16 → ~3 Mio Sterne (TOP 2000000 sinnvoll; ESA-TAP-Limit beachten)
    try:
        if prog: prog(f"    ESA Gaia TAP (mag<{mag_limit}, TOP 2000000)...")
        raw=_tap("https://gea.esac.esa.int/tap-server/tap/sync",
                 f"SELECT TOP 2000000 source_id,ra,dec,phot_g_mean_mag FROM gaiadr3.gaia_source WHERE phot_g_mean_mag <= {mag_limit}",600)
        for row in csv.DictReader(io.StringIO(raw)):
            try:
                gid="Gaia "+str(row.get("source_id","")).strip()
                try: mag=float(str(row.get("phot_g_mean_mag","")).strip())
                except: mag=None
                objs.append({"id":gid,"catalog":"Gaia DR3","ra":float(row["ra"]),"dec":float(row["dec"]),"magnitude":mag,"type":"star","name":gid,"description":f"Gaia G={mag:.2f}" if mag else "Gaia"})
            except: pass
        if prog: prog(f"    ESA Gaia: {len(objs)}")
    except Exception as e:
        if prog: prog(f"    ESA Gaia fehlgeschlagen: {e}")
    if not objs:
        try:
            if prog: prog("    VizieR asu-csv Gaia DR3...")
            raw,host=_asu("I/355/gaiadr3",["Source","RA_ICRS","DE_ICRS","Gmag"],"Gmag",f"..{mag_limit}",max_rows=2000000,timeout=600)
            for row in csv.DictReader(io.StringIO(raw)):
                try:
                    gid="Gaia "+str(row.get("Source","")).strip()
                    try: mag=float(str(row.get("Gmag","")).strip())
                    except: mag=None
                    objs.append({"id":gid,"catalog":"Gaia DR3","ra":float(str(row["RA_ICRS"]).strip()),"dec":float(str(row["DE_ICRS"]).strip()),"magnitude":mag,"type":"star","name":gid,"description":f"Gaia G={mag:.2f}" if mag else "Gaia"})
                except: pass
            if prog: prog(f"    VizieR Gaia ({host}): {len(objs)}")
        except Exception as e:
            if prog: prog(f"    VizieR Gaia fehlgeschlagen: {e}")
    if objs: _insert(conn,objs)
    return len(objs)

# ── Quasare ────────────────────────────────────────────────────────────────────
def download_quasars(conn, prog=None):
    """
    Quasare (SDSS DR16Q, Lyke+ 2020 — der aktuellste vollstaendige SDSS-Quasarkatalog):
      1. Lokale quasars.csv (750.000+ Objekte, ugriz+GALEX UV+WISE IR+Extinktion)
         über import_catalogs.import_quasars() – kein Netzwerk nötig, volle Daten.
      2. Kein lokales File → kompletter Download von VII/289/dr16q nach quasars.csv
         (ein Request, ~100-350 MB) und Import wie in 1. Damit bleiben alle
         Quasar-Statistiken (SED, UV/IR-Abdeckung, Extinktion) voll funktionsfähig.
      3. Kleiner Online-Fallback (200k, nur Basisdaten) als letzte Rettung.
    """
    from pathlib import Path
    base = Path(__file__).parent
    qf = base / "quasars.csv"

    def _try_local():
        try:
            ic = _load_import_catalogs()
            if prog: prog("    Prüfe lokale quasars.csv (SDSS DR16Q)...")
            n = ic.import_quasars(conn)
            if n > 0:
                if prog: prog(f"    quasars.csv: {n:,} Quasare importiert "
                              f"(mit GALEX UV/WISE IR/Extinktion wo vorhanden)")
            return n
        except Exception as e:
            if prog: prog(f"    quasars.csv nicht nutzbar: {e}")
            return 0

    # ── 1. Lokale quasars.csv (bevorzugt – volle SDSS DR16Q Daten) ───────────
    if qf.exists() and qf.stat().st_size > 1_000_000:
        n = _try_local()
        if n > 0: return n

    # ── 2. Komplett-Download nach quasars.csv, dann Import ───────────────────
    if prog: prog("    Keine lokale quasars.csv – lade SDSS DR16Q komplett (VII/289)...")
    ok = _download_vizier_bulk(
        "VII/289/dr16q",
        ["SDSS", "z", "umag", "gmag", "rmag", "imag", "zmag",
         "Extu", "Extg", "Extr", "Exti", "Extz",
         "FFUV", "FNUV", "FW1", "FW2"],
        qf, prog=prog, timeout=1800, min_bytes=10_000_000)
    if ok:
        n = _try_local()
        if n > 0: return n

    # ── 3. Kleiner Online-Fallback (nur Basisdaten) ──────────────────────────
    if prog: prog("    Fallback: VizieR-Kurzabfrage (max. 200k, ohne UV/IR)...")
    objs=[]
    for cat,cols,cc,cv,label,parser in [
        ("VII/289/dr16q",["SDSS","RA_ICRS","DE_ICRS","gmag","z"],"gmag","..22","SDSS DR16Q","sdss"),
        ("VII/258/vv10",["Seq","RAJ2000","DEJ2000","Vmag","z"],"Vmag","..20","Veron-Cetty","vcv"),
    ]:
        if objs: break
        try:
            if prog: prog(f"    VizieR asu-csv {label}...")
            raw,host=_asu(cat,cols,cc,cv,max_rows=200000,timeout=40)
            if prog: prog(f"    {host} OK")
            for row in csv.DictReader(io.StringIO(raw)):
                try:
                    if parser=="sdss":
                        qid="QSO "+str(row.get("SDSS","")).strip()
                        ra=float(str(row.get("RA_ICRS","")).strip())
                        dec=float(str(row.get("DE_ICRS","")).strip())
                        try: mag=float(str(row.get("gmag","")).strip())
                        except: mag=None
                    else:
                        qid="QSO VV"+str(row.get("Seq","")).strip()
                        rs=str(row.get("RAJ2000","")).strip().split()
                        ds=str(row.get("DEJ2000","")).strip().split()
                        ra=(float(rs[0])+float(rs[1])/60+float(rs[2])/3600)*15 if len(rs)==3 else float(rs[0])*15
                        sgn=-1 if ds[0].startswith("-") else 1
                        dec=sgn*(abs(float(ds[0]))+float(ds[1])/60+float(ds[2])/3600) if len(ds)==3 else float(ds[0])
                        try: mag=float(str(row.get("Vmag","")).strip())
                        except: mag=None
                    try: z=float(str(row.get("z","")).strip())
                    except: z=None
                    objs.append({"id":qid,"catalog":"Quasar","ra":ra,"dec":dec,"magnitude":mag,"type":"quasar","name":qid,"description":f"Quasar z={z:.3f}" if z else "Quasar"})
                except: pass
            if prog: prog(f"    {label}: {len(objs)}")
        except Exception as e:
            if prog: prog(f"    {label} fehlgeschlagen: {e}")
    if objs: _insert(conn,objs)
    return len(objs)

# ── Caldwell (hardcodiert, 109 Objekte) ────────────────────────────────────────
_CALDWELL = [
    (1,"NGC 188","cluster",121.13,85.33,8.1),(2,"NGC 40","nebula",4.541,72.531,11.4),
    (3,"NGC 4236","galaxy",184.274,69.465,9.7),(4,"NGC 7023","nebula",315.967,68.17,6.8),
    (5,"IC 342","galaxy",56.702,68.097,9.1),(6,"NGC 6543","nebula",269.639,66.633,8.1),
    (7,"NGC 2403","galaxy",114.214,65.602,8.9),(8,"NGC 559","cluster",22.08,63.3,9.5),
    (9,"Sh2-155","nebula",344.08,62.628,7.7),(10,"NGC 663","cluster",26.572,61.234,7.1),
    (11,"NGC 7635","nebula",350.194,61.2,11.0),(12,"NGC 6946","galaxy",308.718,60.154,8.8),
    (13,"NGC 457","cluster",19.275,58.279,6.4),(14,"NGC 869","cluster",34.748,57.137,4.3),
    (15,"NGC 884","cluster",35.602,57.137,4.4),(16,"NGC 7243","cluster",333.79,49.894,6.4),
    (17,"NGC 147","galaxy",8.3,48.508,9.3),(18,"NGC 185","galaxy",14.742,48.337,9.2),
    (19,"IC 5146","nebula",328.369,47.268,7.2),(20,"NGC 7000","nebula",314.735,44.367,4.0),
    (21,"NGC 4449","galaxy",187.047,44.094,9.4),(22,"NGC 7662","nebula",351.103,42.548,8.3),
    (23,"NGC 891","galaxy",35.639,42.349,9.9),(24,"NGC 1275","galaxy",49.951,41.512,11.6),
    (25,"NGC 2419","cluster",114.535,38.882,10.4),(26,"NGC 4244","galaxy",184.374,37.807,10.2),
    (27,"NGC 6888","nebula",303.103,38.355,7.4),(28,"NGC 752","cluster",29.205,37.678,5.7),
    (29,"NGC 5005","galaxy",197.734,37.059,9.8),(30,"NGC 7331","galaxy",339.267,34.416,9.5),
    (31,"IC 405","nebula",80.745,34.266,6.0),(32,"NGC 4631","galaxy",190.533,32.542,9.2),
    (33,"NGC 6992","nebula",314.363,31.733,7.0),(34,"NGC 6960","nebula",312.702,30.718,7.0),
    (35,"NGC 4889","cluster",195.033,27.977,12.4),(36,"NGC 4559","galaxy",188.994,27.96,9.9),
    (37,"NGC 6885","cluster",303.399,26.488,5.7),(38,"NGC 4565","galaxy",189.087,25.988,9.6),
    (39,"NGC 2392","nebula",112.293,20.912,9.2),(40,"NGC 3626","galaxy",170.021,18.357,10.9),
    (41,"Hyades","cluster",66.75,15.87,0.5),(42,"NGC 7006","cluster",315.37,16.188,10.6),
    (43,"NGC 7814","galaxy",0.815,16.145,10.5),(44,"NGC 7479","galaxy",346.236,12.323,11.0),
    (45,"NGC 5248","galaxy",204.376,8.885,10.2),(46,"NGC 2261","nebula",100.237,8.734,10.0),
    (47,"NGC 6934","cluster",308.547,7.404,8.9),(48,"NGC 2775","galaxy",137.58,7.038,10.3),
    (49,"NGC 2237","nebula",97.996,4.937,6.0),(50,"NGC 2244","cluster",97.978,4.953,4.8),
    (51,"IC 1613","galaxy",16.199,2.118,9.3),(52,"NGC 4697","galaxy",192.149,-5.801,9.3),
    (53,"NGC 3115","galaxy",151.309,-7.719,8.9),(54,"NGC 2506","cluster",120.001,-10.779,7.6),
    (55,"NGC 7009","nebula",316.048,-11.362,8.0),(56,"NGC 246","nebula",11.765,-11.876,8.0),
    (57,"NGC 6822","galaxy",296.235,-14.786,9.3),(58,"NGC 2360","cluster",109.435,-15.632,7.2),
    (59,"NGC 3242","nebula",156.002,-18.638,8.6),(60,"NGC 4038","galaxy",180.471,-18.868,10.5),
    (61,"NGC 4039","galaxy",180.478,-18.885,10.7),(62,"NGC 247","galaxy",11.785,-20.758,9.1),
    (63,"NGC 7293","nebula",337.411,-20.837,7.3),(64,"NGC 2362","cluster",109.677,-24.953,4.1),
    (65,"NGC 253","galaxy",11.888,-25.289,7.6),(66,"NGC 5694","cluster",219.902,-26.537,10.2),
    (67,"NGC 1097","galaxy",41.58,-30.274,9.5),(68,"NGC 6729","nebula",289.977,-36.958,9.7),
    (69,"NGC 6302","nebula",258.84,-37.102,9.6),(70,"NGC 300","galaxy",13.722,-37.685,8.7),
    (71,"NGC 2477","cluster",118.026,-38.535,5.8),(72,"NGC 55","galaxy",3.723,-39.197,8.0),
    (73,"NGC 1851","cluster",78.528,-40.047,7.3),(74,"NGC 3132","nebula",151.752,-40.438,8.2),
    (75,"NGC 6124","cluster",246.358,-40.655,5.8),(76,"NGC 6231","cluster",253.566,-41.82,2.6),
    (77,"NGC 5128","galaxy",201.365,-43.019,6.8),(78,"NGC 6541","cluster",272.002,-43.712,6.3),
    (79,"NGC 3201","cluster",154.402,-46.412,6.7),(80,"NGC 5139","cluster",201.697,-47.48,3.9),
    (81,"NGC 6352","cluster",262.179,-48.423,8.2),(82,"NGC 6193","cluster",248.133,-48.759,5.2),
    (83,"NGC 4945","galaxy",196.365,-49.468,8.7),(84,"NGC 5286","cluster",206.612,-51.374,7.6),
    (85,"IC 2391","cluster",130.527,-53.066,2.5),(86,"NGC 6397","cluster",265.175,-53.675,5.7),
    (87,"NGC 1261","cluster",48.059,-55.216,8.4),(88,"NGC 5823","cluster",226.361,-55.605,7.9),
    (89,"NGC 6087","cluster",244.583,-57.9,5.4),(90,"NGC 2867","nebula",139.573,-58.307,9.7),
    (91,"NGC 3532","cluster",166.432,-58.658,3.0),(92,"NGC 3372","nebula",160.988,-59.867,6.2),
    (93,"NGC 6752","cluster",287.717,-59.985,5.4),(94,"NGC 4755","cluster",193.567,-60.374,4.2),
    (95,"NGC 6025","cluster",241.524,-60.429,5.1),(96,"NGC 2516","cluster",119.53,-60.867,3.8),
    (97,"NGC 3766","cluster",174.014,-61.627,5.3),(98,"NGC 4609","cluster",190.282,-62.985,6.9),
    (99,"Coal Sack","nebula",186.0,-63.0,None),(100,"IC 2944","cluster",173.549,-63.014,4.5),
    (101,"NGC 6744","galaxy",287.441,-63.857,8.3),(102,"IC 2602","cluster",160.743,-64.396,1.9),
    (103,"NGC 2070","nebula",84.673,-69.102,8.0),(104,"NGC 362","cluster",15.809,-70.849,6.4),
    (105,"NGC 4833","cluster",194.891,-70.876,7.4),(106,"NGC 104","cluster",6.023,-72.081,4.0),
    (107,"NGC 6101","cluster",247.799,-72.202,9.3),(108,"NGC 4372","cluster",186.44,-72.66,7.8),
    (109,"NGC 3195","nebula",154.6,-80.866,9.2),
]

def download_caldwell(conn, prog=None):
    objs=[]
    for num,name,typ,ra,dec,mag in _CALDWELL:
        cid=f"C {num}"
        objs.append({"id":cid,"catalog":"Caldwell","ra":ra,"dec":dec,
                     "magnitude":mag,"type":typ,"name":name,
                     "description":f"Caldwell {num} – {name}"})
    _insert(conn,objs)
    if prog: prog(f"    Caldwell: {len(objs)}")
    return len(objs)


# ── Sharpless HII-Regionen ──────────────────────────────────────────────────────
def download_sharpless(conn, prog=None):
    """Sharpless 2 HII-Regionen via SIMBAD TAP (ident-Join auf 'Sh 2-*')."""
    objs = []
    # Primär: SIMBAD – alle Sharpless-2-Objekte
    # Sharpless-Katalog hat ~313 Objekte; kein TOP-Limit nötig
    for adql in [
        ("SELECT b.main_id,b.ra,b.dec FROM basic b "
         "JOIN ident i ON i.oidref=b.oid "
         "WHERE i.id LIKE 'Sh 2-%' AND b.ra IS NOT NULL"),
        # Fallback: alle HII-Regionen ohne Namensfilter (breiter, aber vollständig)
        ("SELECT main_id,ra,dec FROM basic "
         "WHERE otype='HII' AND ra IS NOT NULL"),
    ]:
        if objs: break
        try:
            if prog: prog("    SIMBAD TAP Sharpless (ident)...")
            raw = _simbad_tap(adql, 120)
            for row in csv.DictReader(io.StringIO(raw)):
                try:
                    nm = str(row.get("main_id","")).strip()
                    if not nm: continue
                    ra = float(row["ra"]); dec = float(row["dec"])
                    objs.append({"id":nm,"catalog":"Sharpless","ra":ra,"dec":dec,
                                 "magnitude":None,"type":"nebula","name":nm,
                                 "description":f"HII-Emissionsregion {nm}"})
                except: pass
            if prog: prog(f"    Sharpless SIMBAD: {len(objs)}")
        except Exception as e:
            if prog: prog(f"    Sharpless SIMBAD fehlgeschlagen: {e}")
    # Fallback: VizieR
    if not objs:
        for tbl in ["VII/20/sharpless","VII/20/catalog","VII/20"]:
            if objs: break
            try:
                if prog: prog(f"    VizieR {tbl}...")
                raw,host = _asu(tbl,["SH","_RAJ2000","_DEJ2000"],"SH","1..400",max_rows=400,timeout=30)
                for row in csv.DictReader(io.StringIO(raw)):
                    try:
                        sh = str(row.get("SH","")).strip()
                        if not sh or not sh.isdigit(): continue
                        sid = f"Sh2-{sh}"
                        ra  = float(str(row.get("_RAJ2000","")).strip())
                        dec = float(str(row.get("_DEJ2000","")).strip())
                        objs.append({"id":sid,"catalog":"Sharpless","ra":ra,"dec":dec,
                                     "magnitude":None,"type":"nebula","name":sid,
                                     "description":f"Sharpless HII-Region {sid}"})
                    except: pass
                if prog: prog(f"    Sharpless VizieR ({host}): {len(objs)}")
            except Exception as e:
                if prog: prog(f"    {tbl} fehlgeschlagen: {e}")
    if objs: _insert(conn, objs)
    return len(objs)


# ── Abell-Galaxienhaufen ────────────────────────────────────────────────────────
def download_abell(conn, prog=None):
    """
    Abell-Galaxienhaufen: VizieR VII/110A (Abell, Corwin & Olowin 1989) zuerst,
    da das die kanonische, vollständige Originaltabelle mit allen 4073 Haufen
    ist. SIMBAD wird NUR als Ergänzung genutzt (additiv, nicht exklusiv),
    weil SIMBADs otype='ClG'-Filter nachweislich lückenhaft ist – viele
    Abell-Haufen sind dort unter anderen/keinen Otypes klassifiziert und
    fehlen dann komplett, wenn man nur SIMBAD abfragt. Frühere Versionen
    nutzten SIMBAD zuerst und VizieR nur als Fallback bei SIMBAD-Totalausfall,
    wodurch SIMBAD-Lücken (z.B. fehlende Haufen nahe M51) nie durch die
    vollständigere VizieR-Tabelle ausgeglichen wurden.
    """
    objs = []
    seen_ids = set()

    # ── 1. VizieR VII/110A (Primärquelle, vollständig, kanonisch) ────────────
    for tbl,ra_col,dec_col in [
        ("VII/110A/aco","_RAJ2000","_DEJ2000"),
        ("VII/110A","RAdeg","DEdeg"),
    ]:
        if objs: break
        try:
            if prog: prog(f"    VizieR {tbl} (Primärquelle, alle 4073 Abell-Haufen)...")
            raw,host = _asu(tbl,["ACO",ra_col,dec_col],"ACO","1..5000",max_rows=6000,timeout=35)
            for row in csv.DictReader(io.StringIO(raw)):
                try:
                    aco = str(row.get("ACO","")).strip()
                    if not aco or not aco.isdigit(): continue
                    aid = f"Abell {aco}"
                    ra  = float(str(row.get(ra_col,"")).strip())
                    dec = float(str(row.get(dec_col,"")).strip())
                    objs.append({"id":aid,"catalog":"Abell","ra":ra,"dec":dec,
                                 "magnitude":None,"type":"cluster","name":aid,
                                 "description":"Galaxienhaufen (Abell, Corwin & Olowin 1989)"})
                    seen_ids.add(aid)
                except: pass
            if prog: prog(f"    Abell VizieR ({host}): {len(objs)}")
        except Exception as e:
            if prog: prog(f"    {tbl} fehlgeschlagen: {e}")

    # ── 2. SIMBAD als ADDITIVE Ergänzung (nicht exklusiver Fallback) ─────────
    # Fügt zusätzliche, in VizieR evtl. fehlende ACO-Supplement-Cluster hinzu,
    # ersetzt aber NICHT die VizieR-Liste. Läuft auch wenn VizieR schon
    # Ergebnisse lieferte, damit keine Cluster verloren gehen.
    for adql in [
        ("SELECT b.main_id,b.ra,b.dec FROM basic b "
         "JOIN ident i ON i.oidref=b.oid "
         "WHERE i.id LIKE 'Abell %' AND b.ra IS NOT NULL"),
        ("SELECT b.main_id,b.ra,b.dec FROM basic b "
         "JOIN ident i ON i.oidref=b.oid "
         "WHERE i.id LIKE 'ACO %' AND b.ra IS NOT NULL"),
    ]:
        try:
            if prog: prog("    SIMBAD TAP Abell (Ergänzung, ohne Otype-Filter)...")
            raw = _simbad_tap(adql, 60)
            n_added = 0
            for row in csv.DictReader(io.StringIO(raw)):
                try:
                    nm = str(row.get("main_id","")).strip()
                    if not nm or nm in seen_ids: continue
                    ra = float(row["ra"]); dec = float(row["dec"])
                    objs.append({"id":nm,"catalog":"Abell","ra":ra,"dec":dec,
                                 "magnitude":None,"type":"cluster","name":nm,
                                 "description":"Galaxienhaufen (Abell, SIMBAD-Ergänzung)"})
                    seen_ids.add(nm)
                    n_added += 1
                except: pass
            if prog: prog(f"    SIMBAD-Ergänzung: +{n_added} (gesamt: {len(objs)})")
        except Exception as e:
            if prog: prog(f"    SIMBAD-Ergänzung fehlgeschlagen (nicht kritisch): {e}")

    if objs: _insert(conn, objs)
    return len(objs)



# ── WHL2012 Galaxienhaufen (Wen, Han & Liu 2012, ~132k Cluster) ────────────────
def download_whl(conn, prog=None):
    """
    WHL2012: 132.684 Galaxienhaufen aus SDSS DR8 Photometrie.
    VizieR J/ApJS/199/34/table1 (Wen, Han & Liu 2012, ApJS 199, 34).
    Ergänzt den klassischen Abell-Katalog (4073 Haufen) um einen Faktor ~30.
    """
    if prog: prog("    VizieR TAP J/ApJS/199/34/table1 (WHL2012, ~132k Haufen)...")
    tap_url = "https://tapvizier.cds.unistra.fr/TAPVizieR/tap/sync"
    objs = []
    # VizieR TAP hat kein OFFSET — chunking ueber RA-Bereiche
    ra_edges = list(range(0, 361, 30))  # 12 Chunks a 30 Grad
    for i in range(len(ra_edges) - 1):
        ra_lo, ra_hi = ra_edges[i], ra_edges[i + 1]
        adql = (f'SELECT WHL, RAJ2000, DEJ2000, zph, N200 '
                f'FROM "J/ApJS/199/34/table1" '
                f'WHERE RAJ2000 BETWEEN {ra_lo} AND {ra_hi}')
        try:
            raw = _tap(tap_url, adql, timeout=120)
        except Exception as e:
            if prog: prog(f"    WHL chunk RA {ra_lo}-{ra_hi}: {e}")
            continue
        n_rows = 0
        for row in csv.DictReader(io.StringIO(raw)):
            try:
                whl = str(row.get("WHL", "")).strip()
                if not whl:
                    continue
                ra = float(row["RAJ2000"])
                dec = float(row["DEJ2000"])
                zph = row.get("zph", "")
                n200 = row.get("N200", "")
                desc = "Galaxienhaufen (WHL2012)"
                if zph:
                    desc += f", z={zph}"
                if n200:
                    desc += f", N200={n200}"
                objs.append({
                    "id": f"WHL {whl}", "catalog": "WHL",
                    "ra": ra, "dec": dec, "magnitude": None,
                    "type": "cluster", "name": f"WHL {whl}",
                    "description": desc,
                })
                n_rows += 1
            except (ValueError, KeyError):
                pass
        if prog:
            prog(f"    WHL RA {ra_lo}-{ra_hi}: +{n_rows} (gesamt: {len(objs)})")
    if objs:
        _insert(conn, objs)
    return len(objs)


# ── redMaPPer Galaxienhaufen (Rykoff et al. 2014, ~25k Cluster) ───────────────
def download_redmapper(conn, prog=None):
    """
    redMaPPer v5.10: ~25.325 Galaxienhaufen aus SDSS DR8.
    VizieR J/ApJ/785/104/table1 (Rykoff et al. 2014, ApJ 785, 104).
    Red-sequence Matched-filter Probabilistic Percolation cluster finder.
    """
    if prog: prog("    VizieR TAP J/ApJ/785/104/table1 (redMaPPer, ~25k Haufen)...")
    tap_url = "https://tapvizier.cds.unistra.fr/TAPVizieR/tap/sync"
    adql = ('SELECT Name, RAJ2000, DEJ2000, zlambda, lambda '
            'FROM "J/ApJ/785/104/table1"')
    raw = _tap(tap_url, adql, timeout=120)
    objs = []
    for row in csv.DictReader(io.StringIO(raw)):
        try:
            name = str(row.get("Name", "")).strip()
            if not name:
                continue
            ra = float(row["RAJ2000"])
            dec = float(row["DEJ2000"])
            z = row.get("zlambda", "")
            richness = row.get("lambda", "")
            desc = "Galaxienhaufen (redMaPPer)"
            if z:
                desc += f", z={z}"
            if richness:
                desc += f", Richness={richness}"
            objs.append({
                "id": name, "catalog": "redMaPPer",
                "ra": ra, "dec": dec, "magnitude": None,
                "type": "cluster", "name": name,
                "description": desc,
            })
        except (ValueError, KeyError):
            pass
    if prog:
        prog(f"    redMaPPer: {len(objs)} Haufen")
    if objs:
        _insert(conn, objs)
    return len(objs)


# ── Planetarische Nebel ──────────────────────────────────────────────────────────
def download_planetary_nebulae(conn, prog=None):
    """Planetarische Nebel via SIMBAD TAP (otype=PN) – ohne flux-JOIN."""
    objs = []
    # Primär: SIMBAD – alle PN ohne TOP-Limit (~3500 bekannte galakt. PN)
    for adql in [
        "SELECT main_id,ra,dec FROM basic WHERE otype='PN' AND ra IS NOT NULL",
        # Fallback: breiterer otype-Match (PNb = bipolar PN, etc.)
        ("SELECT main_id,ra,dec FROM basic "
         "WHERE otype LIKE 'PN%' AND ra IS NOT NULL"),
    ]:
        if objs: break
        try:
            if prog: prog("    SIMBAD TAP Planetarische Nebel...")
            raw = _simbad_tap(adql, 120)
            for row in csv.DictReader(io.StringIO(raw)):
                try:
                    nm = str(row.get("main_id","")).strip()
                    if not nm: continue
                    ra  = float(row["ra"]); dec = float(row["dec"])
                    objs.append({"id":nm,"catalog":"PN","ra":ra,"dec":dec,
                                 "magnitude":None,"type":"nebula","name":nm,
                                 "description":f"Planetarischer Nebel {nm}"})
                except: pass
            if prog: prog(f"    Planetarische Nebel SIMBAD: {len(objs)}")
        except Exception as e:
            if prog: prog(f"    PN SIMBAD fehlgeschlagen: {e}")
    # Fallback: VizieR V/84 (Strasbourg-ESO Galaktische PN)
    if not objs:
        for tbl in ["V/84/main","V/84/catalog","V/84"]:
            if objs: break
            try:
                if prog: prog(f"    VizieR {tbl}...")
                raw,host = _asu(tbl,["PN","_RAJ2000","_DEJ2000"],"PN","*",max_rows=2000,timeout=30)
                for row in csv.DictReader(io.StringIO(raw)):
                    try:
                        nm = str(row.get("PN","")).strip()
                        if not nm: continue
                        ra  = float(str(row.get("_RAJ2000","")).strip())
                        dec = float(str(row.get("_DEJ2000","")).strip())
                        objs.append({"id":nm,"catalog":"PN","ra":ra,"dec":dec,
                                     "magnitude":None,"type":"nebula","name":nm,
                                     "description":f"Planetarischer Nebel {nm}"})
                    except: pass
                if prog: prog(f"    PN VizieR ({host}): {len(objs)}")
            except Exception as e2:
                if prog: prog(f"    {tbl} fehlgeschlagen: {e2}")
    if objs: _insert(conn, objs)
    return len(objs)


# ── Arp Peculiar Galaxies ─────────────────────────────────────────────────────
def download_arp(conn, prog=None):
    """Arp Peculiar Galaxies (338 Objekte) via SIMBAD TAP."""
    objs = []
    for adql in [
        ("SELECT b.main_id,b.ra,b.dec FROM basic b "
         "JOIN ident i ON i.oidref=b.oid "
         "WHERE i.id LIKE 'Arp %' AND b.ra IS NOT NULL"),
        ("SELECT main_id,ra,dec FROM basic "
         "WHERE otype='PeG' AND ra IS NOT NULL"),
    ]:
        if objs: break
        try:
            if prog: prog("    SIMBAD TAP Arp...")
            raw = _simbad_tap(adql, 90)
            for row in csv.DictReader(io.StringIO(raw)):
                try:
                    nm = str(row.get("main_id","")).strip()
                    if not nm: continue
                    ra = float(row["ra"]); dec = float(row["dec"])
                    objs.append({"id":nm,"catalog":"Arp","ra":ra,"dec":dec,
                                 "magnitude":None,"type":"galaxy","name":nm,
                                 "description":f"Arp Peculiare Galaxie {nm}"})
                except: pass
            if prog: prog(f"    Arp: {len(objs)}")
        except Exception as e:
            if prog: prog(f"    Arp fehlgeschlagen: {e}")
    if objs: _insert(conn, objs)
    return len(objs)


# ── vdB Reflexionsnebel ───────────────────────────────────────────────────────
def download_vdb(conn, prog=None):
    """van den Bergh Reflexionsnebel (158) via SIMBAD TAP."""
    objs = []
    for adql in [
        ("SELECT b.main_id,b.ra,b.dec FROM basic b "
         "JOIN ident i ON i.oidref=b.oid "
         "WHERE i.id LIKE 'vdB %' AND b.ra IS NOT NULL"),
        ("SELECT main_id,ra,dec FROM basic "
         "WHERE otype='RNe' AND ra IS NOT NULL"),
    ]:
        if objs: break
        try:
            if prog: prog("    SIMBAD TAP vdB...")
            raw = _simbad_tap(adql, 90)
            for row in csv.DictReader(io.StringIO(raw)):
                try:
                    nm = str(row.get("main_id","")).strip()
                    if not nm: continue
                    ra = float(row["ra"]); dec = float(row["dec"])
                    objs.append({"id":nm,"catalog":"vdB","ra":ra,"dec":dec,
                                 "magnitude":None,"type":"nebula","name":nm,
                                 "description":f"vdB Reflexionsnebel {nm}"})
                except: pass
            if prog: prog(f"    vdB: {len(objs)}")
        except Exception as e:
            if prog: prog(f"    vdB fehlgeschlagen: {e}")
    if objs: _insert(conn, objs)
    return len(objs)


# ── LDN Dunkelnebel ───────────────────────────────────────────────────────────
def download_ldn(conn, prog=None):
    """Lynds Dark Nebulae (1802) via SIMBAD TAP."""
    objs = []
    for adql in [
        ("SELECT b.main_id,b.ra,b.dec FROM basic b "
         "JOIN ident i ON i.oidref=b.oid "
         "WHERE i.id LIKE 'LDN %' AND b.ra IS NOT NULL"),
        ("SELECT main_id,ra,dec FROM basic "
         "WHERE otype='DNe' AND ra IS NOT NULL"),
    ]:
        if objs: break
        try:
            if prog: prog("    SIMBAD TAP LDN...")
            raw = _simbad_tap(adql, 120)
            for row in csv.DictReader(io.StringIO(raw)):
                try:
                    nm = str(row.get("main_id","")).strip()
                    if not nm: continue
                    ra = float(row["ra"]); dec = float(row["dec"])
                    objs.append({"id":nm,"catalog":"LDN","ra":ra,"dec":dec,
                                 "magnitude":None,"type":"nebula","name":nm,
                                 "description":f"Lynds Dunkelnebel {nm}"})
                except: pass
            if prog: prog(f"    LDN: {len(objs)}")
        except Exception as e:
            if prog: prog(f"    LDN fehlgeschlagen: {e}")
    if objs: _insert(conn, objs)
    return len(objs)


# ── LBN Hellnebel ─────────────────────────────────────────────────────────────
def download_lbn(conn, prog=None):
    """Lynds Bright Nebulae (1125) via SIMBAD TAP."""
    objs = []
    for adql in [
        ("SELECT b.main_id,b.ra,b.dec FROM basic b "
         "JOIN ident i ON i.oidref=b.oid "
         "WHERE i.id LIKE 'LBN %' AND b.ra IS NOT NULL"),
        ("SELECT b.main_id,b.ra,b.dec FROM basic b "
         "JOIN ident i ON i.oidref=b.oid "
         "WHERE i.id LIKE 'Lyn %' AND b.otype='HII' AND b.ra IS NOT NULL"),
    ]:
        if objs: break
        try:
            if prog: prog("    SIMBAD TAP LBN...")
            raw = _simbad_tap(adql, 120)
            for row in csv.DictReader(io.StringIO(raw)):
                try:
                    nm = str(row.get("main_id","")).strip()
                    if not nm: continue
                    ra = float(row["ra"]); dec = float(row["dec"])
                    objs.append({"id":nm,"catalog":"LBN","ra":ra,"dec":dec,
                                 "magnitude":None,"type":"nebula","name":nm,
                                 "description":f"Lynds Hellnebel {nm}"})
                except: pass
            if prog: prog(f"    LBN: {len(objs)}")
        except Exception as e:
            if prog: prog(f"    LBN fehlgeschlagen: {e}")
    if objs: _insert(conn, objs)
    return len(objs)


# ── Barnard Dunkelnebel ───────────────────────────────────────────────────────
def download_barnard(conn, prog=None):
    """Barnard Dark Nebulae (~370) via SIMBAD TAP."""
    objs = []
    for adql in [
        ("SELECT b.main_id,b.ra,b.dec FROM basic b "
         "JOIN ident i ON i.oidref=b.oid "
         "WHERE i.id LIKE 'Barnard %' AND b.ra IS NOT NULL"),
        ("SELECT b.main_id,b.ra,b.dec FROM basic b "
         "JOIN ident i ON i.oidref=b.oid "
         "WHERE i.id LIKE '[B64] %' AND b.otype='DNe' AND b.ra IS NOT NULL"),
    ]:
        if objs: break
        try:
            if prog: prog("    SIMBAD TAP Barnard...")
            raw = _simbad_tap(adql, 90)
            for row in csv.DictReader(io.StringIO(raw)):
                try:
                    nm = str(row.get("main_id","")).strip()
                    if not nm: continue
                    ra = float(row["ra"]); dec = float(row["dec"])
                    objs.append({"id":nm,"catalog":"Barnard","ra":ra,"dec":dec,
                                 "magnitude":None,"type":"nebula","name":nm,
                                 "description":f"Barnard Dunkelnebel {nm}"})
                except: pass
            if prog: prog(f"    Barnard: {len(objs)}")
        except Exception as e:
            if prog: prog(f"    Barnard fehlgeschlagen: {e}")
    if objs: _insert(conn, objs)
    return len(objs)


# ── SNR Supernovareste ────────────────────────────────────────────────────────
def download_snr(conn, prog=None):
    """Supernova Remnants via SIMBAD TAP."""
    objs = []
    try:
        if prog: prog("    SIMBAD TAP SNR...")
        raw = _simbad_tap(
            "SELECT main_id,ra,dec FROM basic WHERE otype='SNR' AND ra IS NOT NULL", 90)
        for row in csv.DictReader(io.StringIO(raw)):
            try:
                nm = str(row.get("main_id","")).strip()
                if not nm: continue
                ra = float(row["ra"]); dec = float(row["dec"])
                objs.append({"id":nm,"catalog":"SNR","ra":ra,"dec":dec,
                             "magnitude":None,"type":"nebula","name":nm,
                             "description":f"Supernova-Rest {nm}"})
            except: pass
        if prog: prog(f"    SNR: {len(objs)}")
    except Exception as e:
        if prog: prog(f"    SNR fehlgeschlagen: {e}")
    if objs: _insert(conn, objs)
    return len(objs)


# ── Collinder Sternhaufen ─────────────────────────────────────────────────────
def download_collinder(conn, prog=None):
    """Collinder Open Clusters (471) via SIMBAD TAP."""
    objs = []
    for adql in [
        ("SELECT b.main_id,b.ra,b.dec FROM basic b "
         "JOIN ident i ON i.oidref=b.oid "
         "WHERE i.id LIKE 'Cl Collinder %' AND b.ra IS NOT NULL"),
        ("SELECT b.main_id,b.ra,b.dec FROM basic b "
         "JOIN ident i ON i.oidref=b.oid "
         "WHERE i.id LIKE 'Collinder %' AND b.ra IS NOT NULL"),
    ]:
        if objs: break
        try:
            if prog: prog("    SIMBAD TAP Collinder...")
            raw = _simbad_tap(adql, 90)
            for row in csv.DictReader(io.StringIO(raw)):
                try:
                    nm = str(row.get("main_id","")).strip()
                    if not nm: continue
                    ra = float(row["ra"]); dec = float(row["dec"])
                    objs.append({"id":nm,"catalog":"Collinder","ra":ra,"dec":dec,
                                 "magnitude":None,"type":"cluster","name":nm,
                                 "description":f"Collinder Sternhaufen {nm}"})
                except: pass
            if prog: prog(f"    Collinder: {len(objs)}")
        except Exception as e:
            if prog: prog(f"    Collinder fehlgeschlagen: {e}")
    if objs: _insert(conn, objs)
    return len(objs)


# ── Hickson Kompakte Gruppen (hardcodiert, 100 Gruppen) ───────────────────────
_HICKSON = [
    (1,9.67,25.73),(2,13.81,-1.01),(3,17.17,-8.36),(4,16.36,21.26),(5,17.57,-10.30),
    (6,20.01,-10.53),(7,23.31,0.88),(8,27.15,23.57),(9,28.73,18.94),(10,21.37,34.70),
    (11,24.08,-34.60),(12,26.55,-5.77),(13,32.00,-4.00),(14,32.39,3.37),(15,31.39,2.18),
    (16,32.58,27.74),(17,34.46,13.06),(18,36.80,-4.30),(19,37.81,14.87),(20,42.72,-5.97),
    (21,41.40,10.51),(22,46.58,34.08),(23,40.40,-9.57),(24,49.69,-4.00),(25,51.18,-1.04),
    (26,51.50,14.62),(27,55.00,27.00),(28,54.98,-14.05),(29,55.00,8.35),(30,63.40,-14.00),
    (31,75.42,-4.25),(32,81.95,20.81),(33,79.55,18.90),(34,83.60,6.15),(35,83.90,28.70),
    (36,91.92,14.37),(37,92.80,30.00),(38,88.70,-10.40),(39,87.90,-1.60),(40,96.10,5.10),
    (41,96.40,23.20),(42,101.80,20.00),(43,103.30,-31.30),(44,111.50,21.80),(45,113.20,15.80),
    (46,115.40,-8.20),(47,117.60,13.70),(48,120.00,19.30),(49,121.80,67.20),(50,122.40,9.00),
    (51,124.50,24.30),(52,129.80,18.30),(53,133.60,39.60),(54,135.50,20.60),(55,140.10,29.40),
    (56,141.00,52.90),(57,143.20,22.40),(58,147.30,9.70),(59,154.00,12.80),(60,155.10,11.60),
    (61,162.50,22.50),(62,169.60,31.00),(63,170.20,19.00),(64,172.10,20.00),(65,177.30,30.30),
    (66,179.80,57.30),(67,180.50,30.10),(68,184.20,20.20),(69,185.00,25.10),(70,189.60,33.00),
    (71,191.30,25.50),(72,193.30,19.00),(73,194.20,19.00),(74,200.60,21.00),(75,203.40,21.10),
    (76,207.60,7.30),(77,211.90,17.50),(78,214.30,8.50),(79,218.50,21.20),(80,224.30,22.60),
    (81,226.00,12.80),(82,229.80,32.80),(83,231.40,7.20),(84,232.30,7.70),(85,233.50,73.30),
    (86,234.40,51.70),(87,238.70,3.30),(88,241.20,18.20),(89,243.20,3.50),(90,244.30,28.00),
    (91,247.90,29.30),(92,249.70,34.00),(93,252.00,49.00),(94,254.30,18.90),(95,255.50,9.50),
    (96,258.00,18.70),(97,263.70,17.60),(98,272.00,13.80),(99,272.50,17.30),(100,354.90,-10.40),
]

def download_hickson(conn, prog=None):
    objs = []
    for num,ra,dec in _HICKSON:
        hid = f"HCG {num}"
        objs.append({"id":hid,"catalog":"Hickson","ra":ra,"dec":dec,
                     "magnitude":None,"type":"galaxy","name":hid,
                     "description":f"Hickson Kompakte Gruppe {num}"})
    _insert(conn, objs)
    if prog: prog(f"    Hickson: {len(objs)}")
    return len(objs)


# ── WDS Doppelsterne ──────────────────────────────────────────────────────────
def download_wds(conn, prog=None, mag_limit=22.0):
    """Washington Double Star Catalog via VizieR (Vmag1 <= mag_limit)."""
    objs = []
    for cat in ["B/wds/wds", "B/wds/wdss"]:
        if objs: break
        try:
            if prog: prog(f"    VizieR WDS (mag<={mag_limit})...")
            raw,host = _asu(cat,["WDS","RAJ2000","DEJ2000","Vmag1","Vmag2"],
                            "Vmag1",f"..{mag_limit}",max_rows=200000,timeout=40)
            for row in csv.DictReader(io.StringIO(raw)):
                try:
                    wid = str(row.get("WDS","")).strip()
                    if not wid: continue
                    ra  = float(str(row.get("RAJ2000","")).strip())
                    dec = float(str(row.get("DEJ2000","")).strip())
                    try: mag = float(str(row.get("Vmag1","")).strip())
                    except: mag = None
                    try: mag2 = float(str(row.get("Vmag2","")).strip())
                    except: mag2 = None
                    desc = "Doppelstern"
                    if mag is not None and mag2 is not None:
                        desc += f" V1={mag:.1f} V2={mag2:.1f}"
                    objs.append({"id":f"WDS {wid}","catalog":"WDS","ra":ra,"dec":dec,
                                 "magnitude":mag,"type":"double","name":f"WDS {wid}",
                                 "description":desc})
                except: pass
            if prog: prog(f"    WDS ({host}): {len(objs)}")
        except Exception as e:
            if prog: prog(f"    {cat} fehlgeschlagen: {e}")
    if objs: _insert(conn, objs)
    return len(objs)


# ── AAVSO VSX Veränderliche ───────────────────────────────────────────────────
def download_vsx(conn, prog=None, mag_limit=22.0):
    """AAVSO Variable Star Index via VizieR (max-Helligkeit <= mag_limit)."""
    objs = []
    try:
        if prog: prog(f"    VizieR VSX (max<={mag_limit})...")
        raw,host = _asu("B/vsx/vsx",
                        ["Name","RAJ2000","DEJ2000","max","min","Period","Type"],
                        "max",f"..{mag_limit}",max_rows=200000,timeout=40)
        for row in csv.DictReader(io.StringIO(raw)):
            try:
                nm = str(row.get("Name","")).strip()
                if not nm: continue
                ra  = float(str(row.get("RAJ2000","")).strip())
                dec = float(str(row.get("DEJ2000","")).strip())
                try: mag = float(str(row.get("max","")).strip())
                except: mag = None
                vtype = str(row.get("Type","")).strip() or "VAR"
                try: period = float(str(row.get("Period","")).strip())
                except: period = None
                desc = f"Veränderl. ({vtype})"
                if period: desc += f" P={period:.2f}d"
                objs.append({"id":nm,"catalog":"VSX","ra":ra,"dec":dec,
                             "magnitude":mag,"type":"variable","name":nm,"description":desc})
            except: pass
        if prog: prog(f"    VSX ({host}): {len(objs)}")
    except Exception as e:
        if prog: prog(f"    VSX fehlgeschlagen: {e}")
    if objs: _insert(conn, objs)
    return len(objs)


# ── Exoplaneten-Wirtssterne ───────────────────────────────────────────────────
def download_exoplanets(conn, prog=None):
    """Exoplaneten-Wirtssterne via NASA Exoplanet Archive TAP."""
    objs = []
    # Primär: NASA TAP (ADQL mit DISTINCT-Emulation via pscomppars)
    try:
        if prog: prog("    NASA Exoplanet Archive TAP...")
        url = "https://exoplanetarchive.ipac.caltech.edu/TAP/sync"
        adql = ("SELECT hostname,ra,dec,sy_vmag,sy_pnum "
                "FROM pscomppars WHERE ra IS NOT NULL")
        data = urllib.parse.urlencode({
            "REQUEST":"doQuery","LANG":"ADQL","FORMAT":"csv","QUERY":adql
        }).encode()
        raw = _text(url, data=data, timeout=120)
        seen = set()
        for row in csv.DictReader(io.StringIO(raw)):
            try:
                nm = str(row.get("hostname","")).strip()
                if not nm or nm in seen: continue
                seen.add(nm)
                ra  = float(str(row.get("ra","")).strip())
                dec = float(str(row.get("dec","")).strip())
                try: mag = float(str(row.get("sy_vmag","")).strip())
                except: mag = None
                try: npl = int(float(str(row.get("sy_pnum","")).strip()))
                except: npl = 1
                pl = "Planet" if npl == 1 else "Planeten"
                objs.append({"id":nm,"catalog":"Exoplanet","ra":ra,"dec":dec,
                             "magnitude":mag,"type":"exoplanet","name":nm,
                             "description":f"Exoplanetensystem – {npl} {pl}"})
            except: pass
        if prog: prog(f"    Exoplaneten: {len(objs)} Systeme")
    except Exception as e:
        if prog: prog(f"    Exoplaneten TAP fehlgeschlagen: {e}")
    # Fallback: ältere nsted-API
    if not objs:
        try:
            if prog: prog("    NASA nsted-API Fallback...")
            url = ("https://exoplanetarchive.ipac.caltech.edu/cgi-bin/nstedAPI/nph-nstedAPI"
                   "?table=exoplanets&select=pl_hostname,ra,dec,st_vmag,pl_pnum&format=csv")
            raw = _text(url, timeout=90)
            seen = set()
            for row in csv.DictReader(io.StringIO(raw)):
                try:
                    nm = str(row.get("pl_hostname","")).strip()
                    if not nm or nm in seen: continue
                    seen.add(nm)
                    ra  = float(str(row.get("ra","")).strip())
                    dec = float(str(row.get("dec","")).strip())
                    try: mag = float(str(row.get("st_vmag","")).strip())
                    except: mag = None
                    try: npl = int(float(str(row.get("pl_pnum","")).strip()))
                    except: npl = 1
                    pl = "Planet" if npl == 1 else "Planeten"
                    objs.append({"id":nm,"catalog":"Exoplanet","ra":ra,"dec":dec,
                                 "magnitude":mag,"type":"exoplanet","name":nm,
                                 "description":f"Exoplanetensystem – {npl} {pl}"})
                except: pass
            if prog: prog(f"    Exoplaneten Fallback: {len(objs)}")
        except Exception as e:
            if prog: prog(f"    Exoplaneten Fallback fehlgeschlagen: {e}")
    if objs: _insert(conn, objs)
    return len(objs)


# ── Haupt ──────────────────────────────────────────────────────────────────────
def download_all(conn, prog=None):
    def p(m):
        if prog: prog(m)
        else: print(m)
    totals={}
    from solver import _messier_builtin
    p(">>> Messier..."); m=_messier_builtin(); _insert(conn,m); totals["Messier"]=len(m); p(f"    OK: {len(m)}")
    p(">>> NGC/IC...")
    try:
        _retry(lambda: download_ngcic(conn,p),n=3,delay=3,prog=p)
        totals["NGC"]=conn.execute("SELECT COUNT(*) FROM objects WHERE catalog='NGC'").fetchone()[0]
        totals["IC"]=conn.execute("SELECT COUNT(*) FROM objects WHERE catalog='IC'").fetchone()[0]
        p(f"    OK: NGC {totals['NGC']}  IC {totals['IC']}")
    except Exception as e: p(f"    FEHLER: {e}")
    p(">>> PGC...")
    try: n=_retry(lambda: download_pgc(conn,p),n=2,delay=5,prog=p); totals["PGC"]=n; p(f"    OK: {n}")
    except Exception as e: p(f"    FEHLER: {e}"); totals["PGC"]=0
    p(">>> Sterne...")
    try: n=_retry(lambda: download_stars(conn,p),n=2,delay=3,prog=p); totals["Tycho-2"]=n; p(f"    OK: {n}")
    except Exception as e: p(f"    FEHLER: {e}"); totals["Tycho-2"]=0
    p(">>> Gaia DR3...")
    try: n=_retry(lambda: download_gaia(conn,p),n=2,delay=5,prog=p); totals["Gaia DR3"]=n; p(f"    OK: {n}")
    except Exception as e: p(f"    FEHLER: {e}"); totals["Gaia DR3"]=0
    p(">>> Quasare...")
    try: n=_retry(lambda: download_quasars(conn,p),n=2,delay=4,prog=p); totals["Quasar"]=n; p(f"    OK: {n}")
    except Exception as e: p(f"    FEHLER: {e}"); totals["Quasar"]=0
    p(">>> Caldwell...")
    try: n=download_caldwell(conn,p); totals["Caldwell"]=n; p(f"    OK: {n}")
    except Exception as e: p(f"    FEHLER: {e}"); totals["Caldwell"]=0
    p(">>> Sharpless HII-Regionen...")
    try: n=_retry(lambda: download_sharpless(conn,p),n=2,delay=4,prog=p); totals["Sharpless"]=n; p(f"    OK: {n}")
    except Exception as e: p(f"    FEHLER: {e}"); totals["Sharpless"]=0
    p(">>> Abell-Galaxienhaufen...")
    try: n=_retry(lambda: download_abell(conn,p),n=2,delay=4,prog=p); totals["Abell"]=n; p(f"    OK: {n}")
    except Exception as e: p(f"    FEHLER: {e}"); totals["Abell"]=0
    p(">>> WHL2012 Galaxienhaufen (~132k)...")
    try: n=_retry(lambda: download_whl(conn,p),n=2,delay=5,prog=p); totals["WHL"]=n; p(f"    OK: {n}")
    except Exception as e: p(f"    FEHLER: {e}"); totals["WHL"]=0
    p(">>> redMaPPer Galaxienhaufen (~25k)...")
    try: n=_retry(lambda: download_redmapper(conn,p),n=2,delay=5,prog=p); totals["redMaPPer"]=n; p(f"    OK: {n}")
    except Exception as e: p(f"    FEHLER: {e}"); totals["redMaPPer"]=0
    p(">>> Planetarische Nebel...")
    try: n=_retry(lambda: download_planetary_nebulae(conn,p),n=2,delay=4,prog=p); totals["PN"]=n; p(f"    OK: {n}")
    except Exception as e: p(f"    FEHLER: {e}"); totals["PN"]=0
    p(">>> Arp Peculiare Galaxien...")
    try: n=_retry(lambda: download_arp(conn,p),n=2,delay=4,prog=p); totals["Arp"]=n; p(f"    OK: {n}")
    except Exception as e: p(f"    FEHLER: {e}"); totals["Arp"]=0
    p(">>> vdB Reflexionsnebel...")
    try: n=_retry(lambda: download_vdb(conn,p),n=2,delay=4,prog=p); totals["vdB"]=n; p(f"    OK: {n}")
    except Exception as e: p(f"    FEHLER: {e}"); totals["vdB"]=0
    p(">>> LDN Dunkelnebel...")
    try: n=_retry(lambda: download_ldn(conn,p),n=2,delay=4,prog=p); totals["LDN"]=n; p(f"    OK: {n}")
    except Exception as e: p(f"    FEHLER: {e}"); totals["LDN"]=0
    p(">>> LBN Hellnebel...")
    try: n=_retry(lambda: download_lbn(conn,p),n=2,delay=4,prog=p); totals["LBN"]=n; p(f"    OK: {n}")
    except Exception as e: p(f"    FEHLER: {e}"); totals["LBN"]=0
    p(">>> Barnard Dunkelnebel...")
    try: n=_retry(lambda: download_barnard(conn,p),n=2,delay=4,prog=p); totals["Barnard"]=n; p(f"    OK: {n}")
    except Exception as e: p(f"    FEHLER: {e}"); totals["Barnard"]=0
    p(">>> Supernova-Reste (SNR)...")
    try: n=_retry(lambda: download_snr(conn,p),n=2,delay=4,prog=p); totals["SNR"]=n; p(f"    OK: {n}")
    except Exception as e: p(f"    FEHLER: {e}"); totals["SNR"]=0
    p(">>> Collinder Sternhaufen...")
    try: n=_retry(lambda: download_collinder(conn,p),n=2,delay=4,prog=p); totals["Collinder"]=n; p(f"    OK: {n}")
    except Exception as e: p(f"    FEHLER: {e}"); totals["Collinder"]=0
    p(">>> Hickson Kompakte Gruppen...")
    try: n=download_hickson(conn,p); totals["Hickson"]=n; p(f"    OK: {n}")
    except Exception as e: p(f"    FEHLER: {e}"); totals["Hickson"]=0
    p(">>> WDS Doppelsterne...")
    try: n=_retry(lambda: download_wds(conn,p),n=2,delay=4,prog=p); totals["WDS"]=n; p(f"    OK: {n}")
    except Exception as e: p(f"    FEHLER: {e}"); totals["WDS"]=0
    p(">>> VSX Veränderliche Sterne...")
    try: n=_retry(lambda: download_vsx(conn,p),n=2,delay=4,prog=p); totals["VSX"]=n; p(f"    OK: {n}")
    except Exception as e: p(f"    FEHLER: {e}"); totals["VSX"]=0
    p(">>> Exoplaneten-Wirtssterne...")
    try: n=_retry(lambda: download_exoplanets(conn,p),n=2,delay=4,prog=p); totals["Exoplanet"]=n; p(f"    OK: {n}")
    except Exception as e: p(f"    FEHLER: {e}"); totals["Exoplanet"]=0
    ts=time.strftime("%Y-%m-%d %H:%M:%S")
    for name,cnt in totals.items(): conn.execute("INSERT OR REPLACE INTO catalog_meta VALUES (?,?,?)",(name,ts,cnt))
    conn.commit(); total=sum(totals.values()); p(f">>> Fertig: {total:,} Objekte gesamt")
    return totals
