"""
Bildbasierte Galaxien-Morphologie-Schätzung
=============================================

Schätzt den Hubble-Typ (E / S0 / Spirale / Irregulär) einer Galaxie direkt
aus einem Bildausschnitt — ohne externe Katalog-Daten. Das ist eine
*photometrische* Klassifikation, kein Deep-Learning-Modell: sie misst
klassische morphologische Kennzahlen, die in der Astronomie seit Jahrzehnten
zur automatischen Galaxienklassifikation verwendet werden (siehe z.B.
Conselice 2003 "CAS-System": Concentration, Asymmetry, Smoothness).

Gemessene Merkmale
-------------------
1. **Konzentration (C)**: Verhältnis der Flussradien r80/r20 (wie stark ist
   das Licht zur Mitte hin konzentriert). Ellipsen sind hoch konzentriert
   (großer Bulge, scharfer Kern), späte Spiralen/Irreguläre sind flacher.
2. **Elliptizität (ε)**: Achsenverhältnis b/a aus den Bildmomenten zweiter
   Ordnung. Ellipsen sind rund bis mäßig elliptisch, edge-on Spiralen sehr
   lang gestreckt.
3. **Asymmetrie (A)**: Wie sehr unterscheidet sich das Bild von seiner um
   180° gedrehten Version (normiert auf den Gesamtfluss). Spiralarme und
   Irreguläre sind asymmetrisch, Ellipsen sehr symmetrisch.
4. **Glattheit / Clumpiness (S)**: Wie viel hochfrequente (kleinräumige)
   Struktur das Bild relativ zum geglätteten Bild hat. Spiralarme und
   Sternentstehungsregionen erzeugen "Klumpigkeit", Ellipsen sind glatt.
5. **Farbindex** (falls Farbbild): rötlich = alte Sternpopulation (E/S0),
   bläulich = aktive Sternentstehung (späte Spiralen/Irreguläre).

Diese fünf Werte zusammen ergeben eine Position im CAS-Diagramm, die auf
die Hubble-Sequenz abgebildet wird. Das ist eine GROBE SCHÄTZUNG für
schwache/kleine Galaxien im Amateurbild — kein Ersatz für professionelle
morphologische Kataloge (RC3, NED), aber eine sinnvolle Ergänzung wenn kein
Katalog-Eintrag existiert.

Limitationen (werden im Ergebnis als "confidence" mitgegeben)
----------------------------------------------------------------------
- Benötigt eine Mindestgröße im Bild (>~15px Durchmesser), sonst sind die
  Bildmomente zu verrauscht.
- Seeing/Tracking-Fehler (Sternverschmierung) verfälschen Asymmetrie/Smoothness.
- Inklination (Sichtwinkel) beeinflusst Elliptizität stark — eine edge-on
  Spirale sieht "elliptisch" aus, ist aber morphologisch eine Spirale.
  Wird über Konzentration + Asymmetrie teilweise kompensiert.
- Kein Ersatz für spektroskopische/professionelle Klassifikation.
"""

import math
import numpy as np
from PIL import Image


def _radial_profile(img_gray: np.ndarray, cx: float, cy: float, max_r: int,
                     sat_level: float = None):
    """
    Mittlere Helligkeit in konzentrischen Ringen um (cx, cy).

    Bei gesättigten Zentralpixeln (8-bit-Clipping bei 255) kann die wahre
    Spitzenhelligkeit nicht direkt gemessen werden. Statt die gesättigten
    Ringe einfach mit dem letzten gültigen Wert aufzufüllen (das erzeugt ein
    künstliches FLACHES PLATEAU im Zentrum und lässt einen scharfen,
    gesättigten Kern wie einen flachen, ausgedehnten Kern aussehen — das
    UNTERSCHÄTZT den Konzentrationsindex systematisch und lässt Ellipsen
    fälschlich wie Spiralen erscheinen), wird der innere gesättigte Bereich
    aus dem äußeren, nicht-gesättigten Profilverlauf nach innen
    EXTRAPOLIERT (log-linearer Fit auf die ersten gültigen Ringe direkt
    außerhalb des gesättigten Kerns, fortgesetzt mit gleicher Steilheit).
    """
    h, w = img_gray.shape
    if sat_level is None:
        img_max = float(img_gray.max())
        n_at_max = int(np.sum(img_gray >= img_max - 0.5))
        if img_max >= 250 and n_at_max >= 4:
            sat_level = img_max - 0.5
        else:
            sat_level = img_max + 1.0  # kein Clipping erkannt -> Schutz inaktiv

    y, x = np.ogrid[:h, :w]
    r = np.sqrt((x - cx) ** 2 + (y - cy) ** 2).astype(int)
    r = np.clip(r, 0, max_r)
    valid = img_gray < sat_level
    flat_r = r.ravel()
    flat_img = img_gray.ravel()
    flat_valid = valid.ravel()
    tbin = np.bincount(flat_r[flat_valid], flat_img[flat_valid], minlength=max_r + 1)
    nbin = np.bincount(flat_r[flat_valid], minlength=max_r + 1)
    nbin_safe = nbin.copy()
    nbin_safe[nbin_safe == 0] = 1
    result = tbin / nbin_safe
    zero_mask = nbin == 0

    if zero_mask.any() and not zero_mask.all():
        first_valid_idx = int(np.argmax(~zero_mask))
        fit_end = min(first_valid_idx + 8, len(result))
        fit_r = np.arange(first_valid_idx, fit_end)
        fit_vals = np.maximum(result[first_valid_idx:fit_end], 1e-3)
        if len(fit_r) >= 3 and first_valid_idx > 0:
            try:
                coeffs = np.polyfit(fit_r, np.log(fit_vals), 1)
                for i in range(first_valid_idx):
                    result[i] = math.exp(coeffs[1] + coeffs[0] * i)
            except Exception:
                for i in range(first_valid_idx):
                    result[i] = result[first_valid_idx]
        elif first_valid_idx > 0:
            for i in range(first_valid_idx):
                result[i] = result[first_valid_idx]
        # Verbleibende isolierte Luecken im Profil (nicht am Anfang)
        # weiterhin mit letztem gueltigen Wert auffuellen
        last_good = result[0]
        for i in range(len(result)):
            if zero_mask[i] and i >= first_valid_idx:
                result[i] = last_good
            else:
                last_good = result[i]
    return result


def _flux_radius(profile: np.ndarray, frac: float) -> float:
    """
    Radius, innerhalb dessen `frac` des Gesamtflusses (kumulativ) liegt.

    Gewichtung pro Ring i: Ringflaeche = pi*((i+0.5)^2 - (i-0.5)^2) = pi*2*i
    fuer i>0, und pi*0.25 fuer den Zentralring i=0 (Radius 0 bis 0.5).
    Die vorherige Version gewichtete mit reinem Umfang (2*pi*i), was den
    Zentralpixel (i=0) mit Gewicht 0 komplett ignorierte und dadurch r20/r80
    fuer stark zentral konzentrierte (elliptische) Profile systematisch zu
    GROSS schaetzte -> Ellipsen wurden faelschlich als Spiralen erkannt.
    """
    n = len(profile)
    i = np.arange(n, dtype=np.float64)
    ring_area = np.where(i == 0, math.pi * 0.25, 2 * math.pi * i)
    flux_per_ring = profile * ring_area
    cum = np.cumsum(flux_per_ring)
    total = cum[-1] if cum[-1] > 0 else 1
    target = frac * total
    idx = np.searchsorted(cum, target)
    return float(min(idx, n - 1))


def _second_moments(img_gray: np.ndarray, cx: float, cy: float, r_max: float):
    """Bildmomente 2. Ordnung -> Elliptizität und Positionswinkel."""
    h, w = img_gray.shape
    y0, y1 = max(0, int(cy - r_max)), min(h, int(cy + r_max))
    x0, x1 = max(0, int(cx - r_max)), min(w, int(cx + r_max))
    sub = img_gray[y0:y1, x0:x1].astype(np.float64)
    if sub.size == 0:
        return 1.0, 0.0
    yy, xx = np.mgrid[y0:y1, x0:x1].astype(np.float64)
    weight = np.clip(sub - np.percentile(sub, 20), 0, None)
    wsum = weight.sum()
    if wsum <= 0:
        return 1.0, 0.0
    mx = (weight * xx).sum() / wsum
    my = (weight * yy).sum() / wsum
    mxx = (weight * (xx - mx) ** 2).sum() / wsum
    myy = (weight * (yy - my) ** 2).sum() / wsum
    mxy = (weight * (xx - mx) * (yy - my)).sum() / wsum
    # Eigenwerte der 2x2 Kovarianzmatrix -> Haupt-/Nebenachse
    tr = mxx + myy
    det = mxx * myy - mxy ** 2
    disc = max(tr ** 2 / 4 - det, 0)
    lam1 = tr / 2 + math.sqrt(disc)
    lam2 = tr / 2 - math.sqrt(disc)
    if lam1 <= 0:
        return 1.0, 0.0
    ellipticity = 1.0 - math.sqrt(max(lam2, 0) / lam1)
    pa = 0.5 * math.atan2(2 * mxy, mxx - myy)
    return ellipticity, pa


def _asymmetry(img_gray: np.ndarray, cx: float, cy: float, r: float) -> float:
    """A = Summe|I - I_180| / (2*Summe|I|), nach Conselice 2003.
    Misst nur innerhalb einer kreisfoermigen Apertur mit Radius r,
    damit Hintergrundrauschen ausserhalb der Galaxie das Ergebnis
    nicht dominiert."""
    h, w = img_gray.shape
    ri = int(math.ceil(r))
    y0, y1 = max(0, int(cy - ri)), min(h, int(cy + ri))
    x0, x1 = max(0, int(cx - ri)), min(w, int(cx + ri))
    sub = img_gray[y0:y1, x0:x1].astype(np.float64)
    if sub.size < 16:
        return 0.0
    yy, xx = np.mgrid[:sub.shape[0], :sub.shape[1]].astype(np.float64)
    local_cx, local_cy = cx - x0, cy - y0
    dist = np.sqrt((xx - local_cx)**2 + (yy - local_cy)**2)
    mask = dist <= r
    bg = np.percentile(sub[~mask], 50) if (~mask).any() else np.percentile(sub, 15)
    sub = np.clip(sub - bg, 0, None) * mask
    rotated = np.rot90(sub, 2)
    if rotated.shape != sub.shape:
        m = min(sub.shape[0], rotated.shape[0]), min(sub.shape[1], rotated.shape[1])
        sub = sub[:m[0], :m[1]]
        rotated = rotated[:m[0], :m[1]]
    denom = 2 * np.sum(np.abs(sub))
    if denom <= 0:
        return 0.0
    return float(np.sum(np.abs(sub - rotated)) / denom)


def _smoothness(img_gray: np.ndarray, cx: float, cy: float, r: float) -> float:
    """S = hochfrequenter Anteil relativ zum geglätteten Bild (Clumpiness).
    Misst nur innerhalb einer kreisfoermigen Apertur mit Radius r."""
    h, w = img_gray.shape
    ri = int(math.ceil(r))
    y0, y1 = max(0, int(cy - ri)), min(h, int(cy + ri))
    x0, x1 = max(0, int(cx - ri)), min(w, int(cx + ri))
    sub = img_gray[y0:y1, x0:x1].astype(np.float64)
    if sub.shape[0] < 5 or sub.shape[1] < 5:
        return 0.0
    yy, xx = np.mgrid[:sub.shape[0], :sub.shape[1]].astype(np.float64)
    local_cx, local_cy = cx - x0, cy - y0
    dist = np.sqrt((xx - local_cx)**2 + (yy - local_cy)**2)
    mask = dist <= r
    bg = np.percentile(sub[~mask], 50) if (~mask).any() else np.percentile(sub, 15)
    sub = np.clip(sub - bg, 0, None) * mask
    k = max(3, min(5, int(r / 3) * 2 + 1))
    pad = k // 2
    padded = np.pad(sub, pad, mode='edge')
    smooth = np.zeros_like(sub)
    cs = np.cumsum(np.cumsum(padded, axis=0), axis=1)
    cs = np.pad(cs, ((1, 0), (1, 0)), mode='constant')
    for i in range(sub.shape[0]):
        for j in range(sub.shape[1]):
            smooth[i, j] = (cs[i + k, j + k] - cs[i, j + k] - cs[i + k, j] + cs[i, j]) / (k * k)
    sub_masked = sub[mask]
    smooth_masked = smooth[mask]
    denom = np.sum(np.abs(sub_masked))
    if denom <= 0:
        return 0.0
    return float(np.sum(np.abs(sub_masked - smooth_masked)) / denom)


def _color_index(img_rgb: np.ndarray, cx: float, cy: float, r: float):
    """Grober Farbindex (rot - blau Verhaeltnis) im Galaxienbereich, falls Farbbild."""
    h, w = img_rgb.shape[:2]
    y0, y1 = max(0, int(cy - r)), min(h, int(cy + r))
    x0, x1 = max(0, int(cx - r)), min(w, int(cx + r))
    sub = img_rgb[y0:y1, x0:x1]
    if sub.size == 0 or sub.ndim < 3:
        return None
    gray = sub.mean(axis=2)
    bg = np.percentile(gray, 15)
    mask = gray > bg + 5
    if mask.sum() < 10:
        return None
    r_mean = sub[..., 0][mask].mean()
    b_mean = sub[..., 2][mask].mean() if sub.shape[2] > 2 else r_mean
    if b_mean <= 0:
        return None
    return float(r_mean / max(b_mean, 1e-3))


def estimate_morphology(img_path: str, x: float, y: float,
                         box_radius_px: float = 60) -> dict:
    """
    Schätzt den Hubble-Typ einer Galaxie an Pixel-Position (x, y).

    Rückgabe:
        {
          "type": "E" | "S0" | "Sa".."Sm" | "Irr" | None,
          "label": Menschenlesbare Beschreibung,
          "confidence": 0.0-1.0 (grobe Konfidenz, NICHT statistisch kalibriert),
          "features": {concentration, ellipticity, asymmetry, smoothness, color_index},
          "method": "image_estimate",
        }
        Bei zu kleinem/verrauschtem Ausschnitt: {"type": None, "reason": "..."}
    """
    try:
        img = Image.open(img_path)
        img_rgb = np.array(img.convert("RGB"), dtype=np.float64)
    except Exception as e:
        return {"type": None, "reason": f"Bild nicht ladbar: {e}"}

    h, w = img_rgb.shape[:2]
    if not (0 <= x < w and 0 <= y < h):
        return {"type": None, "reason": "Position außerhalb des Bildes"}

    img_gray = img_rgb.mean(axis=2)

    r_max = min(box_radius_px, x, y, w - x, h - y)
    if r_max < 5:
        return {"type": None, "reason": "Zu nah am Bildrand für verlässliche Messung"}

    # Lokalen Schwerpunkt/Radius über radiales Profil grob justieren
    profile = _radial_profile(img_gray[
        max(0, int(y - r_max)):min(h, int(y + r_max)),
        max(0, int(x - r_max)):min(w, int(x + r_max))
    ], min(x, r_max), min(y, r_max), int(r_max))

    if profile.sum() < 1e-6 or len(profile) < 4:
        return {"type": None, "reason": "Zu schwach/klein für Merkmalsextraktion"}

    outer = profile[max(1, len(profile)*3//4):]
    bg_level = float(np.median(outer))
    bg_rms = float(np.std(outer)) if len(outer) > 2 else 1.0
    noise_thresh = bg_level + max(bg_rms, 0.5)
    peak_snr = (profile[0] - bg_level) / max(bg_rms, 0.1)
    if peak_snr < 3.0:
        return {"type": None, "reason": "Zu schwach (SNR < 3)"}
    edge = len(profile)
    for k in range(1, len(profile)):
        if profile[k] <= noise_thresh:
            edge = k
            break
    if edge < 3:
        return {"type": None, "reason": "Zu klein (< 3px Radius)"}
    profile_sub = np.zeros_like(profile)
    profile_sub[:edge] = np.clip(profile[:edge] - bg_level, 0, None)
    if profile_sub.sum() < 1e-6:
        return {"type": None, "reason": "Kein Signal über Hintergrund"}

    r20 = _flux_radius(profile_sub, 0.2)
    r80 = _flux_radius(profile_sub, 0.8)
    if r20 < 0.5:
        r20 = 0.5
    concentration = 5.0 * math.log10(max(r80, 1.0) / r20) if r80 > r20 else 0.0

    gal_r = max(r80 * 1.5, 4.0)
    measure_r = min(gal_r, r_max)

    ellipticity, _pa = _second_moments(img_gray, x, y, measure_r)
    raw_asym = _asymmetry(img_gray, x, y, measure_r)
    bg_asym = _background_asymmetry(img_gray, x, y, measure_r, r_max)
    asymmetry = max(0.0, raw_asym - bg_asym)
    smoothness = _smoothness(img_gray, x, y, min(measure_r, 40))
    color_idx = _color_index(img_rgb, x, y, measure_r)

    gtype, label, confidence = _classify_from_cas(
        concentration, asymmetry, smoothness, color_idx, gal_r, r_max)

    return {
        "type": gtype,
        "label": label,
        "confidence": confidence,
        "features": {
            "concentration": round(concentration, 3),
            "ellipticity": round(ellipticity, 3),
            "asymmetry": round(asymmetry, 3),
            "smoothness": round(smoothness, 3),
            "color_index": round(color_idx, 3) if color_idx is not None else None,
        },
        "method": "image_estimate",
    }


def _background_asymmetry(img_gray, cx, cy, r, r_max):
    """Asymmetrie in einer leeren Hintergrund-Region (gleiche Apertur).
    Conselice (2003): A_korrigiert = A_galaxie - A_hintergrund."""
    h, w = img_gray.shape
    offsets = [(2.5, 0), (-2.5, 0), (0, 2.5), (0, -2.5)]
    bg_vals = []
    for dx_mul, dy_mul in offsets:
        bx = cx + r_max * dx_mul / 3.0
        by = cy + r_max * dy_mul / 3.0
        if 0 <= bx - r < w and bx + r < w and 0 <= by - r < h and by + r < h:
            a = _asymmetry(img_gray, bx, by, r)
            bg_vals.append(a)
    return float(np.median(bg_vals)) if bg_vals else 0.0


def _classify_from_cas(concentration, asymmetry, smoothness, color_idx,
                        gal_r, r_max):
    """Klassifikation aus CAS-Merkmalen mit groessenabhaengigen Schwellenwerten.
    Fuer kleine Galaxien (gal_r < 12px) ist A/S unzuverlaessig wegen
    Rauschen -> Klassifikation primaer ueber Konzentration."""
    confidence = 0.5
    cas_reliable = gal_r >= 12

    if cas_reliable:
        if concentration > 3.8 and asymmetry < 0.15 and smoothness < 0.15:
            gtype, label = "E", "Elliptisch (geschätzt)"
            confidence = 0.55 + min(0.25, (concentration - 3.8) * 0.08)
        elif concentration > 3.0 and asymmetry < 0.22 and smoothness < 0.22:
            gtype, label = "S0", "Lentikulär S0 (geschätzt)"
            confidence = 0.45 + min(0.2, (concentration - 3.0) * 0.1)
        elif asymmetry > 0.35 and smoothness > 0.30:
            gtype, label = "Irr", "Irregulär / wechselwirkend (geschätzt)"
            confidence = 0.4 + min(0.3, (max(asymmetry, smoothness) - 0.35) * 0.5)
        else:
            if concentration > 2.8:
                sub = "ab"
            elif concentration > 2.3:
                sub = "bc"
            elif concentration > 1.8:
                sub = "c"
            else:
                sub = "cd"
            gtype, label = "S" + sub, f"Spirale S{sub} (geschätzt)"
            confidence = 0.35 + min(0.25, asymmetry * 0.5)
    else:
        if concentration > 3.5:
            gtype, label = "E", "Elliptisch (geschätzt)"
            confidence = 0.35 + min(0.15, (concentration - 3.5) * 0.06)
        elif concentration > 2.8:
            gtype, label = "S0", "Lentikulär S0 (geschätzt)"
            confidence = 0.30 + min(0.15, (concentration - 2.8) * 0.08)
        elif concentration > 2.2:
            gtype, label = "Sab", "Spirale Sab (geschätzt)"
            confidence = 0.25
        elif concentration > 1.6:
            gtype, label = "Sbc", "Spirale Sbc (geschätzt)"
            confidence = 0.25
        else:
            gtype, label = "Scd", "Spirale Scd (geschätzt)"
            confidence = 0.20

    if color_idx is not None:
        if gtype.startswith("S") and gtype not in ("S0",) and color_idx > 1.5:
            confidence *= 0.85
        elif gtype == "E" and color_idx < 1.0:
            confidence *= 0.85

    if gal_r < 6:
        confidence *= 0.4
    elif gal_r < 10:
        confidence *= 0.55
    elif gal_r < 20:
        confidence *= 0.7
    confidence = round(min(max(confidence, 0.05), 0.85), 2)

    return gtype, label, confidence


def _estimate_from_arrays(img_rgb, img_gray, x, y, box_radius_px=60):
    h, w = img_rgb.shape[:2]
    if not (0 <= x < w and 0 <= y < h):
        return {"type": None, "reason": "Position außerhalb des Bildes"}

    r_max = min(box_radius_px, x, y, w - x, h - y)
    if r_max < 5:
        return {"type": None, "reason": "Zu nah am Bildrand"}

    profile = _radial_profile(img_gray[
        max(0, int(y - r_max)):min(h, int(y + r_max)),
        max(0, int(x - r_max)):min(w, int(x + r_max))
    ], min(x, r_max), min(y, r_max), int(r_max))

    if profile.sum() < 1e-6 or len(profile) < 4:
        return {"type": None, "reason": "Zu schwach/klein"}

    outer = profile[max(1, len(profile)*3//4):]
    bg_level = float(np.median(outer))
    bg_rms = float(np.std(outer)) if len(outer) > 2 else 1.0
    noise_thresh = bg_level + max(bg_rms, 0.5)
    peak_snr = (profile[0] - bg_level) / max(bg_rms, 0.1)
    if peak_snr < 3.0:
        return {"type": None, "reason": "Zu schwach (SNR < 3)"}
    edge = len(profile)
    for k in range(1, len(profile)):
        if profile[k] <= noise_thresh:
            edge = k
            break
    if edge < 3:
        return {"type": None, "reason": "Zu klein (< 3px Radius)"}
    profile_sub = np.zeros_like(profile)
    profile_sub[:edge] = np.clip(profile[:edge] - bg_level, 0, None)
    if profile_sub.sum() < 1e-6:
        return {"type": None, "reason": "Kein Signal über Hintergrund"}

    r20 = _flux_radius(profile_sub, 0.2)
    r80 = _flux_radius(profile_sub, 0.8)
    if r20 < 0.5:
        r20 = 0.5
    concentration = 5.0 * math.log10(max(r80, 1.0) / r20) if r80 > r20 else 0.0

    gal_r = max(r80 * 1.5, 4.0)
    measure_r = min(gal_r, r_max)

    ellipticity, _pa = _second_moments(img_gray, x, y, measure_r)
    raw_asym = _asymmetry(img_gray, x, y, measure_r)
    bg_asym = _background_asymmetry(img_gray, x, y, measure_r, r_max)
    asymmetry = max(0.0, raw_asym - bg_asym)
    smoothness = _smoothness(img_gray, x, y, min(measure_r, 40))
    color_idx = _color_index(img_rgb, x, y, measure_r)

    gtype, label, confidence = _classify_from_cas(
        concentration, asymmetry, smoothness, color_idx, gal_r, r_max)

    return {
        "type": gtype,
        "label": label,
        "confidence": confidence,
        "features": {
            "concentration": round(concentration, 3),
            "ellipticity": round(ellipticity, 3),
            "asymmetry": round(asymmetry, 3),
            "smoothness": round(smoothness, 3),
            "color_index": round(color_idx, 3) if color_idx is not None else None,
        },
        "method": "image_estimate",
    }


def estimate_batch(img_path: str, objects: list, progress_cb=None) -> list:
    """
    Schätzt Morphologie für eine Liste von Galaxien-Objekten.
    Lädt das Bild nur einmal und verwendet die Arrays für alle Objekte.
    objects: Liste von Dicts mit mind. {"id", "x", "y"}.
    Rückgabe: Liste von Dicts {"id", **estimate-Ergebnis}.
    """
    try:
        img = Image.open(img_path)
        img_rgb = np.array(img.convert("RGB"), dtype=np.float64)
    except Exception as e:
        return [{"id": o.get("id"), "type": None, "reason": f"Bild nicht ladbar: {e}"}
                for o in objects]
    img_gray = img_rgb.mean(axis=2)

    out = []
    n = len(objects)
    for i, o in enumerate(objects):
        if progress_cb and i % 100 == 0:
            progress_cb(f"Bildanalyse {i}/{n} ...")
        x, y = o.get("x"), o.get("y")
        if x is None or y is None:
            out.append({"id": o.get("id"), "type": None, "reason": "Keine Pixel-Position"})
            continue
        res = _estimate_from_arrays(img_rgb, img_gray, float(x), float(y))
        res["id"] = o.get("id")
        out.append(res)
    if progress_cb:
        n_ok = sum(1 for r in out if r.get("type"))
        progress_cb(f"Bildanalyse fertig: {n_ok}/{n} Galaxien geschätzt")
    return out
