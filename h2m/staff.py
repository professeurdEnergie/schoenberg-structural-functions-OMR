"""Staff-line and barline detection (numpy + Pillow only).

Pipeline: ink mask with lighting normalisation -> global skew from projection
sharpness -> staves = 5 equally spaced peaks of the deskewed row profile ->
per-band refinement tracked outward from the centre (handles page curvature
and perspective) -> barlines = columns fully inked across every staff of a system.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageFilter, ImageOps

from .model import Barline, Staff


def load_image(path: str) -> Image.Image:
    """Load with EXIF orientation applied (phone photos), as RGB."""
    return ImageOps.exif_transpose(Image.open(path)).convert("RGB")


def ink_mask(img: Image.Image) -> np.ndarray:
    """Boolean H x W array, True = ink.  Divides out uneven lighting first."""
    gray = img.convert("L")
    w, h = gray.size
    small = gray.resize((max(1, w // 4), max(1, h // 4)), Image.BILINEAR)
    bg = small.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(6)).resize((w, h), Image.BILINEAR)
    g = np.asarray(gray, dtype=np.float32)
    b = np.maximum(np.asarray(bg, dtype=np.float32), 1.0)
    norm = np.clip(g / b, 0.0, 1.2)
    return norm < _otsu(norm)


def _otsu(values: np.ndarray) -> float:
    hist, edges = np.histogram(values, bins=256, range=(0.0, 1.2))
    p = hist.astype(np.float64) / max(1, hist.sum())
    centres = (edges[:-1] + edges[1:]) / 2
    omega = np.cumsum(p)
    mu = np.cumsum(p * centres)
    sigma = (mu[-1] * omega - mu) ** 2 / (omega * (1 - omega) + 1e-12)
    return float(np.clip(centres[int(np.argmax(sigma))], 0.55, 0.9))


def estimate_skew(mask: np.ndarray, max_deg: float = 6.0) -> float:
    """Angle (radians) that makes rows of ink sharpest; y grows with x if > 0."""
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return 0.0
    if len(xs) > 300_000:
        idx = np.random.default_rng(0).choice(len(xs), 300_000, replace=False)
        ys, xs = ys[idx], xs[idx]
    xc = xs - mask.shape[1] / 2.0
    pad = int(mask.shape[1] * math.tan(math.radians(max_deg))) + 2

    def sharpness(deg: float) -> float:
        yp = np.round(ys - xc * math.tan(math.radians(deg))).astype(np.int64) + pad
        return float(np.sum(np.bincount(yp).astype(np.float64) ** 2))

    best = max(np.arange(-max_deg, max_deg + 1e-9, 0.25), key=sharpness)
    best = max(np.arange(best - 0.25, best + 0.25 + 1e-9, 0.02), key=sharpness)
    return math.radians(float(best))


def _profile(mask: np.ndarray, x0: int, x1: int, slope: float, xc: float) -> np.ndarray:
    """Fraction of inked columns per (deskewed) row inside band [x0, x1)."""
    ys, xs = np.nonzero(mask[:, x0:x1])
    prof = np.zeros(mask.shape[0], dtype=np.float64)
    if len(xs):
        yp = np.round(ys - (xs + x0 - xc) * slope).astype(np.int64)
        ok = (yp >= 0) & (yp < len(prof))
        prof += np.bincount(yp[ok], minlength=len(prof))[: len(prof)]
    prof /= max(1, x1 - x0)
    return prof


def _widen(prof: np.ndarray) -> np.ndarray:
    """3-row max filter: tolerate residual tilt / sub-pixel positions."""
    return np.maximum(prof, np.maximum(np.roll(prof, 1), np.roll(prof, -1)))


def _peaks(prof: np.ndarray, thresh: float) -> list[tuple[float, float]]:
    """Centroids and heights of runs above threshold."""
    above = prof > thresh
    peaks, y = [], 0
    while y < len(prof):
        if above[y]:
            y1 = y
            while y1 < len(prof) and above[y1]:
                y1 += 1
            w = prof[y:y1]
            peaks.append((float(np.sum(np.arange(y, y1) * w) / np.sum(w)), float(w.max())))
            y = y1
        else:
            y += 1
    return peaks


def _staff_candidates(peaks: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """(top, spacing) for every run of 5 roughly equidistant peaks."""
    out, i = [], 0
    while i + 4 < len(peaks):
        # extend a run of consecutive, equally spaced peaks
        j = i + 1
        d = peaks[j][0] - peaks[i][0]
        while j + 1 < len(peaks) and abs((peaks[j + 1][0] - peaks[j][0]) - d) <= 0.2 * d:
            j += 1
        run = peaks[i:j + 1]
        if len(run) >= 5 and d >= 3:
            for start in range(0, len(run) - 4, 5) if len(run) >= 10 else [
                    max(range(len(run) - 4), key=lambda s: sum(h for _, h in run[s:s + 5]))]:
                win = run[start:start + 5]
                out.append((win[0][0], (win[4][0] - win[0][0]) / 4.0))
            i = j + 1
        else:
            i += 1
    return out


def _fit_band(prof: np.ndarray, top: float, spacing: float) -> tuple[float, float, float]:
    """Best (top, spacing, score) of a 5-line comb near the prediction.

    Coarse grid search on the widened profile, then sub-pixel refinement: the
    centroid of each line, followed by a least-squares fit y_k = top + k * spacing.
    """
    rows = np.arange(len(prof))
    wide = _widen(prof)
    best = (top, spacing, -1.0)
    for sc in np.arange(0.94, 1.0601, 0.01):
        s = spacing * sc
        for dy in np.arange(-0.6 * spacing, 0.6 * spacing + 1e-9, 0.5):
            ys = top + dy + s * np.arange(5)
            vals = np.interp(ys, rows, wide, left=0.0, right=0.0)
            score = float(vals.mean() + vals.min())  # every line must be present
            if score > best[2] + 1e-9:
                best = (top + dy, s, score)
    top_c, s_c, score = best
    if score < 1.0:
        return best
    cents = []
    for k in range(5):
        y = top_c + k * s_c
        lo, hi = int(math.floor(y - 0.35 * s_c)), int(math.ceil(y + 0.35 * s_c)) + 1
        lo, hi = max(0, lo), min(len(prof), hi)
        # keep only the line's core: stems / noteheads touching the line cover a
        # small fraction of the band and must not pull the centroid
        w = np.clip(prof[lo:hi] - 0.5 * prof[lo:hi].max(), 0.0, None)
        if w.sum() <= 0:
            return best
        cents.append(float(np.sum(np.arange(lo, hi) * w) / w.sum()))
    s_fit, top_fit = np.polyfit(np.arange(5), cents, 1)
    return float(top_fit), float(s_fit), score


@dataclass
class Detection:
    skew_deg: float
    staves: list[Staff]
    barlines: list[Barline]


def detect_staves(mask: np.ndarray) -> tuple[float, list[Staff]]:
    h, w = mask.shape
    theta = estimate_skew(mask)
    slope = math.tan(theta)
    xc = w / 2.0
    prof = _widen(_profile(mask, 0, w, slope, xc))
    peaks = _peaks(prof, 0.4 * prof.max()) if prof.max() > 0 else []
    cands = _staff_candidates(peaks)

    band = max(24, int(w / 24))
    starts = list(range(0, max(1, w - band + 1), max(1, band // 2)))
    centre_order = sorted(range(len(starts)), key=lambda k: abs(starts[k] + band / 2 - xc))
    band_profiles = {}

    staves: list[Staff] = []
    for top0, spacing0 in cands:
        samples = {}
        # track outward from the centre so curvature can accumulate gradually
        for direction in (+1, -1):
            k0 = centre_order[0]
            pred_top, pred_sp, pred_x = top0, spacing0, xc  # deskewed frame at x = xc
            k = k0 if direction == +1 else k0 - 1
            while 0 <= k < len(starts):
                x0 = starts[k]
                bx = x0 + band / 2.0
                if k not in band_profiles:
                    band_profiles[k] = _profile(mask, x0, min(w, x0 + band), slope, bx)
                # prediction in this band's frame: rows are deskewed around bx
                guess = pred_top + (bx - pred_x) * slope
                top, sp, score = _fit_band(band_profiles[k], guess, pred_sp)
                if score >= 1.0 and abs(sp - spacing0) < 0.15 * spacing0:
                    samples[k] = (bx, top, top + 4 * sp)
                    pred_top, pred_sp, pred_x = top, sp, bx
                k += direction
        if len(samples) < 2:
            continue
        pts = [samples[k] for k in sorted(samples)]
        pts = _drop_outliers(pts, spacing0)
        if len(pts) >= 2:
            staves.append(Staff("treble", 0, pts))

    staves.sort(key=lambda s: s.lines_at(w / 2)[0])
    for i, st in enumerate(staves):  # grand staff pairs: upper treble, lower bass
        st.system = i // 2
        st.clef = "treble" if i % 2 == 0 else "bass"
    return math.degrees(theta), staves


def _drop_outliers(pts, spacing):
    if len(pts) < 4:
        return pts
    xs = np.array([p[0] for p in pts])
    tops = np.array([p[1] for p in pts])
    a, b = np.polyfit(xs, tops, 1)
    med = np.median(np.abs(tops - (a * xs + b)))
    keep = np.abs(tops - (a * xs + b)) <= max(0.35 * spacing, 3 * med)
    return [p for p, k in zip(pts, keep) if k] or pts


def detect_barlines(mask: np.ndarray, staves: list[Staff]) -> list[Barline]:
    """Columns inked over >= 90% of every staff height in a system."""
    h, w = mask.shape
    md = mask.copy()
    md[:, 1:] |= mask[:, :-1]
    md[:, :-1] |= mask[:, 1:]
    out: list[Barline] = []
    for system in sorted({s.system for s in staves}):
        members = [s for s in staves if s.system == system]
        xs = np.arange(w)  # outside the staves the coverage test fails by itself
        geo = [np.array([st.lines_at(float(x)) for x in xs]) for st in members]
        y_mid = (geo[0][:, 0] + geo[-1][:, 1]) / 2.0
        slope = np.gradient(geo[0][:, 0])  # barlines are perpendicular to the staff lines
        cov = np.ones(len(xs))
        for tb in geo:
            ts = np.linspace(0.0, 1.0, 25)[:, None]
            yy = tb[:, 0][None, :] + ts * (tb[:, 1] - tb[:, 0])[None, :]
            xx = xs[None, :] - slope[None, :] * (yy - y_mid[None, :])
            yi = np.clip(np.round(yy).astype(int), 0, h - 1)
            xi = np.clip(np.round(xx).astype(int), 0, w - 1)
            cov = np.minimum(cov, md[yi, xi].mean(axis=0))
        cand = cov >= 0.9
        sp = float(np.median([s.spacing for s in members]))
        runs, i = [], 0
        while i < len(xs):
            if cand[i]:
                j = i
                while j + 1 < len(xs) and cand[j + 1]:
                    j += 1
                runs.append([float(xs[i]), float(xs[j])])
                i = j + 1
            else:
                i += 1
        merged = []
        for r in runs:  # double / final barlines -> one barline
            if merged and r[0] - merged[-1][1] <= 1.0 * sp:
                merged[-1][1] = r[1]
            else:
                merged.append(r)
        out += [Barline(system, (a + b) / 2.0) for a, b in merged]
    return out


def analyse(img: Image.Image) -> Detection:
    mask = ink_mask(img)
    skew, staves = detect_staves(mask)
    return Detection(skew, staves, detect_barlines(mask, staves))


def debug_overlay(img: Image.Image, det: Detection) -> Image.Image:
    from PIL import ImageDraw
    out = img.copy()
    d = ImageDraw.Draw(out)
    for st in det.staves:
        x0, x1 = st.x_range
        xs = np.linspace(x0, x1, 40)
        for k in range(5):
            pts = []
            for x in xs:
                t, b = st.lines_at(float(x))
                pts.append((float(x), t + k * (b - t) / 4))
            d.line(pts, fill=(255, 0, 0) if st.clef == "treble" else (0, 90, 255), width=1)
        t, b = st.lines_at(x0)
        d.text((max(0, x0 - 4), t - 14), f"{st.clef} s{st.system} sp={st.spacing:.1f}", fill=(200, 0, 0))
    for bl in det.barlines:
        members = [s for s in det.staves if s.system == bl.system]
        y0 = min(s.lines_at(bl.x)[0] for s in members)
        y1 = max(s.lines_at(bl.x)[1] for s in members)
        d.line([(bl.x, y0 - 6), (bl.x, y1 + 6)], fill=(0, 170, 0), width=2)
    return out
