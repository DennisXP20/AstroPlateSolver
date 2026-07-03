"""
Astro Plate Solver – Server v1.0-rc1
Verwendet catalog_dl.py fuer robusteren Download
"""

import http.server, json, os, threading, time, base64, email.parser, email.policy
from pathlib import Path


def _parse_multipart(body: bytes, boundary: str) -> dict:
    """Parse a multipart/form-data body and return {name: bytes} for each part."""
    sep = ("--" + boundary).encode()
    end = ("--" + boundary + "--").encode()
    parts = {}
    segments = body.split(sep)
    for seg in segments[1:]:          # skip preamble
        if seg.strip() == b"--" or seg.startswith(b"--"):
            break                     # end boundary
        # Split headers from body on first \r\n\r\n
        if b"\r\n\r\n" not in seg:
            continue
        hdr_raw, content = seg.split(b"\r\n\r\n", 1)
        # Strip trailing \r\n before next boundary
        if content.endswith(b"\r\n"):
            content = content[:-2]
        # Parse headers
        hdr_text = hdr_raw.decode("utf-8", errors="replace").strip()
        name = None
        for line in hdr_text.splitlines():
            if line.lower().startswith("content-disposition:"):
                for token in line.split(";"):
                    token = token.strip()
                    if token.startswith('name="'):
                        name = token[6:].rstrip('"')
        if name is not None:
            parts[name] = content
    return parts

BASE = Path(__file__).parent
PORT = 8743

_solver = None
_catalog_dl = None
_photometry = None
_morph = None
_last_image_bytes = None   # Letztes gelöstes Bild (roh, JPEG/PNG), für /api/get_image
_last_image_mime  = "image/jpeg"

def get_solver():
    global _solver
    if _solver is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("solver", BASE/"solver.py")
        _solver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_solver)
    return _solver

def get_dl():
    global _catalog_dl
    if _catalog_dl is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("catalog_dl", BASE/"catalog_dl.py")
        _catalog_dl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_catalog_dl)
    return _catalog_dl

def get_phot():
    global _photometry
    if _photometry is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("photometry", BASE/"photometry.py")
        _photometry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_photometry)
    return _photometry

def get_morph():
    global _morph
    if _morph is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("galaxy_morph", BASE/"galaxy_morph.py")
        _morph = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_morph)
    return _morph

_sdss = None

def get_sdss():
    global _sdss
    if _sdss is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("sdss_query", BASE/"sdss_query.py")
        _sdss = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_sdss)
    return _sdss

_log = []
_downloading = False
_solving = False
_batch = {"done":0,"total":0,"running":False}

_LOGFILE = Path(__file__).parent / "server_errors.log"

def log(msg):
    ts = time.strftime("%H:%M:%S")
    e = f"[{ts}] {msg}"
    _log.append(e)
    if len(_log) > 600: _log.pop(0)
    print(e)
    try:
        with open(_LOGFILE, "a", encoding="utf-8") as f:
            f.write(e + "\n")
    except Exception:
        pass


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(200); self._cors(); self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._file(BASE/"index.html", "text/html; charset=utf-8")
        elif path == "/api/get_image":
            # Liefert das zuletzt gelöste Bild zurück (für Morphologie-KI etc.)
            # ohne dass der Client es erneut hochladen muss.
            if _last_image_bytes:
                self.send_response(200)
                self.send_header("Content-Type", _last_image_mime)
                self.send_header("Content-Length", str(len(_last_image_bytes)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(_last_image_bytes)
            else:
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"Kein Bild im Cache - bitte Bild erneut loesen"}')
        elif path == "/api/status":
            db = BASE/"catalog.db"
            sdss_stats = {}
            try:
                sdss_stats = get_sdss().cache_stats()
            except Exception:
                pass
            self._json({
                "catalog_ready": db.exists() and db.stat().st_size > 10000,
                "is_downloading": _downloading,
                "is_solving": _solving,
                "batch": dict(_batch),
                "log": _log[-60:],
                "db_mb": round(db.stat().st_size/1048576, 1) if db.exists() else 0,
                "sdss_cache": sdss_stats,
            })
        elif path == "/api/catalog_info":
            try:
                s = get_solver(); conn = s.init_db()
                counts = s.catalog_counts(conn); conn.close()
                self._json({"counts": counts, "total": sum(counts.values())})
            except Exception as e:
                self._json({"counts": {}, "total": 0, "error": str(e)})
        elif path == "/api/sdss_cache_stats":
            try:
                sdss = get_sdss()
                self._json(sdss.cache_stats())
            except Exception as e:
                self._json({"error": str(e)})
        elif path.startswith("/api/sdss_diagnose"):
            try:
                import urllib.parse as _up
                qs = _up.parse_qs(_up.urlparse(self.path).query)
                ra  = float(qs.get("ra",  ["186.5"])[0])
                dec = float(qs.get("dec", ["33.5" ])[0])
                sdss = get_sdss()
                log(f"[SDSS] Diagnose für RA={ra} Dec={dec}")
                self._json(sdss.diagnose(ra, dec))
            except Exception as e:
                import traceback
                self._json({"error": str(e), "tb": traceback.format_exc()[:500]})
        elif path.startswith("/api/catalog_diagnose"):
            try:
                import urllib.parse as _up
                qs = _up.parse_qs(_up.urlparse(self.path).query)
                ra  = float(qs.get("ra",  ["202.47"])[0])
                dec = float(qs.get("dec", ["47.19" ])[0])
                radius = float(qs.get("radius", ["3.0"])[0])
                catalog = qs.get("catalog", [None])[0]
                db = BASE / "catalog.db"
                if not db.exists():
                    self._json({"error": "catalog.db existiert nicht"}); 
                else:
                    import sqlite3, math
                    conn = sqlite3.connect(str(db))
                    cos_dec = max(math.cos(math.radians(dec)), 0.017)
                    d_ra = radius / cos_dec
                    ra_min, ra_max = ra - d_ra, ra + d_ra
                    dec_min, dec_max = dec - radius, dec + radius
                    sql = ("SELECT id,catalog,ra,dec,magnitude,type FROM objects "
                           "WHERE ra BETWEEN ? AND ? AND dec BETWEEN ? AND ?")
                    params = [ra_min, ra_max, dec_min, dec_max]
                    if catalog:
                        sql += " AND catalog = ?"
                        params.append(catalog)
                    sql += " ORDER BY catalog, id LIMIT 200"
                    rows = conn.execute(sql, params).fetchall()
                    # Zusätzlich: reine Katalog-Zählung gesamt (egal wo am Himmel)
                    cat_counts = dict(conn.execute(
                        "SELECT catalog, COUNT(*) FROM objects GROUP BY catalog"
                    ).fetchall())
                    conn.close()
                    self._json({
                        "query": {"ra": ra, "dec": dec, "radius_deg": radius,
                                  "ra_box": [round(ra_min,3), round(ra_max,3)],
                                  "dec_box": [round(dec_min,3), round(dec_max,3)]},
                        "found_in_box": len(rows),
                        "objects": [{"id":r[0],"catalog":r[1],"ra":r[2],"dec":r[3],
                                    "magnitude":r[4],"type":r[5]} for r in rows],
                        "total_catalog_counts": cat_counts,
                    })
            except Exception as e:
                import traceback
                self._json({"error": str(e), "tb": traceback.format_exc()[:500]})
        elif path == "/api/find_astap":
            s = get_solver()
            exe = s.find_astap()
            self._json({"found": exe is not None, "path": exe or ""})
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        if   path == "/api/download": self._download()
        elif path == "/api/solve":    self._solve(body)
        elif path == "/api/sdss_cone":         self._sdss_cone(body)
        elif path == "/api/sdss_cache_clear":  self._sdss_cache_clear(body)
        elif path == "/api/reset_catalog_db":  self._reset_catalog_db(body)
        elif path == "/api/export_docx":       self._export_docx(body)
        elif path == "/api/batch":    self._batch_start(body)
        elif path == "/api/photometry": self._photometry(body)
        elif path == "/api/morph_estimate": self._morph_estimate(body)
        elif path == "/api/galaxy_morph": self._galaxy_morph(body)
        elif path == "/api/galaxy_morph_clear_errors": self._galaxy_morph_clear_errors(body)
        elif path == "/api/skybot":        self._skybot(body)
        elif path == "/api/enrich_objects": self._enrich_objects(body)
        elif path == "/api/export_fits":   self._export_fits(body)
        elif path == "/api/photo_z":       self._photo_z(body)
        elif path == "/api/gaia_cone":     self._gaia_cone(body)
        elif path == "/api/bh_masses":     self._bh_masses(body)
        else: self.send_response(404); self.end_headers()

    def _sdss_cone(self, body):
        """
        Direkte SDSS-Kegelabfrage ohne Plate-Solving.
        Nützlich für manuelle Koordinaten oder als Debug-Endpunkt.

        POST JSON:
          ra          – Feldmitte RA (Grad)
          dec         – Feldmitte Dec (Grad)
          radius_deg  – Suchradius (Grad)
          mag_limit   – r-Band Magnitude-Limit (10–22.5, vom Nutzer eingestellt)
        """
        try: payload = json.loads(body)
        except: self._err(400, "JSON Fehler"); return

        ra         = float(payload.get("ra", 0))
        dec        = float(payload.get("dec", 0))
        radius_deg = float(payload.get("radius_deg", 0.5))
        mag_limit  = float(payload.get("mag_limit", 20.0))

        # Magnitude plausibel halten
        mag_limit = max(10.0, min(mag_limit, 22.5))

        log(f"[SDSS] Kegel: RA={ra:.4f} Dec={dec:.4f} r={radius_deg:.3f}° "
            f"mag<={mag_limit:.1f}")

        try:
            sdss = get_sdss()
            slogs = []
            def prog(m): slogs.append(m); log(m)
            objects = sdss.query_sdss_for_field(ra, dec, radius_deg, mag_limit,
                                                progress_cb=prog)
            stats = sdss.cache_stats()
            self._json({
                "ok": True,
                "count": len(objects),
                "objects": objects,
                "mag_limit": mag_limit,
                "cache_stats": stats,
                "log": slogs,
            })
        except Exception as e:
            import traceback
            log(f"[SDSS] Fehler: {e}")
            self._err(500, f"{e}\n{traceback.format_exc()[:400]}")

    def _sdss_cache_clear(self, body):
        try:
            payload = json.loads(body) if body and body.strip() else {}
        except Exception:
            payload = {}
        days = payload.get("older_than_days", None)
        clear_all = payload.get("clear_all", False)
        if days is not None:
            days = int(days)
        # clear_all=True oder kein older_than_days → komplett löschen
        if clear_all:
            days = None
        try:
            sdss = get_sdss()
            vacuum_result = sdss.invalidate_cache(older_than_days=days)
            stats = sdss.cache_stats()
            before = vacuum_result.get("size_before_mb", 0)
            after  = vacuum_result.get("size_after_mb", 0)
            saved  = round(before - after, 1)
            base_msg = (f"Cache gelöscht (Tiles > {days} Tage)" if days is not None
                        else "Cache komplett geleert")
            if saved > 0.1:
                msg = f"{base_msg} – Datei verkleinert: {before} MB → {after} MB (−{saved} MB)"
            else:
                msg = f"{base_msg} ({after} MB)"
            log(f"[SDSS] {msg}")
            self._json({"ok": True, "msg": msg, "cache_stats": stats,
                        "size_before_mb": before, "size_after_mb": after})
        except Exception as e:
            import traceback
            log(f"[SDSS] Cache-Fehler: {traceback.format_exc()[:300]}")
            self._err(500, str(e))

    def _reset_catalog_db(self, body):
        """
        Löscht die lokale Katalog-Datenbank (catalog.db) komplett.
        Nützlich bei korrupten/veralteten Daten oder um mit einem sauberen
        Stand neu zu importieren. Der Nutzer muss danach "Kataloge
        herunterladen" erneut klicken.
        """
        db_path = BASE / "catalog.db"
        try:
            size_mb = round(db_path.stat().st_size / 1_048_576, 1) if db_path.exists() else 0
            if db_path.exists():
                db_path.unlink()
            # Zugehörige WAL/SHM-Hilfsdateien ebenfalls entfernen falls vorhanden
            for suffix in ("-wal", "-shm"):
                p = BASE / f"catalog.db{suffix}"
                if p.exists():
                    p.unlink()
            log(f"[DB] Katalog-Datenbank gelöscht ({size_mb} MB freigegeben)")
            self._json({"ok": True, "msg": f"Datenbank gelöscht ({size_mb} MB freigegeben). "
                                            f"Bitte „Kataloge herunterladen\" erneut klicken."})
        except Exception as e:
            import traceback
            log(f"[DB] Reset-Fehler: {traceback.format_exc()[:300]}")
            self._err(500, str(e))

    def _export_docx(self, body):
        """
        Erzeugt ein Word-Dokument mit Bild-Ausschnitt und Daten für jedes Objekt.

        POST JSON:
          result     – Das solve_field()-Ergebnis (objects, wcs_info, ...)
          image_b64  – Das Originalbild als Base64 (JPEG oder PNG)
          max_objects – Max. Anzahl Objekte (Standard: 500)
          filter_type – Optional: nur Objekte eines Typs ("galaxy", "star", ...)
        """
        import importlib.util, base64, traceback
        try:
            payload = json.loads(body) if body else {}
        except Exception:
            self._err(400, "JSON Fehler"); return

        result      = payload.get("result", {})
        image_b64   = payload.get("image_b64", "")
        max_obj     = int(payload.get("max_objects", 500))
        filter_type = payload.get("filter_type", "")

        if not result or not result.get("objects"):
            self._err(400, "Keine Objekte – erst Bild lösen"); return

        # Filter anwenden wenn gewünscht
        if filter_type:
            result = dict(result)
            result["objects"] = [o for o in result["objects"]
                                  if o.get("type") == filter_type]

        try:
            image_bytes = base64.b64decode(image_b64) if image_b64 else b""
        except Exception:
            image_bytes = b""

        # export_docx-Modul laden
        spec = importlib.util.spec_from_file_location(
            "export_docx", BASE / "export_docx.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        slogs = []
        def prog(m):
            slogs.append(m)
            log(m)

        try:
            buf = mod.generate_report(result, image_bytes,
                                       progress_cb=prog,
                                       max_objects=max_obj)
        except Exception as e:
            log(f"[DOCX] Fehler: {traceback.format_exc()[:400]}")
            self._err(500, f"DOCX-Fehler: {e}"); return

        # Als Datei senden
        docx_bytes = buf.read()
        fname = f"Plate_Solver_Report_{len(result.get('objects',[]))}obj.docx"
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
        self.send_header("Content-Length", str(len(docx_bytes)))
        self.end_headers()
        self.wfile.write(docx_bytes)

    def _galaxy_morph(self, body):
        try: payload = json.loads(body)
        except: self._err(400, "JSON Fehler"); return
        names = payload.get("names") or []
        if not isinstance(names, list) or not names:
            self._err(400, "Keine Namen"); return
        # names kann Strings ODER {name, ra, dec}-Dicts sein
        cleaned = []
        for entry in names[:300]:
            if isinstance(entry, dict):
                n = str(entry.get("name","")).strip()
                if n: cleaned.append({"name": n,
                                       "ra":  entry.get("ra"),
                                       "dec": entry.get("dec")})
            else:
                n = str(entry).strip()
                if n: cleaned.append(n)
        names = cleaned
        try:
            m = get_morph()
            def prog(msg): log("[Morph] " + msg)
            # names kann jetzt Strings ODER {name, ra, dec}-Dicts sein
            results = m.lookup_batch(names, progress_cb=prog, max_queries=150)
            self._json({"ok": True, "results": results})
        except Exception as e:
            import traceback
            log(f"Morph-Fehler: {e}")
            self._err(500, f"{e}\n{traceback.format_exc()[:400]}")

    def _galaxy_morph_clear_errors(self, body):
        """
        Loescht veraltete, faelschlich permanent gecachte 'kein Treffer'-
        Eintraege aus morph_cache.db (vor dem SSL-Fix entstanden). Danach
        werden diese Namen beim naechsten 'Online suchen' erneut versucht.
        """
        try:
            m = get_morph()
            n = m.clear_error_cache()
            log(f"[Morph] {n} veraltete Fehler-Cache-Einträge gelöscht")
            self._json({"ok": True, "cleared": n})
        except Exception as e:
            self._err(500, str(e))

    def _photometry(self, body):
        if not (BASE/"catalog.db").exists():
            self._err(400, "Katalog fehlt"); return

        ct = self.headers.get("Content-Type", "")

        if "multipart/form-data" in ct:
            # Neues Format: FormData mit 'params' (JSON) und 'file' (Roh-Bildbytes)
            try:
                bnd = ct.split("boundary=")[1].split(";")[0].strip().strip('"')
            except IndexError:
                self._err(400, "Kein Multipart-Boundary"); return
            try:
                parts = _parse_multipart(body, bnd)
            except Exception as e:
                self._err(400, f"Multipart-Fehler: {e}"); return
            if "params" not in parts or "file" not in parts:
                self._err(400, f"Fehlende Multipart-Teile (vorhanden: {list(parts.keys())})"); return
            try:
                payload = json.loads(parts["params"].decode("utf-8"))
            except Exception as e:
                self._err(400, f"params-JSON Fehler: {e}"); return
            img_bytes = parts["file"]

        elif self.headers.get("X-Phot-Params"):
            # Legacy-Format: Parameter im Header, Roh-Bytes im Body
            try: payload = json.loads(self.headers.get("X-Phot-Params"))
            except: self._err(400, "X-Phot-Params JSON Fehler"); return
            img_bytes = body

        else:
            # Aeltestes Format: JSON mit image_b64
            try: payload = json.loads(body)
            except: self._err(400, "JSON Fehler"); return
            img_b64 = payload.get("image_b64", "")
            if not img_b64: self._err(400, "Kein Bild"); return
            img_bytes = base64.b64decode(img_b64)

        filename = payload.get("filename", "img.jpg")
        wcs_info = payload.get("wcs_info")
        r_ap     = float(payload.get("r_ap", 5.0))
        r_in     = float(payload.get("r_in", 8.0))
        r_out    = float(payload.get("r_out", 12.0))
        mag_lim  = float(payload.get("mag_limit", 16.0))
        refs     = payload.get("ref_catalogs") or ["Tycho-2","Gaia DR3"]
        ref_objs = payload.get("ref_objects") or None
        if not wcs_info: self._err(400, "Keine WCS-Info - erst Bild loesen"); return
        tmp = None
        try:
            suffix = Path(filename).suffix or ".jpg"
            tmp = BASE / f"_tmp_phot_{int(time.time())}{suffix}"
            tmp.write_bytes(img_bytes)
            s = get_solver(); p = get_phot()
            conn = s.init_db()
            plogs = []
            def prog(m): plogs.append(m); log("[Phot] "+m)
            result = p.run_photometry(str(tmp), wcs_info, conn, s,
                                      r_ap=r_ap, r_in=r_in, r_out=r_out,
                                      mag_limit=mag_lim, ref_catalogs=refs,
                                      ref_objects=ref_objs,
                                      progress_cb=prog)
            conn.close()
            result["log"] = plogs
            self._json(result)
        except Exception as e:
            import traceback
            log(f"Photometrie-Fehler: {e}")
            self._err(500, f"{e}\n{traceback.format_exc()[:500]}")
        finally:
            if tmp is not None:
                try: tmp.unlink()
                except: pass

    def _morph_estimate(self, body):
        """
        Bildbasierte morphologische Schätzung für eine oder mehrere Galaxien.
        Erwartet JSON: {image_b64, objects: [{id, x, y}, ...], box_radius_px?}
        Gibt zurück: [{id, type, label, confidence, features, method}, ...]
        """
        import tempfile, base64
        tmp = None
        try:
            payload = json.loads(body)
        except:
            self._err(400, "JSON Fehler"); return
        try:
            import importlib.util as ilu
            spec = ilu.spec_from_file_location("morph_estimate", BASE/"morph_estimate.py")
            me = ilu.module_from_spec(spec); spec.loader.exec_module(me)

            b64 = payload.get("image_b64","")
            objs = payload.get("objects",[])
            box_r = float(payload.get("box_radius_px", 60))

            if not b64:
                self._err(400, "image_b64 fehlt"); return
            if not objs:
                self._json([]); return

            # Bild in temporäre Datei schreiben
            raw = base64.b64decode(b64)
            suf = ".jpg" if raw[:3]==b'\xff\xd8\xff' else ".png"
            tmp = Path(tempfile.mktemp(suffix=suf))
            tmp.write_bytes(raw)

            log(f"[Morph] Bildanalyse für {len(objs)} Objekte, box={box_r}px ...")
            results = me.estimate_batch(str(tmp), objs,
                                         progress_cb=lambda m: log(f"[Morph] {m}"))
            n_ok = sum(1 for r in results if r.get("type"))
            log(f"[Morph] Fertig: {n_ok}/{len(objs)} Typen geschätzt")
            self._json(results)
        except Exception as e:
            import traceback
            log(f"[Morph] Fehler: {e}")
            self._err(500, f"{e}\n{traceback.format_exc()[:400]}")
        finally:
            if tmp is not None:
                try: tmp.unlink()
                except: pass

    def _skybot(self, body):
        """SkyBot-Anfrage an IMCCE: Sonnensystem-Objekte im Bildfeld suchen."""
        import urllib.request, urllib.parse, math
        try: payload = json.loads(body)
        except: self._err(400, "JSON Fehler"); return

        ra      = float(payload.get("ra", 0))        # Feldmitte RA (Grad, J2000)
        dec     = float(payload.get("dec", 0))       # Feldmitte Dec (Grad, J2000)
        sr      = float(payload.get("radius_deg", 1.0))  # Feldhalbdiagonale (Grad)
        epoch   = str(payload.get("epoch", "")).strip()   # ISO UTC (Mittelpunkt Bel.)
        mag_lim = float(payload.get("mag_limit", 21.0))
        observer= str(payload.get("observer", "500"))     # MPC-Code; 500=geozentrisch

        if not epoch:
            self._err(400, "Kein Beobachtungszeitpunkt (epoch) angegeben"); return

        log(f"[SkyBot] RA={ra:.4f} Dec={dec:.4f} SR={sr:.3f}° Epoch={epoch}")

        params = urllib.parse.urlencode({
            "EPOCH":   epoch,
            "RA":      f"{ra:.6f}",
            "DEC":     f"{dec:+.6f}",
            "SR":      f"{sr:.4f}",
            "-mime":   "text",
            "-output": "all",
            "-filter": f"{mag_lim:.1f}",
            "-refsys": "EQJ2000",
            "-observer": observer,
        })
        url = f"https://vo.imcce.fr/webservices/skybot/skybotconesearch_query.php?{params}"

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "AstroSolver/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read().decode("utf-8", errors="replace")
        except Exception as e:
            self._err(502, f"SkyBot nicht erreichbar: {e}"); return

        # Prüfe auf IMCCE-Fehlermeldung
        if "!No solar system object" in raw or "No object was found" in raw:
            self._json({"ok": True, "count": 0, "objects": [],
                        "epoch": epoch, "note": "Keine Objekte gefunden"}); return
        if raw.strip().startswith("!") or "Error" in raw[:200]:
            self._err(502, f"SkyBot Fehler: {raw[:300]}"); return

        objects = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("!"):
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 7:
                continue
            try:
                num     = parts[0].strip()
                name    = parts[1].strip()
                ra_str  = parts[2].strip()   # "hh mm ss.ss"
                dec_str = parts[3].strip()   # "+dd mm ss.s"
                cls     = parts[4].strip()
                mv_str  = parts[5].strip()
                err_str = parts[6].strip()
                # Optionale Felder
                pm_ra_str  = parts[8].strip()  if len(parts) > 8  else ""
                pm_dec_str = parts[9].strip()  if len(parts) > 9  else ""
                dg_str     = parts[10].strip() if len(parts) > 10 else ""
                dh_str     = parts[11].strip() if len(parts) > 11 else ""

                # RA: "hh mm ss.ss" → Grad
                rp = ra_str.split()
                if len(rp) == 3:
                    ra_deg = (float(rp[0]) + float(rp[1])/60 + float(rp[2])/3600) * 15.0
                else:
                    ra_deg = float(ra_str) * 15.0

                # Dec: "+dd mm ss.s" → Grad
                dp = dec_str.split()
                if len(dp) == 3:
                    sign = -1.0 if dp[0].startswith("-") else 1.0
                    dec_deg = sign * (abs(float(dp[0])) + float(dp[1])/60 + float(dp[2])/3600)
                else:
                    dec_deg = float(dec_str)

                def _f(s):
                    try: return float(s)
                    except: return None

                mv      = _f(mv_str)
                err_as  = _f(err_str)   # Ephemeriden-Unsicherheit in arcsec
                pm_ra   = _f(pm_ra_str)  # arcsec/h in RA·cos(Dec)
                pm_dec  = _f(pm_dec_str) # arcsec/h in Dec
                d_geo   = _f(dg_str)     # Erdabstand in AU
                d_sol   = _f(dh_str)     # Sonnenabstand in AU

                # Gesamtbewegung arcsec/h
                pm_total = math.sqrt(pm_ra**2 + pm_dec**2) if (pm_ra and pm_dec) else None

                # Konfidenz-Flag anhand Unsicherheit
                if err_as is None:       confidence = "unknown"
                elif err_as <= 5:        confidence = "high"
                elif err_as <= 60:       confidence = "medium"
                else:                    confidence = "low"

                # Topozentrische Warnung: relevant wenn Erdabstand < 0.3 AU
                topo_warn = (d_geo is not None and d_geo < 0.3)

                objects.append({
                    "num":        num,
                    "name":       name,
                    "ra":         ra_deg,
                    "dec":        dec_deg,
                    "class":      cls,
                    "mag":        mv,
                    "err_arcsec": err_as,
                    "pm_ra":      pm_ra,
                    "pm_dec":     pm_dec,
                    "pm_total":   pm_total,
                    "dist_geo_au":d_geo,
                    "dist_sun_au":d_sol,
                    "confidence": confidence,
                    "topo_warn":  topo_warn,
                })
            except Exception as ex:
                log(f"[SkyBot] Parse-Fehler Zeile '{line}': {ex}")
                continue

        log(f"[SkyBot] {len(objects)} Objekte gefunden")
        self._json({"ok": True, "count": len(objects),
                    "objects": objects, "epoch": epoch,
                    "geocentric": observer == "500"})

    def _enrich_objects(self, body):
        """Reichert Objekte mit SIMBAD + VizieR Gaia DR3 Daten für 3D-Export an."""
        try: payload = json.loads(body)
        except: self._err(400, "JSON Fehler"); return

        objects = payload.get("objects", [])
        if not objects:
            self._json({"ok": True, "results": {}, "count": 0}); return

        log(f"[Enrich] Anreicherung gestartet: {len(objects)} Objekte")
        try:
            s = get_solver()
            results = s.enrich_for_3d(objects, progress_cb=log)
            log(f"[Enrich] Abgeschlossen: {len(results)} Objekte angereichert")
            self._json({"ok": True, "results": results, "count": len(results)})
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            log(f"[Enrich] Fehler: {e}")
            self._err(500, f"{e}\n{tb[:400]}")

    def _download(self):
        global _downloading
        if _downloading:
            self._json({"ok": False, "msg": "Download laeuft bereits"}); return
        _downloading = True
        threading.Thread(target=self._do_download, daemon=True).start()
        self._json({"ok": True, "msg": "Download gestartet"})

    def _do_download(self):
        global _downloading
        try:
            s = get_solver()
            dl = get_dl()
            conn = s.init_db()
            log("=== Katalog-Download gestartet ===")
            results = dl.download_all(conn, prog=log)
            log(f"=== Fertig: {sum(results.values()):,} Objekte ===")
            conn.close()
        except Exception as e:
            import traceback
            log(f"Download-Fehler: {e}\n{traceback.format_exc()[:300]}")
        finally:
            _downloading = False

    def _solve(self, body):
        global _solving
        if not (BASE/"catalog.db").exists():
            self._err(400, "Katalog fehlt – erst herunterladen"); return

        # FormData oder JSON akzeptieren
        ct = self.headers.get("Content-Type", "")
        img_bytes = None
        if "multipart/form-data" in ct:
            bnd = ct.split("boundary=")[-1].strip()
            try:
                parts = _parse_multipart(body, bnd)
            except Exception as e:
                self._err(400, f"Multipart-Fehler: {e}"); return
            if "params" not in parts or "file" not in parts:
                self._err(400, "Fehlende Multipart-Teile"); return
            try:
                payload = json.loads(parts["params"].decode("utf-8"))
            except Exception as e:
                self._err(400, f"params-JSON Fehler: {e}"); return
            img_bytes = parts["file"]
        else:
            try: payload = json.loads(body)
            except: self._err(400, "Ungueltiges JSON"); return
            img_b64 = payload.get("image_b64", "")
            if not img_b64: self._err(400, "Kein Bild"); return
            img_bytes = base64.b64decode(img_b64)

        filename   = payload.get("filename", "img.jpg")
        mag_limit  = float(payload.get("mag_limit", 19.0))
        active     = payload.get("active_catalogs", None)
        manual_wcs = payload.get("manual_wcs", None)
        astap_path = payload.get("astap_path", None)
        use_sdss   = payload.get("use_sdss", False)
        sdss_mag   = float(payload.get("sdss_mag_limit", mag_limit))

        s = get_solver()
        astap_exe = astap_path or s.find_astap()

        _solving = True; slogs = []
        try:
            # Bild global cachen damit /api/get_image es ohne erneuten Upload liefern kann
            global _last_image_bytes, _last_image_mime
            _last_image_bytes = img_bytes
            _last_image_mime  = "image/jpeg" if img_bytes[:2]==b'\xff\xd8' else "image/png"
            suffix = Path(filename).suffix or ".jpg"
            tmp = BASE / f"_tmp_{int(time.time())}{suffix}"
            tmp.write_bytes(img_bytes)
            conn = s.init_db()
            def prog(m): slogs.append(m); log(m)
            result = s.solve_field(str(tmp), conn,
                                   mag_limit=mag_limit,
                                   active_catalogs=active,
                                   manual_wcs=manual_wcs,
                                   astap_exe=astap_exe,
                                   progress_cb=prog)
            conn.close()

            # ── SDSS DR17 Integration ──────────────────────────────────────
            if use_sdss and result.get("status") == "solved":
                wcs   = result.get("wcs_info", {})
                ra_c  = wcs.get("ra_center")
                dec_c = wcs.get("dec_center")
                scale = wcs.get("scale_deg_per_px", 0)
                img_w = int(wcs.get("img_w", 3000))
                img_h = int(wcs.get("img_h", 2000))
                import math as _math
                fw     = scale * img_w
                fh     = scale * img_h
                radius = _math.sqrt(fw**2 + fh**2) / 2 * 1.1 if (fw > 0 and fh > 0) else 0

                if ra_c is not None and dec_c is not None and radius > 0:
                    prog(f"[SDSS] Starte Abfrage: mag<={sdss_mag:.1f} Radius={radius:.3f}°")
                    try:
                        sdss = get_sdss()
                        sdss_objs_raw = sdss.query_sdss_for_field(
                            ra_c, dec_c, radius, sdss_mag, progress_cb=prog
                        )

                        # WCS-Objekt aufbauen
                        has_cd = wcs.get("has_full_cd", False)
                        try:
                            if has_cd:
                                wcs_obj = s.TanWCS(
                                    ra_c, dec_c,
                                    scale,
                                    wcs.get("rotation_deg", 0),
                                    img_w / 2, img_h / 2,
                                    cd11=wcs["cd11"], cd12=wcs["cd12"],
                                    cd21=wcs["cd21"], cd22=wcs["cd22"],
                                    crval1=wcs["crval1"], crval2=wcs["crval2"],
                                    crpix1=wcs["crpix1"], crpix2=wcs["crpix2"],
                                )
                            else:
                                wcs_obj = s.TanWCS(
                                    ra_c, dec_c,
                                    scale,
                                    wcs.get("rotation_deg", 0),
                                    img_w / 2, img_h / 2,
                                )
                            use_wcs_proj = True
                        except Exception as e_wcs:
                            prog(f"[SDSS] WCS-Objekt nicht verfügbar ({e_wcs}), "
                                 f"verwende lineare Projektion")
                            use_wcs_proj = False

                        # Pixel-Koordinaten berechnen
                        sdss_placed = []
                        rot_rad = _math.radians(wcs.get("rotation_deg", 0))
                        cos_r, sin_r = _math.cos(rot_rad), _math.sin(rot_rad)

                        for obj in sdss_objs_raw:
                            if use_wcs_proj:
                                try:
                                    px, py = wcs_obj.world_to_pixel(obj["ra"], obj["dec"])
                                except Exception:
                                    continue
                            else:
                                # Einfache lineare Näherung als Fallback
                                cos_dec = max(_math.cos(_math.radians(dec_c)), 0.017)
                                dra  = (obj["ra"]  - ra_c) * cos_dec
                                ddec = (obj["dec"] - dec_c)
                                px_raw =  dra  / scale
                                py_raw = -ddec / scale
                                px = img_w/2 + cos_r*px_raw - sin_r*py_raw
                                py = img_h/2 + sin_r*px_raw + cos_r*py_raw

                            if -50 <= px <= img_w + 50 and -50 <= py <= img_h + 50:
                                sdss_placed.append({
                                    "id":          obj["id"],
                                    "catalog":     "SDSS DR17",
                                    "type":        obj["type"],
                                    "ra":          round(obj["ra"],  6),
                                    "dec":         round(obj["dec"], 6),
                                    "magnitude":   obj.get("magnitude"),
                                    "description": obj.get("description", ""),
                                    "x":           round(px, 1),
                                    "y":           round(py, 1),
                                    "x_frac":      round(px / img_w, 5),
                                    "y_frac":      round(py / img_h, 5),
                                    "sdss_mag_u":  obj.get("sdss_mag_u"),
                                    "sdss_mag_g":  obj.get("sdss_mag_g"),
                                    "sdss_mag_r":  obj.get("sdss_mag_r"),
                                    "sdss_mag_i":  obj.get("sdss_mag_i"),
                                    "sdss_mag_z":  obj.get("sdss_mag_z"),
                                    "sdss_type":   obj.get("sdss_type"),
                                    "redshift_z":  obj.get("redshift_z"),
                                    "redshift_err":obj.get("redshift_err"),
                                    "spec_class":  obj.get("spec_class"),
                                    "photoz":      obj.get("photoz"),
                                    "photoz_err":  obj.get("photoz_err"),
                                })

                        result["objects"]    = result.get("objects", []) + sdss_placed
                        result["sdss_count"] = len(sdss_placed)
                        # Footprint-Info: war das Feld überhaupt im SDSS-Footprint?
                        in_fp = sdss_objs_raw[0].get("_sdss_in_footprint", True) if sdss_objs_raw else None
                        if in_fp is False and len(sdss_placed) == 0:
                            result["sdss_footprint_warning"] = (
                                f"Das Bildfeld (RA={ra_c:.1f}° Dec={dec_c:.1f}°) liegt "
                                f"außerhalb des SDSS-Footprints. SDSS deckt nur ~1/3 des "
                                f"Himmels ab (hauptsächlich Dec 0°–70°, RA 100°–270°)."
                            )
                            prog(f"[SDSS] Footprint-Warnung ins Ergebnis geschrieben")
                        prog(f"[SDSS] {len(sdss_placed)} Objekte hinzugefügt")

                    except Exception as e_sdss:
                        import traceback
                        prog(f"[SDSS] Fehler (Solver-Ergebnis trotzdem gültig): {e_sdss}")
                        log(f"[SDSS] {traceback.format_exc()[:500]}")
                        result["sdss_error"] = str(e_sdss)
            # ── Ende SDSS ──────────────────────────────────────────────────

            try: tmp.unlink()
            except: pass
            result["log"] = slogs
            self._json(result)
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            log(f"Solve-Fehler: {e}")
            try:
                with open(_LOGFILE, "a", encoding="utf-8") as f:
                    f.write(f"=== SOLVE EXCEPTION ===\n{tb}\n")
            except Exception:
                pass
            self._err(500, f"{e}\n{tb[:500]}")
            try: tmp.unlink()
            except: pass
        finally:
            _solving = False

    def _batch_start(self, body):
        try: payload = json.loads(body)
        except: self._err(400, "JSON Fehler"); return
        paths = payload.get("paths", [])
        if not paths: self._err(400, "Keine Pfade"); return
        mag   = float(payload.get("mag_limit", 19.0))
        active = payload.get("active_catalogs", None)
        out_dir = Path(payload.get("output_dir", str(BASE/"output")))
        out_dir.mkdir(parents=True, exist_ok=True)

        def run():
            _batch.update({"done": 0, "total": len(paths), "running": True})
            s = get_solver(); conn = s.init_db()
            astap_exe = s.find_astap()
            results = []
            for i, p in enumerate(paths):
                log(f"[Batch {i+1}/{len(paths)}] {p}")
                try:
                    r = s.solve_field(p, conn, mag_limit=mag,
                                      active_catalogs=active,
                                      astap_exe=astap_exe, progress_cb=log)
                    r["file"] = p
                    (out_dir/f"{Path(p).stem}_solved.json").write_text(
                        json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
                    results.append({"file":p,"status":r.get("status"),"objects":len(r.get("objects",[]))})
                except Exception as e:
                    log(f"  Fehler: {e}")
                    results.append({"file":p,"status":"error","error":str(e)})
                _batch["done"] = i + 1
            conn.close()
            (out_dir/"batch_summary.json").write_text(
                json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            log(f"[Batch] Fertig: {len(paths)} Bilder")
            _batch["running"] = False

        threading.Thread(target=run, daemon=True).start()
        self._json({"ok": True, "msg": f"Batch: {len(paths)} Bilder", "output": str(out_dir)})

    def _export_fits(self, body):
        """Annotated FITS export mit WCS-Headern und Katalogobjekten."""
        import traceback
        ct = self.headers.get("Content-Type", "")
        if "multipart/form-data" in ct:
            bnd = ct.split("boundary=")[-1].strip()
            try:
                parts = _parse_multipart(body, bnd)
            except Exception as e:
                self._err(400, f"Multipart-Fehler: {e}"); return
            if "params" not in parts or "file" not in parts:
                self._err(400, "Fehlende Teile"); return
            try: payload = json.loads(parts["params"].decode("utf-8"))
            except: self._err(400, "JSON Fehler"); return
            img_bytes = parts["file"]
        else:
            try: payload = json.loads(body)
            except: self._err(400, "JSON Fehler"); return
            img_b64 = payload.get("image_b64", "")
            if not img_b64: self._err(400, "Kein Bild"); return
            img_bytes = base64.b64decode(img_b64)

        result  = payload.get("result", {})
        wcs     = result.get("wcs_info", {})
        objects = result.get("objects", [])
        if not wcs:
            self._err(400, "Keine WCS-Info"); return

        try:
            import numpy as np
            from astropy.io import fits as afits
            from PIL import Image
            import io as _io

            pil_img = Image.open(_io.BytesIO(img_bytes))
            if pil_img.mode in ("RGB", "RGBA"):
                arr = np.array(pil_img)
                if arr.ndim == 3:
                    if arr.shape[2] == 4:
                        arr = arr[:,:,:3]
                    arr = np.moveaxis(arr, 2, 0).astype(np.float32)
                else:
                    arr = arr.astype(np.float32)
            elif pil_img.mode == "I;16":
                arr = np.array(pil_img, dtype=np.uint16).astype(np.float32)
            else:
                arr = np.array(pil_img.convert("L"), dtype=np.float32)

            hdu = afits.PrimaryHDU(arr)
            h = hdu.header
            h["COMMENT"] = "Plate Solver Annotated FITS Export"

            # WCS
            h["CTYPE1"] = "RA---TAN"
            h["CTYPE2"] = "DEC--TAN"
            h["CRVAL1"] = wcs.get("crval1", wcs.get("ra_center", 0))
            h["CRVAL2"] = wcs.get("crval2", wcs.get("dec_center", 0))
            h["CRPIX1"] = wcs.get("crpix1", wcs.get("img_w", arr.shape[-1]) / 2)
            h["CRPIX2"] = wcs.get("crpix2", wcs.get("img_h", arr.shape[-2]) / 2)
            if wcs.get("has_full_cd"):
                h["CD1_1"] = wcs["cd11"]
                h["CD1_2"] = wcs["cd12"]
                h["CD2_1"] = wcs["cd21"]
                h["CD2_2"] = wcs["cd22"]
            else:
                import math
                scale = wcs.get("scale_deg_per_px", 1e-4)
                rot   = math.radians(wcs.get("rotation_deg", 0))
                h["CD1_1"] = -scale * math.cos(rot)
                h["CD1_2"] =  scale * math.sin(rot)
                h["CD2_1"] =  scale * math.sin(rot)
                h["CD2_2"] =  scale * math.cos(rot)
            h["EQUINOX"] = 2000.0
            h["RADESYS"] = "ICRS"

            # Objekte als FITS-Tabelle (Binary Table Extension)
            if objects:
                from astropy.table import Table
                tbl_data = {
                    "NAME": [o.get("id","")[:40] for o in objects[:5000]],
                    "RA":   [float(o.get("ra",0)) for o in objects[:5000]],
                    "DEC":  [float(o.get("dec",0)) for o in objects[:5000]],
                    "MAG":  [float(o.get("magnitude") or 99) for o in objects[:5000]],
                    "TYPE": [o.get("type","")[:12] for o in objects[:5000]],
                    "CAT":  [o.get("catalog","")[:16] for o in objects[:5000]],
                }
                tbl = Table(tbl_data)
                tbl_hdu = afits.BinTableHDU(tbl, name="OBJECTS")
                hdulist = afits.HDUList([hdu, tbl_hdu])
            else:
                hdulist = afits.HDUList([hdu])

            buf = _io.BytesIO()
            hdulist.writeto(buf, overwrite=True)
            fits_bytes = buf.getvalue()

            fname = "plate_solver_annotated.fits"
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/fits")
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            self.send_header("Content-Length", str(len(fits_bytes)))
            self.end_headers()
            self.wfile.write(fits_bytes)

        except Exception as e:
            log(f"[FITS] Fehler: {traceback.format_exc()[:500]}")
            self._err(500, f"FITS-Export Fehler: {e}")

    def _photo_z(self, body):
        """Photometrische Rotverschiebung aus SDSS ugriz-Bändern via Farb-Template-Fitting."""
        try: payload = json.loads(body)
        except: self._err(400, "JSON Fehler"); return
        objects = payload.get("objects", [])
        if not objects:
            self._err(400, "Keine Objekte"); return

        import numpy as np

        # SED-Templates: normierte Flüsse in ugriz für typische Galaxientypen
        # Basierend auf Coleman, Wu & Weedman (1980) + Kinney et al. (1996)
        TEMPLATES = {
            "E":   np.array([0.15, 0.55, 1.00, 1.20, 1.25]),
            "Sbc": np.array([0.30, 0.65, 1.00, 1.10, 1.05]),
            "Scd": np.array([0.50, 0.80, 1.00, 0.95, 0.85]),
            "Irr": np.array([0.80, 0.95, 1.00, 0.85, 0.70]),
            "SB":  np.array([1.20, 1.10, 1.00, 0.75, 0.55]),
        }
        # Effektive Wellenlängen der SDSS-Filter [Angstrom]
        LAMBDA_EFF = np.array([3543, 4770, 6231, 7625, 9134], dtype=float)

        # Vorberechnung: Template-Farben (u-g, g-r, r-i, i-z) für jedes z
        z_grid = np.arange(0.001, 1.201, 0.005)
        tmpl_colors = {}
        for tname, tmpl in TEMPLATES.items():
            colors_at_z = []
            for z in z_grid:
                lam_rest = LAMBDA_EFF / (1 + z)
                shifted = np.interp(lam_rest, LAMBDA_EFF, tmpl, left=tmpl[0]*0.5, right=tmpl[-1])
                shifted = np.clip(shifted, 1e-10, None)
                mags_tmpl = -2.5 * np.log10(shifted)
                c = np.array([mags_tmpl[0]-mags_tmpl[1], mags_tmpl[1]-mags_tmpl[2],
                              mags_tmpl[2]-mags_tmpl[3], mags_tmpl[3]-mags_tmpl[4]])
                colors_at_z.append(c)
            tmpl_colors[tname] = np.array(colors_at_z)

        color_err = 0.07  # typischer SDSS-Farbfehler ~0.05-0.10 mag

        results = []
        for obj in objects[:500]:
            mags = []
            for band in ["u","g","r","i","z"]:
                v = obj.get(band)
                if v is None or v == "":
                    mags.append(None)
                else:
                    try:
                        mv = float(v)
                        mags.append(mv if mv < 30 else None)
                    except:
                        mags.append(None)

            obs_colors = []
            color_mask = []
            pairs = [(0,1),(1,2),(2,3),(3,4)]
            for a, b in pairs:
                if mags[a] is not None and mags[b] is not None:
                    obs_colors.append(mags[a] - mags[b])
                    color_mask.append(True)
                else:
                    obs_colors.append(0)
                    color_mask.append(False)
            obs_colors = np.array(obs_colors)
            color_mask = np.array(color_mask)

            if color_mask.sum() < 2:
                results.append({"id": obj.get("id",""), "z_phot": None, "reason": "Zu wenige Farben"})
                continue

            best_z = 0
            best_chi2 = 1e30
            best_tmpl = ""
            for tname, tc_arr in tmpl_colors.items():
                for iz, z in enumerate(z_grid):
                    diff = obs_colors[color_mask] - tc_arr[iz][color_mask]
                    chi2 = np.sum((diff / color_err)**2)
                    if chi2 < best_chi2:
                        best_chi2 = chi2
                        best_z = float(z)
                        best_tmpl = tname

            # Feingitter
            z_fine = np.arange(max(0.001, best_z - 0.025), best_z + 0.026, 0.001)
            for tname, tmpl in TEMPLATES.items():
                for z in z_fine:
                    lam_rest = LAMBDA_EFF / (1 + z)
                    shifted = np.interp(lam_rest, LAMBDA_EFF, tmpl, left=tmpl[0]*0.5, right=tmpl[-1])
                    shifted = np.clip(shifted, 1e-10, None)
                    mags_t = -2.5 * np.log10(shifted)
                    tc = np.array([mags_t[0]-mags_t[1], mags_t[1]-mags_t[2],
                                   mags_t[2]-mags_t[3], mags_t[3]-mags_t[4]])
                    diff = obs_colors[color_mask] - tc[color_mask]
                    chi2 = np.sum((diff / color_err)**2)
                    if chi2 < best_chi2:
                        best_chi2 = chi2
                        best_z = float(z)
                        best_tmpl = tname

            ndof = max(1, int(color_mask.sum()) - 1)
            chi2_red = best_chi2 / ndof
            if chi2_red < 2:
                conf = "hoch"
            elif chi2_red < 8:
                conf = "mittel"
            else:
                conf = "niedrig"

            H0, c_kms = 67.74, 299792.458
            if best_z < 0.1:
                d_mpc = c_kms * best_z / H0
            else:
                from scipy.integrate import quad
                Om, Ol = 0.3, 0.7
                f = lambda zz: 1.0 / np.sqrt(Om*(1+zz)**3 + Ol)
                dc, _ = quad(f, 0, best_z)
                d_mpc = c_kms / H0 * dc

            d_ly = d_mpc * 3.2616e6
            lookback_gyr = 0
            if best_z > 0:
                from scipy.integrate import quad as quad2
                Om, Ol = 0.3, 0.7
                f2 = lambda zz: 1.0 / ((1+zz) * np.sqrt(Om*(1+zz)**3 + Ol))
                tl, _ = quad2(f2, 0, best_z)
                tH_gyr = 3.0857e19 / (H0 * 3.1557e16)
                lookback_gyr = tl * tH_gyr

            results.append({
                "id": obj.get("id",""),
                "z_phot": round(best_z, 4),
                "z_err": round(0.02 + best_z * 0.05, 4),
                "template": best_tmpl,
                "chi2_red": round(chi2_red, 2),
                "confidence": conf,
                "d_mpc": round(d_mpc, 1),
                "d_mly": round(d_ly / 1e6, 1),
                "lookback_gyr": round(lookback_gyr, 2),
            })

        self._json({"status": "ok", "results": results})

    def _gaia_cone(self, body):
        """Gaia DR3 Cone Search via VizieR TAP für HRD/CMD."""
        try: payload = json.loads(body)
        except: self._err(400, "JSON Fehler"); return

        ra   = payload.get("ra")
        dec  = payload.get("dec")
        radius = payload.get("radius", 0.5)
        mag_limit = float(payload.get("mag_limit", 18))

        if ra is None or dec is None:
            self._err(400, "RA/Dec fehlt"); return

        import csv, io as _io
        try:
            s = get_solver()
            # VizieR TAP: Gaia DR3 mit Photometrie, Parallaxe, Teff
            # Gmag + BPmag + RPmag für echten Farbindex
            r_use = min(float(radius), 1.0)
            mag_use = min(float(mag_limit), 17)
            adql = (
                'SELECT TOP 20000 Source, RA_ICRS, DE_ICRS, Gmag, BPmag, RPmag, '
                'Plx, e_Plx, Teff, RV '
                'FROM "I/355/gaiadr3" '
                'WHERE 1=CONTAINS(POINT(\'ICRS\', RA_ICRS, DE_ICRS), '
                'CIRCLE(\'ICRS\', {ra}, {dec}, {r})) '
                'AND Gmag < {mag} '
                'AND Plx IS NOT NULL '
                'AND BPmag IS NOT NULL AND RPmag IS NOT NULL'
            ).format(ra=ra, dec=dec, r=r_use, mag=mag_use)

            log(f"[Gaia] Cone search RA={ra:.3f} Dec={dec:.3f} r={r_use:.3f}° mag<{mag_use}")
            # Gaia TAP direkt (schneller als VizieR für große Cone Searches)
            import urllib.request, urllib.parse
            tap_url = "https://gea.esac.esa.int/tap-server/tap/sync"
            gaia_adql = (
                'SELECT TOP 20000 source_id, ra, dec, phot_g_mean_mag, '
                'phot_bp_mean_mag, phot_rp_mean_mag, parallax, parallax_error, '
                'teff_gspphot, radial_velocity '
                'FROM gaiadr3.gaia_source '
                'WHERE 1=CONTAINS(POINT(\'ICRS\', ra, dec), '
                'CIRCLE(\'ICRS\', {ra}, {dec}, {r})) '
                'AND phot_g_mean_mag < {mag} '
                'AND parallax IS NOT NULL '
                'AND phot_bp_mean_mag IS NOT NULL '
                'AND phot_rp_mean_mag IS NOT NULL'
            ).format(ra=ra, dec=dec, r=r_use, mag=mag_use)
            post_data = urllib.parse.urlencode({
                "REQUEST": "doQuery", "LANG": "ADQL",
                "FORMAT": "csv", "QUERY": gaia_adql
            }).encode("utf-8")
            req = urllib.request.Request(tap_url, data=post_data,
                headers={"User-Agent": "AstroSolver/3.0",
                         "Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=180) as resp:
                raw = resp.read().decode("utf-8")

            stars = []
            reader = csv.DictReader(_io.StringIO(raw))
            for row in reader:
                try:
                    source = (row.get("source_id") or row.get("Source", "")).strip()
                    if not source: continue
                    g_mag = float((row.get("phot_g_mean_mag") or row.get("Gmag", "")).strip())
                    bp_mag = float((row.get("phot_bp_mean_mag") or row.get("BPmag", "")).strip())
                    rp_mag = float((row.get("phot_rp_mean_mag") or row.get("RPmag", "")).strip())
                    plx_s = (row.get("parallax") or row.get("Plx", "")).strip()
                    plx = float(plx_s) if plx_s else None
                    plx_e_s = (row.get("parallax_error") or row.get("e_Plx", "")).strip()
                    plx_e = float(plx_e_s) if plx_e_s else None
                    teff_s = (row.get("teff_gspphot") or row.get("Teff", "")).strip()
                    teff = float(teff_s) if teff_s else None
                    rv_s = (row.get("radial_velocity") or row.get("RV", "")).strip()
                    rv = float(rv_s) if rv_s else None
                    ra_s = float((row.get("ra") or row.get("RA_ICRS", "")).strip())
                    dec_s = float((row.get("dec") or row.get("DE_ICRS", "")).strip())

                    star = {
                        "id": f"Gaia {source}",
                        "ra": round(ra_s, 6),
                        "dec": round(dec_s, 6),
                        "g_mag": round(g_mag, 4),
                        "bp_mag": round(bp_mag, 4),
                        "rp_mag": round(rp_mag, 4),
                        "bp_rp": round(bp_mag - rp_mag, 4),
                    }
                    if plx is not None:
                        star["parallax_mas"] = round(plx, 4)
                        if plx_e: star["parallax_err"] = round(plx_e, 4)
                        if plx > 0.1:
                            d_pc = 1000.0 / plx
                            star["dist_pc"] = round(d_pc, 1)
                            star["M_abs"] = round(g_mag - 5 * __import__('math').log10(d_pc) + 5, 3)
                    if teff: star["teff_k"] = round(teff, 0)
                    if rv: star["rv_kms"] = round(rv, 1)
                    stars.append(star)
                except (ValueError, KeyError):
                    continue

            log(f"[Gaia] {len(stars)} Sterne mit BP/RP+Parallaxe")
            self._json({"status": "ok", "count": len(stars), "stars": stars})

        except Exception as e:
            import traceback
            log(f"[Gaia] Fehler: {traceback.format_exc()[:400]}")
            self._err(500, f"Gaia-Abfrage Fehler: {e}")

    def _bh_masses(self, body):
        """Virial-Schwarzloch-Massen für SDSS-Quasare: Shen et al. 2011 (ApJS 194, 45),
        VizieR J/ApJS/194/45 — Cone Search via TAP. Spaltennamen werden zur
        Laufzeit erkannt (fuzzy), da VizieR-Tabellenlayouts variieren."""
        try: payload = json.loads(body)
        except Exception: self._err(400, "JSON Fehler"); return

        ra = payload.get("ra"); dec = payload.get("dec")
        radius = min(float(payload.get("radius", 0.5)), 2.0)
        if ra is None or dec is None:
            self._err(400, "RA/Dec fehlt"); return

        import csv as _csv, io as _io, urllib.request, urllib.parse
        tap_url = "https://tapvizier.cds.unistra.fr/TAPVizieR/tap/sync"
        # Kandidaten: (Tabelle, RA-Spalte, Dec-Spalte) — erste funktionierende gewinnt.
        # Shen et al. 2011 (ApJS 194, 45): 105.783 SDSS-DR7-Quasare mit virialen
        # BH-Massen (logBH), L_bol und Eddington-Verhältnis. Wu & Shen 2022 (DR16Q)
        # ist (Stand 07/2026) nicht als VizieR-TAP-Tabelle verfügbar.
        candidates = [
            ('"J/ApJS/194/45/catalog"', "RAJ2000", "DEJ2000"),
            ('"J/ApJS/194/45/catalog"', "RAdeg", "DEdeg"),
        ]
        rows, used = [], None
        for tbl, cra, cde in candidates:
            adql = ("SELECT TOP 5000 * FROM {t} WHERE 1=CONTAINS("
                    "POINT('ICRS', {cra}, {cde}), CIRCLE('ICRS', {ra}, {dec}, {r}))"
                    ).format(t=tbl, cra=cra, cde=cde, ra=ra, dec=dec, r=radius)
            post = urllib.parse.urlencode({
                "REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "csv", "QUERY": adql
            }).encode("utf-8")
            try:
                req = urllib.request.Request(tap_url, data=post,
                    headers={"User-Agent": "AstroSolver/3.0",
                             "Content-Type": "application/x-www-form-urlencoded"})
                with urllib.request.urlopen(req, timeout=120) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                if raw.lstrip().startswith("<"):  # VOTable-Fehlermeldung statt CSV
                    continue
                rdr = _csv.DictReader(_io.StringIO(raw))
                got = list(rdr)
                used = (tbl, cra, cde)
                rows = got
                break  # Tabelle+Spalten funktionieren (auch bei 0 Treffern im Feld)
            except Exception as e:
                log(f"[BH] {tbl}/{cra}: {str(e)[:120]}")
                continue

        if used is None:
            self._err(502, "Shen et al. 2011 Katalog via VizieR nicht erreichbar"); return

        def _find(fields, key, exclude=("e_", "f_", "l_", "n_")):
            key = key.lower()
            for f in fields:
                fl = (f or "").lower()
                if key in fl and not fl.startswith(exclude): return f
            return None

        out = []
        if rows:
            fields = list(rows[0].keys())
            c_ra   = next((f for f in fields if f in (used[1], "RAJ2000", "RAdeg", "RA_ICRS")), None)
            c_dec  = next((f for f in fields if f in (used[2], "DEJ2000", "DEdeg", "DE_ICRS")), None)
            c_z    = next((f for f in fields if (f or "").lower() in ("z", "zsys", "zdr16q", "zbest")), None)
            c_mbh  = _find(fields, "mbh") or next((f for f in fields if (f or "").lower() == "logbh"), None)
            c_embh = next((f for f in fields if (f or "").lower() in ("e_logmbh", "e_mbh", "e_logbh")), None)
            c_lbol = _find(fields, "lbol")
            c_edd  = _find(fields, "edd")
            for row in rows:
                try:
                    o = {"ra": round(float(row[c_ra]), 6), "dec": round(float(row[c_dec]), 6)}
                    for col, key in ((c_z, "z"), (c_mbh, "log_mbh"), (c_embh, "e_log_mbh"),
                                     (c_lbol, "log_lbol"), (c_edd, "log_edd")):
                        v = (row.get(col) or "").strip() if col else ""
                        if v:
                            try: o[key] = float(v)
                            except ValueError: pass
                    if "log_mbh" in o or "log_lbol" in o:
                        out.append(o)
                except (ValueError, KeyError, TypeError):
                    continue

        log(f"[BH] Shen+2011: {len(out)} Quasare mit M_BH/L_bol (Tabelle {used[0]})")
        self._json({"status": "ok", "count": len(out), "table": used[0], "objects": out})

    def _file(self, path, ctype):
        if not path.exists(): self.send_response(404); self.end_headers(); return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self._cors(); self.end_headers(); self.wfile.write(data)

    def _json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors(); self.end_headers(); self.wfile.write(body)

    def _err(self, code, msg):
        body = json.dumps({"error": msg}, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self._cors(); self.end_headers(); self.wfile.write(body)


if __name__ == "__main__":
    missing = []
    for pkg, name in [("numpy","numpy"),("PIL","pillow"),("astropy","astropy"),("scipy","scipy")]:
        try: __import__(pkg)
        except: missing.append(name)

    s = get_solver()
    astap = s.find_astap()

    # Messier-Daten in bestehender DB auf aktuellen Stand bringen (110 Objekte)
    if (BASE/"catalog.db").exists():
        try:
            n = s.refresh_messier(BASE/"catalog.db")
            print(f"  Messier: {n} Objekte aktualisiert")
        except Exception as e:
            print(f"  Messier-Update uebersprungen: {e}")

    print("\n" + "="*50)
    print("  Astro Plate Solver v1.0-rc1")
    print("="*50)
    print(f"  URL  : http://localhost:{PORT}")
    print(f"  DB   : {BASE/'catalog.db'}")
    print(f"  ASTAP: {astap or 'nicht gefunden'}")
    if missing:
        print(f"  Fehlende Pakete: pip install {' '.join(missing)}")
    else:
        print("  Alle Pakete vorhanden")
    print("  Beenden: Strg+C")
    print("="*50 + "\n")

    server = http.server.HTTPServer(("localhost", PORT), Handler)
    try: server.serve_forever()
    except KeyboardInterrupt: print("\n  Gestoppt.")
