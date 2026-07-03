"""
Fotometrie-Modul fuer Astro Plate Solver
- Aperturphotometrie (Kreis-Apertur + Ringhintergrund)
- Automatische Zeropoint-Kalibrierung gegen Katalog (Tycho-2 / Gaia DR3)
- Liefert kalibrierte Magnituden, FWHM, SNR, Residuen
"""
import math
import numpy as np
from pathlib import Path


def _srgb_to_linear(arr):
    """sRGB-Gamma entfernen: 8-bit sRGB -> lineare Intensitaet [0..1].
    Ohne das ist Photometrie auf JPEG/PNG systematisch falsch (Gamma ~2.2)."""
    x = arr.astype(np.float64) / 255.0
    lin = np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)
    return lin


def _online_gaia_cone(ra, dec, radius_deg, mag_limit, max_stars=3000, timeout=30):
    """Feldspezifische VizieR-Kegelsuche fuer Gaia DR3.

    Liefert immer die richtigen Sterne fuer genau dieses Bildfeld,
    unabhaengig vom lokalen Kataloginhalt.
    Wird aufgerufen wenn der lokale Katalog zu wenige Referenzsterne hat.
    """
    import io as _io
    import csv as _csv
    import urllib.request as _ureq
    import urllib.parse as _uparse

    params = {
        "-source": "I/355/gaiadr3",
        "-c":      f"{ra:.6f} {dec:+.6f}",
        "-c.rd":   f"{min(radius_deg, 3.0):.4f}",   # VizieR-Limit: max ~3 deg
        "-out":    "Source,RA_ICRS,DE_ICRS,Gmag",
        "Gmag":    f"..{mag_limit}",
        "-out.max": str(max_stars),
        "-oc.form": "dec",
    }
    url = "https://vizier.cds.unistra.fr/viz-bin/asu-csv?" + _uparse.urlencode(params)
    req = _ureq.Request(url, headers={"User-Agent": "AstroPhotSolver/1.0", "Accept": "*/*"})
    with _ureq.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", errors="replace")

    objects = []
    for row in _csv.DictReader(_io.StringIO(raw)):
        try:
            ra_v   = float(str(row.get("RA_ICRS", "")).strip())
            dec_v  = float(str(row.get("DE_ICRS", "")).strip())
            mag    = float(str(row.get("Gmag",    "")).strip())
            gid    = "Gaia " + str(row.get("Source", "")).strip()
            objects.append({
                "id": gid, "catalog": "Gaia DR3",
                "ra": ra_v, "dec": dec_v,
                "magnitude": mag, "type": "star", "name": gid,
            })
        except Exception:
            continue
    return objects


def _saturation_level(path, img):
    """Bestimmt die Saettigungsgrenze des Bildes.

    FITS: SATURATE-Header bevorzugt; sonst aus BITPIX/BZERO abgeleitet.
    PNG/JPEG/TIFF: load_raw skaliert immer auf 0..65535, daher fester Grenzwert.
    Fallback (float-FITS oder fehlender Header): robuste Percentil-Schaetzung,
    damit einzelne Hot Pixels / Cosmic Rays sat_level nicht hochziehen.
    """
    p = str(path).lower()
    if p.endswith((".fit", ".fits")):
        try:
            from astropy.io import fits
            with fits.open(str(path), memmap=False) as hdul:
                img_hdu = next(
                    (h for h in hdul if h.data is not None and len(np.shape(h.data)) >= 2),
                    None,
                )
                if img_hdu is not None:
                    hdr = img_hdu.header
                    # Standard-FITS-Keyword (von INDI, NINA, Maxim, SGP etc. geschrieben)
                    if "SATURATE" in hdr:
                        return float(hdr["SATURATE"])
                    bitpix = int(hdr.get("BITPIX", 0))
                    if bitpix == 8:
                        return 255.0
                    if bitpix == 16:
                        # BZERO=32768 -> astropy liefert uint16 (0..65535)
                        # BZERO=0     -> int16  (-32768..32767)
                        bzero = float(hdr.get("BZERO", 0.0))
                        return 65535.0 if bzero >= 32768.0 else 32767.0
                    if bitpix == 32:
                        return float(2**31 - 1)
        except Exception:
            pass
        # Float-FITS oder kein passender Header:
        # Pixel am exakten Maximum sind oft Hot Pixels oder Cosmic Rays.
        # Wir ignorieren sie und schaetzen die echte Grenze aus dem oberen Rand.
        img_max = float(img.max())
        if img_max <= 0:
            return max(img_max, 1.0)
        sub = img[img < img_max * 0.9999]
        if sub.size > 100:
            return float(np.percentile(sub, 99.9)) * 1.05
        return img_max * 0.98

    # PNG 8-bit, JPEG, sRGB-Graustufen:
    #   load_raw wendet sRGB->linear an und skaliert auf *65535.
    #   Ein gesaettigtes (weisses) Pixel landet bei 65535.
    # PNG 16-bit (mode I;16):
    #   load_raw gibt den Rohwert (0..65535) unveraendert zurueck.
    # -> In allen Faellen ist 65535 der theoretische Maximalwert.
    return 65535.0 * 0.95


def load_raw(path):
    """Laedt Bild als linearisierte Flux-Karte fuer Photometrie.
    - FITS: unveraendert (bereits linear, ADU)
    - JPEG/PNG: sRGB->linear Entgamma-Korrektur, dann skaliert
    """
    p = str(path).lower()
    is_fits = p.endswith((".fit", ".fits"))
    if is_fits:
        from astropy.io import fits
        hdul = fits.open(str(path), memmap=False)
        img_hdu = next((h for h in hdul if h.data is not None and len(np.shape(h.data)) >= 2), None)
        if img_hdu is None:
            hdul.close()
            raise ValueError("Kein Bilddaten-HDU gefunden")
        data = np.array(img_hdu.data, dtype=np.float64)
        if data.ndim == 3:
            data = data.mean(axis=0)
        elif data.ndim > 3:
            data = data[0, 0]
        hdul.close()
        return data
    # 8/16-bit Foto: RGB-Luminanz -> linear entgamma-t
    from PIL import Image
    im = Image.open(str(path))
    if im.mode in ("RGB", "RGBA"):
        rgb = np.array(im.convert("RGB"), dtype=np.float64)
        # Rec.709 Luminanz in linearem Raum:
        lin_r = _srgb_to_linear(rgb[..., 0])
        lin_g = _srgb_to_linear(rgb[..., 1])
        lin_b = _srgb_to_linear(rgb[..., 2])
        lin = 0.2126 * lin_r + 0.7152 * lin_g + 0.0722 * lin_b
        # In pseudo-ADU skalieren (Photometrie-Differenzen sind additiv im log)
        return lin * 65535.0
    if im.mode == "I" or im.mode == "I;16":
        return np.array(im, dtype=np.float64)
    # L (8-bit greyscale): annimm sRGB-Gamma
    arr = np.array(im.convert("L"), dtype=np.float64)
    return _srgb_to_linear(arr) * 65535.0


def refine_center(img, x, y, search=8):
    """Suche Pixelmaximum in kleinem Fenster um (x,y)."""
    h, w = img.shape
    ix = int(round(x))
    iy = int(round(y))
    x0, x1 = max(0, ix - search), min(w, ix + search + 1)
    y0, y1 = max(0, iy - search), min(h, iy + search + 1)
    if x1 <= x0 or y1 <= y0:
        return float(x), float(y)
    patch = img[y0:y1, x0:x1]
    iy2, ix2 = np.unravel_index(int(np.argmax(patch)), patch.shape)
    return float(x0 + ix2), float(y0 + iy2)


def aperture_phot(img, x, y, r_ap, r_in, r_out):
    """Kreisfoermige Aperturphotometrie mit Ringhintergrund."""
    h, w = img.shape
    ix = int(round(x))
    iy = int(round(y))
    r = int(math.ceil(r_out)) + 1
    x0, x1 = max(0, ix - r), min(w, ix + r + 1)
    y0, y1 = max(0, iy - r), min(h, iy + r + 1)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    yy, xx = np.mgrid[y0:y1, x0:x1]
    dx = xx - x
    dy = yy - y
    rr2 = dx * dx + dy * dy
    patch = img[y0:y1, x0:x1]
    ap_mask = rr2 <= r_ap * r_ap
    an_mask = (rr2 >= r_in * r_in) & (rr2 <= r_out * r_out)
    ap_n = float(np.sum(ap_mask))
    if ap_n < 4 or np.sum(an_mask) < 8:
        return None
    an_vals = patch[an_mask]
    # Sigma-Clipping des Hintergrunds
    med = float(np.median(an_vals))
    mad = float(1.4826 * np.median(np.abs(an_vals - med)) + 1e-9)
    keep = np.abs(an_vals - med) < 3.0 * mad
    if np.sum(keep) > 8:
        an_vals = an_vals[keep]
    bg = float(np.median(an_vals))
    bg_std = float(np.std(an_vals))
    ap_sum = float(np.sum(patch[ap_mask]))
    peak = float(patch[ap_mask].max()) if ap_mask.any() else 0.0
    flux = ap_sum - bg * ap_n
    # Flussgewichteter Zentroid
    w_pix = patch[ap_mask] - bg
    w_pix = np.clip(w_pix, 0, None)
    tot = float(w_pix.sum())
    if tot <= 0:
        return None
    xa = xx[ap_mask].astype(float)
    ya = yy[ap_mask].astype(float)
    cx = float((xa * w_pix).sum() / tot)
    cy = float((ya * w_pix).sum() / tot)
    vx = float(((xa - cx) ** 2 * w_pix).sum() / tot)
    vy = float(((ya - cy) ** 2 * w_pix).sum() / tot)
    sigma = math.sqrt(max((vx + vy) / 2.0, 0.0))
    fwhm = 2.3548 * sigma
    # Poisson+Hintergrundrauschen
    noise = math.sqrt(max(flux, 0.0) + ap_n * bg_std * bg_std + (ap_n * ap_n * bg_std * bg_std) / max(np.sum(an_mask), 1))
    snr = flux / (noise + 1e-9)
    return {
        "flux": flux, "bg": bg, "bg_std": bg_std,
        "x": cx, "y": cy, "fwhm": fwhm, "snr": snr,
        "ap_n": ap_n, "peak": peak,
    }


def _sigma_clip(vals, sigma=3.0, iters=5):
    v = np.asarray(vals, dtype=float)
    for _ in range(iters):
        if len(v) < 3:
            break
        m = np.median(v)
        s = 1.4826 * np.median(np.abs(v - m))
        if s == 0:
            break
        mask = np.abs(v - m) < sigma * s
        if mask.sum() == len(v) or mask.sum() < 3:
            break
        v = v[mask]
    return v


def run_photometry(img_path, wcs_info, conn, solver_mod,
                   r_ap=5.0, r_in=8.0, r_out=12.0,
                   mag_limit=16.0, ref_catalogs=None,
                   ref_objects=None,
                   progress_cb=None):
    """
    Fuehrt vollstaendige Aperturphotometrie + Kalibrierung durch.

    ref_objects: optionale Liste aus lastResult.objects (mit x,y,ra,dec,magnitude,catalog,id).
                 Wenn gegeben, werden diese direkt als Referenzen genutzt
                 (kein zweiter DB-Query).
    """
    def prog(m):
        if progress_cb:
            progress_cb(m)

    if ref_catalogs is None:
        ref_catalogs = ["Tycho-2", "Gaia DR3"]

    img = load_raw(img_path)
    h, w = img.shape
    prog(f"Bild (raw) geladen: {w}x{h}, Flux {img.min():.0f}..{img.max():.0f}")

    # WCS aufbauen (bevorzugt volle CD-Matrix)
    if wcs_info.get("has_full_cd"):
        wcs = solver_mod.TanWCS(
            ra0=wcs_info["ra_center"], dec0=wcs_info["dec_center"],
            scale=wcs_info["scale_deg_per_px"], rot=wcs_info.get("rotation_deg", 0.),
            cx=w / 2, cy=h / 2,
            cd11=wcs_info["cd11"], cd12=wcs_info["cd12"],
            cd21=wcs_info["cd21"], cd22=wcs_info["cd22"],
            crval1=wcs_info["crval1"], crval2=wcs_info["crval2"],
            crpix1=wcs_info["crpix1"], crpix2=wcs_info["crpix2"],
        )
    else:
        wcs = solver_mod.TanWCS(wcs_info["ra_center"], wcs_info["dec_center"],
                                wcs_info["scale_deg_per_px"], wcs_info.get("rotation_deg", 0.),
                                w / 2, h / 2)

    scale = wcs_info["scale_deg_per_px"]

    # Referenzen: entweder aus Solve-Ergebnis (bevorzugt, viel reicher)
    # oder neu aus lokalem Katalog
    ref_list = []
    if ref_objects:
        # ref_objects kommt vom Frontend (lastResult.objects gefiltert)
        prefer_cats = set(c.lower() for c in (ref_catalogs or []))
        for o in ref_objects:
            mag = o.get("magnitude")
            if mag is None:
                continue
            try:
                mag = float(mag)
            except (TypeError, ValueError):
                continue
            if mag > mag_limit:
                continue
            cat_name = (o.get("catalog") or "").lower()
            # Nur Sterne mit Punkt-Morphologie: Tycho, Gaia, kein galaxy/nebula
            otype = (o.get("type") or "").lower()
            if otype not in ("star",):
                continue
            if prefer_cats and cat_name not in prefer_cats:
                continue
            ref_list.append(o)
        prog(f"{len(ref_list)} Referenzsterne aus Solve-Ergebnis uebernommen "
             f"(Kataloge: {'+'.join(ref_catalogs) if ref_catalogs else 'alle'}, mag<={mag_limit})")
    else:
        radius = math.sqrt((scale * w) ** 2 + (scale * h) ** 2) / 2 * 1.05
        prog(f"Referenzsterne suchen: {'+'.join(ref_catalogs)}, r={radius:.2f} deg, mag<={mag_limit}")
        ref_list = solver_mod.query_region(conn, wcs_info["ra_center"], wcs_info["dec_center"],
                                           radius, mag_limit, ref_catalogs)
        prog(f"{len(ref_list)} Referenzsterne im lokalen Katalog")

    # Online-Fallback: wenn lokaler Katalog / Solve-Ergebnis zu wenige Sterne
    # liefert, direkte VizieR-Kegelsuche fuer genau dieses Bildfeld.
    # Vorteil: funktioniert immer, unabhaengig vom lokalen DB-Inhalt.
    # Ergebnisse werden lokal gecacht (INSERT OR IGNORE) fuer kuenftige Abfragen.
    _MIN_REFS = 10
    if len(ref_list) < _MIN_REFS and "Gaia DR3" in (ref_catalogs or []):
        prog(f"Zu wenige lokale Referenzsterne ({len(ref_list)}) – "
             f"starte Online-Gaia-Query fuer dieses Feld...")
        try:
            _radius = math.sqrt((scale * w) ** 2 + (scale * h) ** 2) / 2 * 1.1
            online = _online_gaia_cone(
                wcs_info["ra_center"], wcs_info["dec_center"],
                _radius, mag_limit, max_stars=3000, timeout=30,
            )
            if online:
                prog(f"Online Gaia: {len(online)} Sterne fuer dieses Feld")
                # Lokal cachen damit naechste Abfrage sofort klappt
                try:
                    conn.executemany(
                        "INSERT OR IGNORE INTO objects "
                        "(id,catalog,ra,dec,magnitude,type,name,description) "
                        "VALUES (:id,:catalog,:ra,:dec,:magnitude,:type,:name,:description)",
                        [{**o, "description": f"Gaia G={o['magnitude']:.2f}"}
                         for o in online],
                    )
                    conn.commit()
                    prog(f"  {len(online)} Sterne lokal gecacht")
                except Exception as _ce:
                    prog(f"  Lokaler Cache fehlgeschlagen (nicht kritisch): {_ce}")
                ref_list = online
            else:
                prog("Online-Gaia: keine Sterne zurueckgegeben")
        except Exception as _oe:
            prog(f"Online-Gaia fehlgeschlagen: {_oe}")

    # Saettigungsschwelle: FITS-Header (SATURATE/BITPIX) oder PNG-Festwert.
    # img.max() allein waere anfaellig fuer einzelne Hot Pixels / Cosmic Rays,
    # die sat_level kuenstlich hochziehen und echte Sterne nicht erkennen lassen.
    sat_level = _saturation_level(img_path, img)
    prog(f"Saettigungsgrenze: {sat_level:.0f} ADU")
    margin = max(r_out + 2, 14)

    def _measure_refs(r_ap_, r_in_, r_out_):
        margin_ = max(r_out_ + 2, 14)
        out, n_sat = [], 0
        for obj in ref_list:
            try:
                cat_mag_val = float(obj.get("magnitude"))
            except (TypeError, ValueError):
                continue
            if obj.get("x") is not None and obj.get("y") is not None:
                px, py = float(obj["x"]), float(obj["y"])
            else:
                px, py = wcs.world_to_pixel(float(obj["ra"]), float(obj["dec"]))
            if px < margin_ or py < margin_ or px > w - margin_ or py > h - margin_:
                continue
            rx, ry = refine_center(img, px, py, search=8)
            ph = aperture_phot(img, rx, ry, r_ap_, r_in_, r_out_)
            if ph is None or ph["flux"] <= 0:
                continue
            if ph["peak"] >= sat_level:
                n_sat += 1
                continue
            inst = -2.5 * math.log10(ph["flux"])
            out.append({
                "id": obj.get("id", "?"),
                "catalog": obj.get("catalog", "?"),
                "ra": float(obj.get("ra", 0)),
                "dec": float(obj.get("dec", 0)),
                "cat_mag": cat_mag_val,
                "inst_mag": inst,
                "flux": ph["flux"], "bg": ph["bg"],
                "snr": ph["snr"], "fwhm": ph["fwhm"],
                "x": ph["x"], "y": ph["y"],
            })
        return out, n_sat

    # Durchgang 1: mit vom Nutzer vorgegebener Apertur, um FWHM zu schaetzen
    matched, n_sat = _measure_refs(r_ap, r_in, r_out)
    prog(f"{len(matched)} Sterne vermessen (1. Durchgang, {n_sat} gesaettigt uebersprungen)")

    # Adaptive Apertur: wenn FWHM deutlich groesser als aktuelle Apertur ist,
    # nachmessen mit r_ap ~= 1.5*FWHM, r_in ~= r_ap+3, r_out ~= r_ap+7.
    # Dadurch werden PSF-Fluegel konsistent erfasst -> kleinerer ZP-Scatter.
    if len(matched) >= 10:
        fwhms = sorted([m["fwhm"] for m in matched if m["fwhm"] > 0])
        fwhm_med = fwhms[len(fwhms)//2] if fwhms else 0.0
        target_ap = max(r_ap, 1.5 * fwhm_med)
        if fwhm_med > 0 and target_ap > r_ap * 1.15:
            r_ap_eff = float(round(target_ap, 1))
            r_in_eff  = r_ap_eff + 3.0
            r_out_eff = r_ap_eff + 7.0
            prog(f"Adaptive Apertur: FWHM_med={fwhm_med:.2f}px -> "
                 f"r_ap={r_ap_eff:.1f}, r_in={r_in_eff:.1f}, r_out={r_out_eff:.1f}")
            matched2, n_sat2 = _measure_refs(r_ap_eff, r_in_eff, r_out_eff)
            if len(matched2) >= max(10, int(0.5 * len(matched))):
                matched = matched2
                n_sat = n_sat2  # matched2 ersetzt matched komplett, kein Addieren
                r_ap, r_in, r_out = r_ap_eff, r_in_eff, r_out_eff
                margin = max(r_out + 2, 14)
                prog(f"  uebernommen: {len(matched)} Sterne mit adaptiver Apertur")
    prog(f"{len(matched)} Sterne final aperturvermessen")

    # --- Pre-Rejection: gesaettigte / nichtlineare Sterne vor ZP-Fit markieren --------
    # Gesaettigte Sterne und Sterne im nichtlinearen Sensorbereich (typisch fuer JPEG-
    # Aufnahmen, wo JPEG-Ringing den Peak leicht unter sat_level drueckt) erscheinen
    # systematisch ZU SCHWACH gegenueber dem Katalog -> stark positives Residual.
    # Vorgehen: grobe ZP-Schaetzung per Katalog (Median, kein Fit), dann alle Sterne
    # mit Residual > max(2.5*MAD, 0.4 mag) als "excluded_from_zp" markieren.
    # Diese Sterne bleiben in der Ausgabeliste, werden aber NICHT fuer den ZP-Fit genutzt.
    def _rough_zp_est(grp):
        vals = [m["cat_mag"] - m["inst_mag"] for m in grp if m["snr"] > 10]
        if len(vals) < 3:
            vals = [m["cat_mag"] - m["inst_mag"] for m in grp]
        return float(np.median(vals)) if vals else None

    for m in matched:
        m["excluded_from_zp"] = False

    by_cat_rough = {}
    for m in matched:
        by_cat_rough.setdefault(m.get("catalog") or "?", []).append(m)

    n_prerej = 0
    for _cat_name, _grp in by_cat_rough.items():
        if len(_grp) < 5:
            continue
        zp_r = _rough_zp_est(_grp)
        if zp_r is None:
            continue
        # _r = cat_mag - inst_mag - zp_rough = -(calib_mag - cat_mag) = -(output-residual)
        # Gesaettigte Sterne haben output-residual > 0 (zu schwach gemessen),
        # also _r < 0. Wir schliessen Sterne mit stark NEGATIVEM _r aus.
        _resids = np.array([m["cat_mag"] - m["inst_mag"] - zp_r for m in _grp])
        # Nur die "guten" Sterne (|_r| < 1 mag) fuer die MAD-Schaetzung nutzen,
        # damit viele Ausreisser die Schwelle nicht kuenstlich aufblaehen.
        # MAD nur aus dem "Kern" der Verteilung (|_r| < 0.5 mag),
        # damit viele Ausreisser den Schwellwert nicht kuenstlich aufblaehen.
        _core = _resids[np.abs(_resids) < 0.5]
        if len(_core) < 5:
            _core = _resids[np.abs(_resids) < 1.0]
        if len(_core) < 5:
            _core = _resids
        _mad = float(1.4826 * np.median(np.abs(_core - np.median(_core)))) + 1e-6
        # Schwellwert: MAD-basiert, aber maximal 0.5 mag.
        # Sterne mit |Δ| > 0.5 mag positiv sind immer gesaettigt/nichtlinear –
        # kein realer Messfehler sollte konsistent so gross sein.
        _thresh = min(max(2.5 * _mad, 0.30), 0.50)
        for _m, _r in zip(_grp, _resids):
            if _r < -_thresh:   # stark negatives _r = star zu schwach gemessen = gesaettigt
                _m["excluded_from_zp"] = True
                n_prerej += 1

    if n_prerej > 0:
        prog(f"Pre-Rejection: {n_prerej} Sterne vor ZP-Fit markiert "
             f"(wahrsch. gesaettigt/nichtlinear, starkes positives Residual)")
    # ---------------------------------------------------------------------------------

    if len(matched) < 3:
        hints = []
        if not ref_objects:
            hints.append("Lokaler Katalog ist ggf. zu duenn - Frontend sollte ref_objects aus Solve-Ergebnis uebergeben.")
        hints.append("mag-Limit anheben (z.B. 17 oder 18).")
        hints.append("Auch Gaia-Sterne aktivieren wenn nur Tycho an war.")
        hints.append("Apertur ggf. groesser waehlen (r_ap=6-8).")
        return {
            "status": "insufficient",
            "message": f"Nur {len(matched)} verwendbare Referenzsterne (benoetigt >=3). "
                       f"Von {len(ref_list)} Kandidaten im Katalog. "
                       + " ".join(hints),
            "n_matched": len(matched),
            "n_candidates": len(ref_list),
            "matched": [],
            "all_sources": [],
        }

    # Pro Katalog (= pro Photometrieband) getrennt kalibrieren.
    # Mischen von Tycho-2 Vt und Gaia G fuehrt zu systematischen Offsets
    # (Farbterm ~0.1-0.3 mag), deshalb einzeln rechnen und den besten
    # Band als primaeren Zeropoint verwenden.
    # Achtung: nur nicht-vorselektierte Sterne fuer den ZP-Fit verwenden!
    by_cat = {}
    for m in matched:
        if not m.get("excluded_from_zp"):
            by_cat.setdefault(m.get("catalog") or "?", []).append(m)

    zp_per_cat = {}
    for cat_name, group in by_cat.items():
        zp_vals = np.array([g["cat_mag"] - g["inst_mag"] for g in group], dtype=float)
        snrs = np.array([g["snr"] for g in group])
        good = (snrs > 10) & np.isfinite(zp_vals)
        arr = zp_vals[good] if np.sum(good) >= 3 else zp_vals[np.isfinite(zp_vals)]
        if len(arr) < 3:
            continue
        z = _sigma_clip(arr)
        if len(z) < 3:
            continue
        zp_cat = float(np.median(z))
        zp_sig = float(1.4826 * np.median(np.abs(z - np.median(z))))
        zp_per_cat[cat_name] = {
            "zp": zp_cat, "sigma": zp_sig,
            "n_used": int(len(z)), "n_total": int(len(group)),
        }
        prog(f"  ZP[{cat_name}] = {zp_cat:.3f} +- {zp_sig:.3f} (N={len(z)}/{len(group)})")

    if not zp_per_cat:
        return {
            "status": "insufficient",
            "message": "Kein Katalog hat >=3 verwendbare Referenzsterne fuer einen Zeropoint.",
            "n_matched": len(matched), "n_candidates": len(ref_list),
            "matched": [], "all_sources": [],
        }

    # Wahl des primaeren Bandes: bevorzugt Gaia G (am homogensten),
    # sonst kleinster sigma.
    priority = ["Gaia DR3", "Tycho-2", "Hipparcos"]
    primary_cat = None
    for p in priority:
        if p in zp_per_cat:
            primary_cat = p; break
    if primary_cat is None:
        primary_cat = min(zp_per_cat, key=lambda k: zp_per_cat[k]["sigma"])
    zp = zp_per_cat[primary_cat]["zp"]
    zp_std = zp_per_cat[primary_cat]["sigma"]
    n_used = zp_per_cat[primary_cat]["n_used"]
    prog(f"Primaerer Zeropoint: {primary_cat} -> {zp:.3f} +- {zp_std:.3f}")

    # Residuen jeweils gegen den Zeropoint des eigenen Katalogs.
    # RMS / Statistik nur aus nicht-vorselektierten Sternen berechnen,
    # damit gesaettigte Sterne die Qualitaetsangabe nicht verzerren.
    residuals_primary = []
    for m in matched:
        cat = m.get("catalog") or "?"
        zp_m = zp_per_cat.get(cat, {}).get("zp", zp)
        m["calib_mag"] = m["inst_mag"] + zp_m
        m["residual"]  = m["calib_mag"] - m["cat_mag"]
        if cat == primary_cat and not m.get("excluded_from_zp"):
            residuals_primary.append(m["residual"])
    if residuals_primary:
        rp = np.array(residuals_primary, dtype=float)
        rms = float(math.sqrt(np.mean(rp ** 2)))
        med_res = float(np.median(rp))
        mad_res = float(1.4826 * np.median(np.abs(rp - np.median(rp))))
    else:
        rms = float("nan"); med_res = float("nan"); mad_res = float("nan")
    mean_fwhm = float(np.median([m["fwhm"] for m in matched]))

    # Qualitaetsklasse auf Basis von zp-sigma und systematischem Offset
    _s = zp_per_cat[primary_cat]["sigma"]
    if   _s < 0.08 and abs(med_res) < 0.05: quality = "exzellent"
    elif _s < 0.15 and abs(med_res) < 0.10: quality = "gut"
    elif _s < 0.30: quality = "brauchbar"
    else: quality = "schwach"
    prog(f"Qualitaet: {quality} (sigma_ZP={_s:.3f}, median-Residual={med_res:.3f})")

    # Alle erkannten Quellen messen (fuer Komplett-Photometrie-Katalog)
    # Grobdetektion auf Logarithmus-skaliertem Bild fuer dynamischen Bereich
    log_img = np.log1p(np.clip(img - np.percentile(img, 25), 0, None))
    if log_img.max() > 0:
        log_img /= log_img.max()
    try:
        stars_det = solver_mod.extract_stars(log_img.astype(np.float32), max_stars=800)
    except Exception:
        stars_det = []
    prog(f"Gesamtkatalog: {len(stars_det)} Quellen detektiert - messe alle...")

    all_sources = []
    n_sat_allsrc = 0
    for s in stars_det:
        sx, sy = float(s["x"]), float(s["y"])
        if sx < margin or sy < margin or sx > w - margin or sy > h - margin:
            continue
        ph = aperture_phot(img, sx, sy, r_ap, r_in, r_out)
        if ph is None or ph["flux"] <= 0:
            continue
        is_sat = ph["peak"] >= sat_level
        if is_sat:
            n_sat_allsrc += 1
        inst = -2.5 * math.log10(ph["flux"])
        all_sources.append({
            "x": round(ph["x"], 2), "y": round(ph["y"], 2),
            "flux": round(ph["flux"], 2),
            "bg": round(ph["bg"], 2),
            "snr": round(ph["snr"], 2),
            "fwhm": round(ph["fwhm"], 2),
            "inst_mag": round(inst, 3),
            "calib_mag": round(inst + zp, 3),
            "saturated": is_sat,  # Wichtig fuer SDSS-Vergleich: gesaettigte
                                  # Quellen haben eine geclippte, zu schwache
                                  # Helligkeit und verzerren jeden Soll/Ist-
                                  # Vergleich. Frontend sollte sie ausschliessen
                                  # koennen statt sie unkommentiert mitzuzaehlen.
        })
    if n_sat_allsrc:
        prog(f"  Hinweis: {n_sat_allsrc} der gemessenen Quellen sind gesaettigt "
             f"(Peak >= {sat_level:.0f} ADU) - deren Magnitude ist zu schwach/falsch")
    # Nach Helligkeit sortieren
    all_sources.sort(key=lambda s: s["calib_mag"])
    prog(f"{len(all_sources)} Quellen kalibriert (hellster: {all_sources[0]['calib_mag']:.2f} mag)" if all_sources else "Keine Quellen")

    out_matched = []
    for m in matched:
        out_matched.append({
            "id": m["id"], "catalog": m["catalog"],
            "ra": round(m["ra"], 5), "dec": round(m["dec"], 5),
            "x": round(m["x"], 2), "y": round(m["y"], 2),
            "cat_mag": round(m["cat_mag"], 3),
            "inst_mag": round(m["inst_mag"], 3),
            "calib_mag": round(m["calib_mag"], 3),
            "residual": round(m["residual"], 3),
            "flux": round(m["flux"], 2),
            "snr": round(m["snr"], 2),
            "fwhm": round(m["fwhm"], 2),
            "excluded_from_zp": m.get("excluded_from_zp", False),
        })
    # Nach cat_mag sortieren
    out_matched.sort(key=lambda m: m["cat_mag"])

    return {
        "status": "ok",
        "zeropoint": round(zp, 4),
        "zp_std": round(zp_std, 4),
        "primary_band": primary_cat,
        "zp_per_catalog": {k: {"zp": round(v["zp"], 4),
                                "sigma": round(v["sigma"], 4),
                                "n_used": v["n_used"],
                                "n_total": v["n_total"]}
                           for k, v in zp_per_cat.items()},
        "rms": round(rms, 4),
        "median_residual": round(med_res, 4) if med_res == med_res else None,
        "robust_sigma": round(mad_res, 4) if mad_res == mad_res else None,
        "quality": quality,
        "n_saturated_skipped": n_sat,
        "n_prerejected": n_prerej,
        "mean_fwhm": round(mean_fwhm, 2),
        "n_matched": len(matched),
        "n_used": n_used,
        "aperture": {"r_ap": r_ap, "r_in": r_in, "r_out": r_out},
        "image_size_px": {"w": w, "h": h},
        "matched": out_matched,
        "all_sources": all_sources,
        "limiting_mag": round(all_sources[-1]["calib_mag"], 2) if all_sources else None,
        "brightest_mag": round(all_sources[0]["calib_mag"], 2) if all_sources else None,
        "notes": [
            "Zeropoint pro Katalog getrennt berechnet (kein Farbterm)",
            "JPEG/PNG wird sRGB->linear entgamma-t; fuer exakte Photometrie FITS bevorzugen",
            f"Primaeres Band: {primary_cat}",
            "sigma_ZP < 0.1 mag = sehr gut; median-Residual sollte nahe 0 sein",
            f"Saettigungsgrenze: {sat_level:.0f} ADU ({n_sat} Sterne uebersprungen)",
            f"Pre-Rejection: {n_prerej} gesaettigte/nichtlineare Sterne nicht im ZP-Fit",
        ],
    }
