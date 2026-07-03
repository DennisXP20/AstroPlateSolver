"""
Katalog-Import aus lokal heruntergeladenen Dateien.
Lege die heruntergeladenen Dateien in denselben Ordner wie diese Datei.
Dann: python import_catalogs.py
"""

import csv, io, gzip, sqlite3, math, time, os, sys
from pathlib import Path

BASE    = Path(__file__).parent
DB_PATH = BASE / "catalog.db"

def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS objects (
        id TEXT PRIMARY KEY, catalog TEXT NOT NULL,
        ra REAL NOT NULL, dec REAL NOT NULL,
        magnitude REAL, type TEXT, name TEXT, description TEXT)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dec ON objects(dec)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ra  ON objects(ra)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cat ON objects(catalog)")
    conn.execute("""CREATE TABLE IF NOT EXISTS catalog_meta (
        name TEXT PRIMARY KEY, downloaded_at TEXT, count INTEGER)""")
    conn.commit()
    return conn

def insert(conn, objects):
    conn.executemany("""INSERT OR REPLACE INTO objects
        (id,catalog,ra,dec,magnitude,type,name,description)
        VALUES (:id,:catalog,:ra,:dec,:magnitude,:type,:name,:description)""", objects)
    conn.commit()

def read_file(path):
    """Liest .gz oder normale Textdatei."""
    p = Path(path)
    if not p.exists():
        return None
    data = p.read_bytes()
    if data[:2] == b'\x1f\x8b':
        data = gzip.decompress(data)
    return data.decode("utf-8", errors="replace")


# ══════════════════════════════════════════════════════════════════════════════
# NGC/IC – OpenNGC (NGC.csv)
# ══════════════════════════════════════════════════════════════════════════════

def import_ngcic(conn):
    f = BASE / "NGC.csv"
    raw = read_file(f)
    if not raw:
        print(f"  FEHLT: {f}")
        print("  -> Download: https://raw.githubusercontent.com/mattiaverga/OpenNGC/master/database_files/NGC.csv")
        return 0

    type_map = {
        "G":"galaxy","GGroup":"galaxy","GPair":"galaxy","GTrpl":"galaxy","GClstr":"cluster",
        "OC":"cluster","OCl":"cluster","GC":"cluster","*Ass":"cluster",
        "PN":"nebula","SNR":"nebula","EN":"nebula","RN":"nebula","HII":"nebula",
        "Dup":"star","*":"star","D*":"star","**":"star",
    }
    objects = []
    for row in csv.DictReader(io.StringIO(raw), delimiter=";"):
        try:
            name = row.get("Name","").strip()
            if not name: continue
            rp = row["RA"].strip().split(":")
            dp = row["Dec"].strip()
            sign = -1 if dp.startswith("-") else 1
            dp = dp.lstrip("+-").split(":")
            ra  = (float(rp[0])+float(rp[1])/60+float(rp[2])/3600)*15
            dec = sign*(float(dp[0])+float(dp[1])/60+float(dp[2])/3600)
            try: mag = float(row.get("V-Mag") or row.get("B-Mag") or "x")
            except: mag = None
            t   = type_map.get(row.get("Type",""), "unknown")
            cat = "NGC" if name.startswith("N") else "IC"
            nice = name.replace("NGC","NGC ").replace("IC","IC ").strip()
            objects.append({"id":nice,"catalog":cat,"ra":ra,"dec":dec,"magnitude":mag,
                            "type":t,"name":nice,"description":row.get("Common names","").strip() or t})
        except: continue
    insert(conn, objects)
    print(f"  NGC/IC: {len(objects)} Objekte importiert")
    return len(objects)


# ══════════════════════════════════════════════════════════════════════════════
# PGC – pgc.csv oder pgc.csv.gz (von VizieR manuell heruntergeladen)
# ══════════════════════════════════════════════════════════════════════════════

def import_pgc(conn):
    # Versuche verschiedene Dateinamen
    candidates = ["pgc.csv", "pgc.csv.gz", "pgc.tsv", "VII_237_pgc.csv",
                  "vizier_pgc.csv", "pgc_catalog.csv"]
    raw = None
    found = None
    for name in candidates:
        raw = read_file(BASE / name)
        if raw:
            found = name
            break

    if not raw:
        print(f"  FEHLT: pgc.csv")
        print()
        print("  So herunterladen (Hinweis: VII/237 hat KEINE Bmag-Spalte):")
        print("  1. Diesen Link im Browser oeffnen (laedt ~80 MB):")
        print("     https://vizier.cds.unistra.fr/viz-bin/asu-tsv?-source=VII/237/pgc&-out=PGC,OType,MType,logD25,logR25&-out.add=_RAJ,_DEJ&-out.max=999999&-oc.form=dec")
        print("  2. Seite speichern als 'pgc.csv' in diesen Ordner")
        return 0

    print(f"  Lese {found} ({len(raw)} Bytes)...")
    objects = []

    # VizieR-Dateien: alle Kommentarzeilen (#) entfernen
    lines = [l for l in raw.splitlines() if not l.startswith("#")]
    raw_clean = "\n".join(lines).strip()

    # Trennzeichen erkennen
    sample = raw_clean[:2000]
    if sample.count(";") > sample.count("\t") and sample.count(";") > sample.count(","):
        delimiter = ";"
    elif sample.count("\t") >= sample.count(","):
        delimiter = "\t"
    else:
        delimiter = ","
    print(f"  Trennzeichen: '{delimiter}', erste Zeile: {raw_clean.splitlines()[0][:80] if raw_clean else '?'}")

    try:
        reader = csv.DictReader(io.StringIO(raw_clean), delimiter=delimiter)
        for row in reader:
            try:
                pgc = str(row.get("PGC", row.get("pgc", ""))).strip()
                if not pgc or pgc in ("---", "PGC", ""): continue
                # VizieR liefert Koordinaten als _RAJ2000/_DEJ2000 oder RAJ2000/DEJ2000
                ra_s  = str(row.get("_RAJ2000", row.get("RAJ2000", row.get("ra","")))).strip()
                dec_s = str(row.get("_DEJ2000", row.get("DEJ2000", row.get("dec","")))).strip()
                mag_s = str(row.get("Bmag", row.get("bmag", row.get("mag","")))).strip()
                if not ra_s or not dec_s: continue
                try: ra  = float(ra_s)
                except: continue
                try: dec = float(dec_s)
                except: continue
                try: mag = float(mag_s)
                except: mag = None
                pid = f"PGC {pgc}"
                # Morphologie-Typ (HyperLEDA MType, z.B. "Sb", "E", "SBc") und
                # Winkelgroesse D25 = 10^logD25 * 0.1 arcmin — der Typ im
                # Beschreibungstext wird von der Hubble-Sequenz-Karte als
                # Katalog-Klassifikation erkannt.
                desc = "Galaxie (PGC/HyperLEDA)"
                mt = str(row.get("MType", "")).strip()
                if mt and mt not in ("---", "MType"):
                    desc += f" · Typ {mt}"
                try:
                    d25 = (10.0 ** float(str(row.get("logD25", "")).strip())) * 0.1
                    if 0 < d25 < 1000: desc += f" · D25≈{d25:.1f}'"
                except (ValueError, TypeError): pass
                objects.append({"id":pid,"catalog":"PGC","ra":ra,"dec":dec,
                                "magnitude":mag,"type":"galaxy","name":pid,
                                "description":desc})
            except: continue
    except Exception as e:
        print(f"  Parse-Fehler: {e}")
        return 0

    insert(conn, objects)
    print(f"  PGC: {len(objects)} Objekte importiert")
    return len(objects)


# ══════════════════════════════════════════════════════════════════════════════
# Sterne – hip_main.csv oder yale_bsc.csv
# ══════════════════════════════════════════════════════════════════════════════

def import_stars(conn):
    # Eingebettete Sterne zuerst
    from catalog_dl import _builtin_stars
    builtin = _builtin_stars()
    insert(conn, builtin)
    print(f"  Eingebettete helle Sterne: {len(builtin)}")

    extra = 0
    # Hipparcos
    for fname in ["hip_main.csv", "hipparcos.csv", "hip_main.dat"]:
        raw = read_file(BASE / fname)
        if not raw: continue
        print(f"  Lese {fname}...")
        objects = []
        for row in csv.DictReader(io.StringIO(raw)):
            try:
                hip = str(row.get("HIP","")).strip()
                sid = f"HIP {hip}"
                # RArad in Radiant oder Grad?
                ra_s = str(row.get("RArad", row.get("RA",""))).strip()
                dec_s = str(row.get("DErad", row.get("Dec",""))).strip()
                vmag_s = str(row.get("Vmag","")).strip()
                try: vmag = float(vmag_s)
                except: continue
                if vmag > 10: continue
                ra_val = float(ra_s)
                dec_val = float(dec_s)
                # Wenn RArad in Bogenmass
                if ra_val < 7:  # < 2*pi -> Bogenmass
                    ra_val  = math.degrees(ra_val)
                    dec_val = math.degrees(dec_val)
                objects.append({"id":sid,"catalog":"Tycho-2","ra":ra_val,"dec":dec_val,
                                "magnitude":vmag,"type":"star","name":sid,
                                "description":f"Hipparcos V={vmag:.1f}"})
            except: continue
        insert(conn, objects)
        extra += len(objects)
        print(f"  Hipparcos: {len(objects)} Sterne")
        break

    if not extra:
        print("  Hinweis: Keine Hipparcos-Datei gefunden.")
        print("  Download:")
        print("  https://vizier.iucaa.in/viz-bin/asu-csv?-source=I/239/hip_main&-out=HIP,RArad,DErad,Vmag&Vmag=..9&-out.max=30000")
        print("  Speichern als 'hip_main.csv'")

    return conn.execute("SELECT COUNT(*) FROM objects WHERE catalog='Tycho-2'").fetchone()[0]


# ══════════════════════════════════════════════════════════════════════════════
# Quasare – quasars.csv
# ══════════════════════════════════════════════════════════════════════════════

def import_quasars(conn):
    """
    Importiert Quasare aus quasars.csv.
    Unterstützt SDSS DR16Q (Lyke+2020) mit GALEX UV + WISE IR Daten.
    Format: VizieR Semikolon-CSV
    Spalten: _RAJ2000;_DEJ2000;SDSS;z;umag;gmag;rmag;imag;zmag;FFUV;FNUV;FW1;FW2;Extu;Extg;Extr;...
    """
    candidates = ["quasars.csv", "dr16q.csv", "sdss_quasars.csv",
                  "vv10.csv", "veron_cetty.csv", "milliquas.csv"]
    raw = None; found = None
    for name in candidates:
        raw = read_file(BASE / name)
        if raw and len(raw) > 100: found = name; break

    if not raw:
        print("  FEHLT: quasars.csv")
        print("  Download (laedt ~100-350 MB, alle 750.000 DR16Q-Quasare):")
        print("  https://vizier.cds.unistra.fr/viz-bin/asu-tsv?-source=VII/289/dr16q"
              "&-out=SDSS,z,umag,gmag,rmag,imag,zmag,Extu,Extg,Extr,Exti,Extz,FFUV,FNUV,FW1,FW2"
              "&-out.add=_RAJ,_DEJ&-out.max=999999&-oc.form=dec")
        return 0

    print(f"  Lese {found} ({len(raw)//1024} KB)...")
    lines = [l for l in raw.splitlines() if l.strip() and not l.startswith("#")]
    raw_clean = "\n".join(lines).strip()
    sample = raw_clean[:3000]
    # Trennzeichen erkennen — asu-tsv liefert Tabs, manuelle Exporte oft Semikolons
    if sample.count(";") > sample.count("\t") and sample.count(";") > sample.count(","):
        delim = ";"
    elif sample.count("\t") >= sample.count(","):
        delim = "\t"
    else:
        delim = ","

    def _f(row, *keys):
        for k in keys:
            v = str(row.get(k,"")).strip()
            try:
                f = float(v)
                return f if 0 < f < 99 else None
            except: pass
        return None

    def _flux(row, *keys):
        for k in keys:
            v = str(row.get(k,"")).strip()
            try:
                f = float(v)
                return f if f > 0 else None
            except: pass
        return None

    objects = []
    skip_vals = {"deg","mag","---","3.63uJy","W/m2/Hz","309.05nJy","167.66nJy"}
    try:
        reader = csv.DictReader(io.StringIO(raw_clean), delimiter=delim)
        for row in reader:
            try:
                ra_s  = str(row.get("_RAJ2000", row.get("RAJ2000", row.get("RA_ICRS","")))).strip()
                dec_s = str(row.get("_DEJ2000", row.get("DEJ2000", row.get("DE_ICRS","")))).strip()
                if not ra_s or ra_s in skip_vals or "---" in ra_s: continue
                try: ra = float(ra_s); dec = float(dec_s)
                except: continue

                sdss = str(row.get("SDSS","")).strip()
                if sdss and sdss not in skip_vals and len(sdss) > 3:
                    qid = f"QSO {sdss}"
                elif "Seq" in row:
                    seq = str(row.get("Seq","")).strip()
                    if not seq or seq in skip_vals: continue
                    qid = f"QSO VV{seq}"
                else:
                    continue

                try: z = float(str(row.get("z","")).strip())
                except: z = None
                if z is not None and (z < 0 or z > 10): z = None

                gmag = _f(row,"gmag","g_mag"); rmag = _f(row,"rmag","r_mag")
                umag = _f(row,"umag","u_mag"); imag = _f(row,"imag","i_mag")
                zmag_v = _f(row,"zmag","z_mag")
                mag = gmag or rmag or _f(row,"Vmag")

                # GALEX & WISE (in Katalog-Einheiten)
                ffuv = _flux(row,"FFUV","fuv"); fnuv = _flux(row,"FNUV","nuv")
                fw1  = _flux(row,"FW1","w1");   fw2  = _flux(row,"FW2","w2")

                # Galaktische Extinktion (Schlafly & Finkbeiner 2011)
                extr = _f(row,"Extr"); extg = _f(row,"Extg")
                extu = _f(row,"Extu"); exti = _f(row,"Exti"); extz_v = _f(row,"Extz")

                # Beschreibung mit allen Daten
                desc_parts = []
                if z is not None:     desc_parts.append(f"z={z:.4f}")
                if gmag is not None:  desc_parts.append(f"g={gmag:.2f}")
                if rmag is not None:  desc_parts.append(f"r={rmag:.2f}")
                if ffuv is not None:  desc_parts.append(f"FUV={ffuv:.3f}")
                if fnuv is not None:  desc_parts.append(f"NUV={fnuv:.3f}")
                if fw1  is not None:  desc_parts.append(f"W1={fw1:.1f}")
                if fw2  is not None:  desc_parts.append(f"W2={fw2:.1f}")
                if extr is not None:  desc_parts.append(f"Extr={extr:.3f}")
                # SED-Rohdaten für Spektral-Darstellung im Modal
                sed = f"|SED:{ffuv or ''},{fnuv or ''},{umag or ''},{gmag or ''},{rmag or ''},{imag or ''},{zmag_v or ''},{fw1 or ''},{fw2 or ''},{extr or ''},{extg or ''}"
                desc = (" | ".join(desc_parts) if desc_parts else "Quasar (SDSS DR16Q)") + sed

                objects.append({"id":qid,"catalog":"Quasar","ra":ra,"dec":dec,
                                "magnitude":mag,"type":"quasar","name":qid,"description":desc})
            except: continue
    except Exception as e:
        print(f"  Parse-Fehler: {e}"); return 0

    if objects:
        insert(conn, objects)
        n_uv = sum(1 for o in objects if "FUV=" in o["description"])
        n_ir = sum(1 for o in objects if "W1=" in o["description"])
        print(f"  SDSS DR16Q: {len(objects):,} Quasare importiert")
        print(f"    mit GALEX UV: {n_uv:,} · mit WISE IR: {n_ir:,}")
    return len(objects)


# ══════════════════════════════════════════════════════════════════════════════
# Gaia DR3 – gaia.csv
# ══════════════════════════════════════════════════════════════════════════════

def import_gaia(conn):
    # Versuche zuerst automatischen Download (ESA TAP funktioniert meist)
    try:
        import urllib.request, urllib.parse
        print("  ESA Gaia TAP (automatisch)...")
        data = urllib.parse.urlencode({
            "REQUEST":"doQuery","LANG":"ADQL","FORMAT":"csv",
            "QUERY":"SELECT TOP 500000 source_id,ra,dec,phot_g_mean_mag FROM gaiadr3.gaia_source WHERE phot_g_mean_mag <= 16"
        }).encode()
        req = urllib.request.Request("https://gea.esac.esa.int/tap-server/tap/sync",
                                      data=data, headers={"User-Agent":"AstroSolver/10","Accept":"*/*"})
        req.add_header("Content-Type","application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=1000) as r:
            raw = r.read().decode("utf-8", errors="replace")
        if "<html" not in raw.lower()[:200] and len(raw) > 100:
            objects = []
            for row in csv.DictReader(io.StringIO(raw)):
                try:
                    gid = "Gaia " + str(row.get("source_id","")).strip()
                    try: mag = float(str(row.get("phot_g_mean_mag","")).strip())
                    except: mag = None
                    objects.append({"id":gid,"catalog":"Gaia DR3",
                                    "ra":float(row["ra"]),"dec":float(row["dec"]),
                                    "magnitude":mag,"type":"star","name":gid,
                                    "description":f"Gaia G={mag:.2f}" if mag else "Gaia"})
                except: continue
            insert(conn, objects)
            print(f"  Gaia DR3 (automatisch): {len(objects)} Sterne")
            return len(objects)
    except Exception as e:
        print(f"  ESA Gaia automatisch fehlgeschlagen: {e}")

    # Lokale Datei
    for fname in ["gaia.csv", "gaia_dr3.csv", "gaiadr3.csv"]:
        raw = read_file(BASE / fname)
        if not raw: continue
        objects = []
        for row in csv.DictReader(io.StringIO(raw)):
            try:
                gid = "Gaia " + str(row.get("source_id", row.get("Source",""))).strip()
                ra_s  = str(row.get("ra", row.get("RA_ICRS",""))).strip()
                dec_s = str(row.get("dec", row.get("DE_ICRS",""))).strip()
                mag_s = str(row.get("phot_g_mean_mag", row.get("Gmag",""))).strip()
                try: mag = float(mag_s)
                except: mag = None
                objects.append({"id":gid,"catalog":"Gaia DR3",
                                "ra":float(ra_s),"dec":float(dec_s),
                                "magnitude":mag,"type":"star","name":gid,
                                "description":f"Gaia G={mag:.2f}" if mag else "Gaia"})
            except: continue
        insert(conn, objects)
        print(f"  Gaia DR3: {len(objects)} Sterne")
        return len(objects)

    print("  FEHLT: gaia.csv")
    print("  Download:")
    print("  https://vizier.iucaa.in/viz-bin/asu-csv?-source=I/355/gaiadr3&-out=Source,RA_ICRS,DE_ICRS,Gmag&Gmag=..13&-out.max=80000&-oc.form=dec")
    print("  Speichern als 'gaia.csv'")
    return 0


# ══════════════════════════════════════════════════════════════════════════════
# Hauptprogramm
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print()
    print("=" * 55)
    print("  Astro Plate Solver – Katalog-Import")
    print("=" * 55)
    print(f"  Ordner: {BASE}")
    print()

    conn = init_db()
    totals = {}

    print(">>> Messier (eingebettet)...")
    try:
        sys.path.insert(0, str(BASE))
        from catalog_dl import _builtin_stars
        from solver import _messier_builtin
        m = _messier_builtin()
        insert(conn, m)
        totals["Messier"] = len(m)
        print(f"    OK: {len(m)}")
    except Exception as e:
        print(f"    FEHLER: {e}")

    print(">>> NGC/IC...")
    n = import_ngcic(conn)
    if n:
        totals["NGC"] = conn.execute("SELECT COUNT(*) FROM objects WHERE catalog='NGC'").fetchone()[0]
        totals["IC"]  = conn.execute("SELECT COUNT(*) FROM objects WHERE catalog='IC'").fetchone()[0]

    print(">>> PGC Galaxien...")
    n = import_pgc(conn)
    totals["PGC"] = n

    print(">>> Sterne...")
    n = import_stars(conn)
    totals["Tycho-2"] = n

    print(">>> Gaia DR3...")
    n = import_gaia(conn)
    totals["Gaia DR3"] = n

    print(">>> Quasare...")
    n = import_quasars(conn)
    totals["Quasar"] = n

    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    for name, cnt in totals.items():
        conn.execute("INSERT OR REPLACE INTO catalog_meta VALUES (?,?,?)", (name, ts, cnt))
    conn.commit()

    total = sum(totals.values())
    print()
    print("=" * 55)
    print(f"  Gesamt importiert: {total:,} Objekte")
    for k, v in totals.items():
        print(f"    {k:<15} {v:>8,}")
    print("=" * 55)
    print()

    if any(totals.get(k, 0) == 0 for k in ["PGC","Quasar"]):
        print("  Fehlende Kataloge:")
        print()
        if totals.get("PGC", 0) == 0:
            print("  PGC (Hintergrundgalaxien):")
            print("  Diesen Link im Browser oeffnen und als 'pgc.csv' speichern:")
            print("  https://vizier.cds.unistra.fr/viz-bin/asu-csv?-source=VII/237/pgc&-out=PGC,RAJ2000,DEJ2000,Bmag&Bmag=..17&-out.max=100000&-oc.form=dec")
            print()
            print("  Falls der Link nicht geht, diesen probieren (Japan Mirror):")
            print("  https://vizier.nao.ac.jp/viz-bin/asu-csv?-source=VII/237/pgc&-out=PGC,RAJ2000,DEJ2000,Bmag&Bmag=..17&-out.max=100000&-oc.form=dec")
            print()
        if totals.get("Quasar", 0) == 0:
            print("  Quasare (SDSS DR16Q):")
            print("  Diesen Link im Browser oeffnen und als 'quasars.csv' speichern:")
            print("  https://vizier.cds.unistra.fr/viz-bin/asu-csv?-source=VII/289/dr16q&-out=SDSS,RA_ICRS,DE_ICRS,gmag,z&gmag=..20&-out.max=100000&-oc.form=dec")
            print()
            print("  Falls der Link nicht geht:")
            print("  https://vizier.iucaa.in/viz-bin/asu-csv?-source=VII/258/vv10&-out=Seq,RAJ2000,DEJ2000,Vmag,z&Vmag=..19&-out.max=100000&-oc.form=dec")
            print("  Speichern als 'quasars.csv'")
            print()
        print("  Dann: python import_catalogs.py  erneut ausfuehren")

    input("\n  Enter druecken zum Beenden...")
