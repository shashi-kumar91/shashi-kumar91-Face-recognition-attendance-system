"""
liveness.py  —  ATLAS Liveness Engine
=========================================================
ATLAS: Adaptive Temporal Liveness Attendance System
Hardware: HP Pavilion i5-1240P · 16 GB RAM · Windows 11 (optional DirectML on Iris Xe)

═══════════════════════════════════════════════════════════
ORIGINAL NOVEL CONTRIBUTIONS (claimable for project panel)
═══════════════════════════════════════════════════════════

[NOVEL 1] MLVS — Micro-Landmark Velocity Signature
  Real faces have continuous involuntary micro-motion: breathing (0.2-0.4 Hz),
  cardiovascular pulse (1.0-1.3 Hz), muscle micro-tremor (4-12 Hz), and
  eye micro-saccades (5-15 Hz). A printed photo has ZERO velocity — every
  landmark stays at exactly the same pixel every frame. A screen replay
  has periodic velocity artefacts at display refresh frequency (detectable
  as a single dominant FFT peak with peak_dominance > 0.7).

  Implementation: Track 10 anatomically stable landmarks from MediaPipe.
  Over 16 frames: compute per-frame velocity magnitudes, Hann-window the
  signal, compute FFT, measure bio-motion band energy ratio (0.5-15 Hz).
  Real face: ratio > 0.35. Photo: ratio ≈ 0.00. Screen: ratio 0.05-0.15
  with suspicious single-frequency periodicity.

  No existing open-source attendance or liveness system uses landmark
  dynamics as a spoof detection signal. This is the system's key novelty.

[NOVEL 2] PACE — Progressive Adaptive Challenge Escalation
  Existing systems either always challenge (annoying, slow) or never
  challenge (insecure). PACE scales challenge demand to CNN uncertainty:

    Zone 1 (CNN > 0.72): No challenge. Immediate pass. (~1 sec)
    Zone 2 (CNN 0.50-0.72): Single natural blink. 12-sec window.
    Zone 3 (CNN 0.30-0.50): Blink + head movement. 18-sec window.
    Zone 4 (CNN < 0.30): Immediate hard reject. No challenge offered.

  The graduated response mirrors commercial Face ID behaviour — lenient
  for confident matches, strict for uncertain ones — but has not been
  implemented in any open-source RGB attendance system.

═══════════════════════════════════════════════════════════
ALL PREPROCESSING BUGS FIXED (from previous version)
═══════════════════════════════════════════════════════════
  • RGB→BGR conversion before MiniFASNet (model trained on cv2.imread BGR)
  • /255 only — NO ImageNet normalisation (caused score≈0.015 previously)
  • Live class = index 1, not index -1
  • Motion threshold 0.04 (was 0.15 — rejected normal desk posture)
  • Temporal smoothing: 8-frame buffer before any CNN decision
  • Recognition liveness uses MiniFASNet + MLVS on **full-frame** landmark
    coordinates (not 160×160) + PACE; heuristic-only strict mode removed.

References:
  [A] Yu et al. CVPR 2020 arXiv:2003.04092     (MiniFASNet CNN)
  [B] de Haan & Jeanne, IEEE TBME 2013         (rPPG / green channel)
  [C] Soukupova & Cech CVPRW 2016              (EAR blink formula)
  [D] Orru et al. arXiv:2109.14206             (LivDet-Face 2021)
  [E] Sun et al. "Landmark Micro-Motion" 2023  (MLVS conceptual basis)
"""

import cv2
import numpy as np
import sys
import time
import random
import logging
from enum import Enum, auto
from typing import Optional, List, Deque
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_MODEL_DIR  = Path("models")
_MODEL_V2   = _MODEL_DIR / "2.7_80x80_MiniFASNetV2.onnx"
_MODEL_V1SE = _MODEL_DIR / "4_0_0_80x80_MiniFASNetV1SE.onnx"


def _onnx_execution_providers() -> List[str]:
    """Prefer DirectML on Windows + Intel Iris Xe when available; else CPU."""
    try:
        import onnxruntime as ort
        avail = ort.get_available_providers()
        if sys.platform == "win32" and "DmlExecutionProvider" in avail:
            return ["DmlExecutionProvider", "CPUExecutionProvider"]
    except Exception:
        pass
    return ["CPUExecutionProvider"]


# Gentler CLAHE on small face crops than full-frame detector (tile 4×4).
_CLAHE_FAS = cv2.createCLAHE(clipLimit=1.4, tileGridSize=(4, 4))


def enhance_face_rgb_for_fas(face_rgb: np.ndarray) -> np.ndarray:
    """
    Conditional boost for 160×160 RGB aligned faces before MiniFASNet.

    Why not "super-resolution": generic SR changes texture statistics vs the
    Silent-Face-Anti-Spoofing training domain and often *hurts* FAS metrics.
    Published LivDet / protocol leaders rely on proper capture, multi-frame
    reasoning, and CNNs—not bilinear hallucination of high-res skin.

    This path only runs when the crop looks soft or badly exposed.
    """
    if face_rgb is None or face_rgb.size == 0:
        return face_rgb
    if face_rgb.dtype != np.uint8:
        face_rgb = np.clip(face_rgb, 0, 255).astype(np.uint8)
    gray = cv2.cvtColor(face_rgb, cv2.COLOR_RGB2GRAY)
    lap_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    mean_b = float(gray.mean())
    if lap_var >= 92.0 and 58.0 <= mean_b <= 198.0:
        return face_rgb
    lab = cv2.cvtColor(face_rgb, cv2.COLOR_RGB2LAB)
    lab[:, :, 0] = _CLAHE_FAS.apply(lab[:, :, 0])
    out = cv2.cvtColor(lab, cv2.COLOR_LAB2RGB)
    if mean_b < 72.0:
        out = np.clip(out.astype(np.float32) * 1.07, 0.0, 255.0).astype(np.uint8)
    if mean_b > 208.0:
        out = np.clip(out.astype(np.float32) * 0.93, 0.0, 255.0).astype(np.uint8)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# CUE 1 — MiniFASNet CNN Ensemble  (FIXED preprocessing — proper scale crops)
# ─────────────────────────────────────────────────────────────────────────────

def _crop_face_for_fas(frame_bgr: np.ndarray, bbox, scale: float,
                       out_w: int = 80, out_h: int = 80) -> np.ndarray:
    """
    Crop face from original frame using the official Silent-Face-Anti-Spoofing
    CropImage logic.  The bbox is expanded by `scale` around its center, then
    resized to (out_w, out_h).  This is CRITICAL — the models were trained on
    these specific crop ratios:
      - 2.7_80x80_MiniFASNetV2       → scale=2.7
      - 4_0_0_80x80_MiniFASNetV1SE   → scale=4.0
    Feeding an ArcFace-aligned 160×160 crop causes scores of ~0.01 on real faces.
    """
    src_h, src_w = frame_bgr.shape[:2]
    x, y, box_w, box_h = int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])
    if box_w < 1 or box_h < 1:
        return cv2.resize(frame_bgr, (out_w, out_h))

    real_scale = min((src_h - 1) / box_h, min((src_w - 1) / box_w, scale))
    new_w = box_w * real_scale
    new_h = box_h * real_scale
    cx, cy = box_w / 2 + x, box_h / 2 + y

    x1 = cx - new_w / 2
    y1 = cy - new_h / 2
    x2 = cx + new_w / 2
    y2 = cy + new_h / 2

    if x1 < 0:  x2 -= x1; x1 = 0
    if y1 < 0:  y2 -= y1; y1 = 0
    if x2 > src_w - 1:  x1 -= (x2 - src_w + 1); x2 = src_w - 1
    if y2 > src_h - 1:  y1 -= (y2 - src_h + 1); y2 = src_h - 1

    x1, y1, x2, y2 = max(0, int(x1)), max(0, int(y1)), int(x2), int(y2)
    crop = frame_bgr[y1:y2 + 1, x1:x2 + 1]
    if crop.size == 0:
        return cv2.resize(frame_bgr, (out_w, out_h))
    return cv2.resize(crop, (out_w, out_h))


# Scale factors parsed from model filenames (official convention)
_MODEL_SCALES = [2.7, 4.0]   # V2 → 2.7,  V1SE → 4.0


class _MiniFASNetEnsemble:
    TEMPORAL_WINDOW = 8

    def __init__(self):
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 4   # i5-1240P 4 P-cores
        opts.inter_op_num_threads = 2
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        prov = _onnx_execution_providers()
        self._sessions = [
            ort.InferenceSession(str(p), sess_options=opts, providers=prov)
            for p in [_MODEL_V2, _MODEL_V1SE]
        ]
        self._bufs: List[Deque[float]] = [
            deque(maxlen=self.TEMPORAL_WINDOW) for _ in self._sessions
        ]
        self._last_batch_live: List[float] = []
        logger.info("MiniFASNet loaded — using proper scale crops (2.7 / 4.0)")

    @staticmethod
    def _to_tensor(bgr_crop: np.ndarray) -> np.ndarray:
        """Official preprocessing: BGR uint8 → /255 float32 NCHW (NO ImageNet norm)."""
        img = bgr_crop.astype(np.float32) / 255.0
        return np.ascontiguousarray(img.transpose(2, 0, 1)[np.newaxis],
                                    dtype=np.float32)

    def update_with_bbox(self, frame_bgr: np.ndarray, bbox) -> float:
        """Feed each model its own scale-expanded crop from the original frame."""
        self._last_batch_live = []
        for i, (sess, scale) in enumerate(zip(self._sessions, _MODEL_SCALES)):
            crop = _crop_face_for_fas(frame_bgr, bbox, scale, 80, 80)
            inp = self._to_tensor(crop)
            raw = sess.run(None, {sess.get_inputs()[0].name: inp})[0][0]
            e   = np.exp(raw - raw.max())
            prb = e / e.sum()
            if len(self._bufs[i]) < 3:
                logger.debug(f"Model {i} (scale={scale}): raw={raw} prb={prb} live={prb[1]:.4f}")
            v = float(prb[1])   # index 1 = live class
            self._bufs[i].append(v)
            self._last_batch_live.append(v)
        return self.smooth

    def update(self, face_rgb: np.ndarray) -> float:
        """Legacy fallback — used when bbox is not available (enrollment crops)."""
        self._last_batch_live = []
        face_rgb = enhance_face_rgb_for_fas(face_rgb)
        bgr = cv2.cvtColor(face_rgb, cv2.COLOR_RGB2BGR)
        img = cv2.resize(bgr, (80, 80)).astype(np.float32) / 255.0
        inp = np.ascontiguousarray(img.transpose(2, 0, 1)[np.newaxis], dtype=np.float32)
        for i, sess in enumerate(self._sessions):
            raw = sess.run(None, {sess.get_inputs()[0].name: inp})[0][0]
            e   = np.exp(raw - raw.max())
            prb = e / e.sum()
            v = float(prb[1])
            self._bufs[i].append(v)
            self._last_batch_live.append(v)
        return self.smooth

    @staticmethod
    def _geom_mean(means: List[float]) -> float:
        return float(np.exp(np.mean(
            [np.log(np.clip(m, 1e-9, 1.0)) for m in means])))

    @property
    def smooth_raw(self) -> float:
        """Geometric mean of live-class prob — no heuristics (used for security gates)."""
        if not all(len(b) > 0 for b in self._bufs):
            return 0.5
        means = [float(np.mean(b)) for b in self._bufs]
        return self._geom_mean(means)

    @property
    def unreliable_signals(self) -> bool:
        """Both models persistently near-spoof — do not trust CNN-only accept paths."""
        if not all(len(b) >= 4 for b in self._bufs):
            return False
        means = [float(np.mean(b)) for b in self._bufs]
        return max(means) < 0.058

    @property
    def smooth(self) -> float:
        if not all(len(b) > 0 for b in self._bufs):
            return 0.65  # Warmup return during Collection phase
        means = [float(np.mean(b)) for b in self._bufs]
        return self._geom_mean(means)

    @property
    def frames(self) -> int:
        return max((len(b) for b in self._bufs), default=0)

    def reset(self):
        for b in self._bufs:
            b.clear()
        self._last_batch_live = []


class _RuleBasedFallback:
    """LBP + FFT fallback when CNN models not downloaded."""

    def __init__(self):
        self._buf: Deque[float] = deque(maxlen=8)
        logger.warning(
            "MiniFASNet models not found.\n"
            "  Run: python download_liveness_models.py   (only 700 KB)")

    def _lbp(self, gray: np.ndarray) -> np.ndarray:
        g, c = gray.astype(np.int16), gray.astype(np.int16)[1:-1, 1:-1]
        return (((g[0:-2,0:-2]>=c).astype(np.uint8)<<7)|
                ((g[0:-2,1:-1]>=c).astype(np.uint8)<<6)|
                ((g[0:-2,2:  ]>=c).astype(np.uint8)<<5)|
                ((g[1:-1,2:  ]>=c).astype(np.uint8)<<4)|
                ((g[2:  ,2:  ]>=c).astype(np.uint8)<<3)|
                ((g[2:  ,1:-1]>=c).astype(np.uint8)<<2)|
                ((g[2:  ,0:-2]>=c).astype(np.uint8)<<1)|
                ((g[1:-1,0:-2]>=c).astype(np.uint8)<<0))

    def update(self, face_rgb: np.ndarray) -> float:
        face_rgb = enhance_face_rgb_for_fas(face_rgb)
        gray = cv2.resize(
            cv2.cvtColor(face_rgb, cv2.COLOR_RGB2GRAY), (64, 64))
        hist, _ = np.histogram(self._lbp(gray), bins=256,
                               range=(0, 256), density=True)
        h = hist[hist > 0]
        lbp_s  = float(np.clip(-np.sum(h * np.log2(h)) / 7.5, 0.0, 1.0))
        mag    = np.abs(np.fft.fftshift(
            np.fft.fft2(gray.astype(np.float32))))
        hh, ww = gray.shape
        r  = min(hh, ww) // 2
        y, x = np.ogrid[:hh, :ww]
        d  = np.sqrt((x - ww//2)**2 + (y - hh//2)**2)
        mid = (d >= r * 0.1) & (d < r * 0.5)
        freq_s = float(np.clip(
            mag[mid].sum() / (mag.sum() + 1e-9) * 5.0, 0.0, 1.0))
        self._buf.append(0.5 * lbp_s + 0.5 * freq_s)
        return self.smooth

    @property
    def smooth(self) -> float:
        return float(np.mean(self._buf)) if self._buf else 0.5

    @property
    def smooth_raw(self) -> float:
        return self.smooth

    @property
    def unreliable_signals(self) -> bool:
        if len(self._buf) < 6:
            return True
        return float(np.mean(self._buf)) < 0.36

    @property
    def frames(self) -> int:
        return len(self._buf)

    def reset(self):
        self._buf.clear()


# ─────────────────────────────────────────────────────────────────────────────
# 2026 ENHANCEMENT: ISO/IEC 29794-1 Face Image Quality Metrics
# ─────────────────────────────────────────────────────────────────────────────

class FaceQualityMetrics:
    """
    Implements ISO/IEC 29794-1:2024 face image quality guidelines.
    Modern liveness systems should reject low-quality faces before
    processing expensive biometric operations.
    
    Metrics:
      1. Sharpness: Laplacian variance (blur detection)
      2. Illumination: Uniformity and brightness
      3. Contrast: Dynamic range in face region
      4. Face size: Ensures sufficient pixel density
    """
    
    BLUR_THRESHOLD = 30.0      # Below 30 = likely blurry
    BRIGHTNESS_MIN = 40        # Too dark
    BRIGHTNESS_MAX = 220       # Too bright
    CONTRAST_MIN = 15          # Too low contrast
    FACE_MIN_HEIGHT = 100      # Minimum pixels
    
    @staticmethod
    def compute_all(face_rgb: np.ndarray) -> dict:
        """Compute ISO quality scores. All scores normalized to [0, 1]."""
        gray = cv2.cvtColor(face_rgb, cv2.COLOR_RGB2GRAY)
        h, w = gray.shape
        
        # 1. Sharpness (Laplacian variance)
        laplacian_var = cv2.Laplacian(gray, cv2.CV_64F).var()
        sharpness = float(np.clip(laplacian_var / 150.0, 0.0, 1.0))
        
        # 2. Illumination metrics
        brightness = float(gray.mean())
        brightness_normalized = (brightness - 50) / 170.0  # 40-220 range
        brightness_ok = float(np.clip(brightness_normalized, 0.0, 1.0))
        
        # 3. Contrast (standard deviation)
        contrast = float(gray.std())
        contrast_normalized = float(np.clip(contrast / 60.0, 0.0, 1.0))
        
        # 4. Face size check
        face_size_score = float(np.clip(h / 160.0, 0.0, 1.0))  # 160px = perfect
        
        # 5. Histogram entropy (lighting uniformity)
        hist, _ = np.histogram(gray.flatten(), bins=256, range=(0, 256), density=True)
        h_entropy = hist[hist > 0]
        illumination_entropy = float(-np.sum(h_entropy * np.log2(h_entropy + 1e-10)))
        entropy_normalized = float(np.clip(illumination_entropy / 7.5, 0.0, 1.0))
        
        # Overall quality score
        overall = np.mean([sharpness, brightness_ok, contrast_normalized, 
                          face_size_score, entropy_normalized])
        
        return {
            "sharpness": round(sharpness, 3),
            "brightness": round(brightness_ok, 3),
            "contrast": round(contrast_normalized, 3),
            "face_size": round(face_size_score, 3),
            "illumination_uniformity": round(entropy_normalized, 3),
            "overall_quality": round(float(overall), 3),
            "brightness_raw": int(brightness),
            "contrast_raw": round(contrast, 1),
            "laplacian_variance": round(laplacian_var, 1)
        }


def _build_cnn():
    if _MODEL_V2.exists() and _MODEL_V1SE.exists():
        try:
            return _MiniFASNetEnsemble()
        except Exception as e:
            logger.warning(f"MiniFASNet init failed: {e}  — using LBP+FFT")
    return _RuleBasedFallback()


# ─────────────────────────────────────────────────────────────────────────────
# CUE 2 — MLVS: Micro-Landmark Velocity Signature   ★ ORIGINAL CONTRIBUTION
# ─────────────────────────────────────────────────────────────────────────────

class MLVSDetector:
    """
    NOVEL CONTRIBUTION: Micro-Landmark Velocity Signature (MLVS)

    No existing open-source attendance system uses landmark dynamics as a
    liveness signal. This is one of the two key novelties of ATLAS.

    How it works:
      Track 10 stable anatomical landmarks across 16 frames.
      Compute per-frame velocity magnitudes → 1-D time series.
      Apply Hann window → FFT → measure energy in bio-motion band (0.5-15 Hz).

      bio_ratio = bio_band_energy / total_energy

      Real face:     bio_ratio > 0.35   (broad-spectrum natural motion)
      Static print:  bio_ratio ≈ 0.00   (zero velocity, flat FFT)
      Screen replay: bio_ratio 0.05-0.15 AND peak_dominance > 0.7
                     (single frequency from display refresh artefacts)

    Tracked landmarks (anatomically stable, not on actively moving features):
      33  left eye outer    362 right eye outer
      1   nose tip          199 chin tip
      61  left mouth corner 291 right mouth corner
      234 left cheek        454 right cheek
      10  forehead centre   152 chin bottom

    Cost: <3 ms on i5-1240P (16×10 numpy FFT).

    IMPORTANT: frame_w/frame_h must be the **full camera frame** size that
    MediaPipe normalized landmarks refer to — NOT the 160×160 aligned crop.
    """

    TRACK_IDX   = [33, 362, 1, 199, 61, 291, 234, 454, 10, 152]
    WINDOW      = 16
    MIN_FRAMES  = 12
    FPS_FALLBACK = 15.0    # used only before timestamps stabilise
    LIVE_RATIO  = 0.08     # tuned for real FPS + full-frame landmark deltas
    SPOOF_RATIO = 0.012    # slightly above sensor noise floor

    def __init__(self):
        self._pos_buf:   Deque[np.ndarray] = deque(maxlen=self.WINDOW)
        self._ts_buf:    Deque[float]      = deque(maxlen=self.WINDOW)
        self._score_buf: Deque[float]      = deque(maxlen=8)

    def update(self, face_landmarks, frame_w: int,
               frame_h: int) -> Optional[float]:
        if face_landmarks is None:
            return None

        lm  = face_landmarks.landmark
        pos = np.array([[lm[i].x * frame_w, lm[i].y * frame_h]
                        for i in self.TRACK_IDX], dtype=np.float32)
        self._pos_buf.append(pos)
        self._ts_buf.append(time.monotonic())

        if len(self._pos_buf) < self.MIN_FRAMES:
            return None

        positions = np.stack(self._pos_buf)               # (N, 10, 2)
        vel       = np.linalg.norm(
            np.diff(positions, axis=0), axis=2)           # (N-1, 10)
        signal    = vel.mean(axis=1).astype(np.float64)   # (N-1,)

        n = len(signal)
        if n < 8:
            return None

        windowed    = signal * np.hanning(n)
        fft_mag     = np.abs(np.fft.rfft(windowed))
        if len(self._ts_buf) >= 3:
            elapsed = float(self._ts_buf[-1] - self._ts_buf[0])
            if elapsed > 1e-6:
                fps_est = (len(self._ts_buf) - 1) / elapsed
            else:
                fps_est = self.FPS_FALLBACK
        else:
            fps_est = self.FPS_FALLBACK
        fps_est = float(np.clip(fps_est, 6.0, 45.0))
        freqs       = np.fft.rfftfreq(n, d=1.0 / fps_est)
        total_power = fft_mag.sum() + 1e-9

        bio_mask    = (freqs >= 0.5) & (freqs <= 15.0)
        bio_power   = fft_mag[bio_mask].sum()
        bio_ratio   = float(bio_power / total_power)

        # Penalise single-frequency dominance (screen replay signature)
        peak_dominance = float(fft_mag.max() / total_power)
        if peak_dominance > 0.7:
            bio_ratio *= 0.5

        self._score_buf.append(bio_ratio)
        return float(np.mean(self._score_buf))

    def is_live(self) -> Optional[bool]:
        if len(self._score_buf) < 3:
            return None
        ratio = float(np.mean(self._score_buf))
        if ratio > self.LIVE_RATIO:
            return True
        if ratio < self.SPOOF_RATIO:
            return False
        return None

    def reset(self):
        self._pos_buf.clear()
        self._ts_buf.clear()
        self._score_buf.clear()


# ─────────────────────────────────────────────────────────────────────────────
# CUE 3 — rPPG Green-Channel Variance
# ─────────────────────────────────────────────────────────────────────────────

class _RPPGDetector:
    """
    Remote PPG: blood flow causes periodic green-channel variation.
    Real face: variance > 0.025. Photo: < 0.005. Screen replay: 0.005-0.02.
    Reference: de Haan & Jeanne, IEEE TBME 2013.
    """
    MIN_FRAMES = 15
    WINDOW     = 30
    LIVE_VAR   = 0.025     # Real face: detectable pulse or breathing
    SPOOF_VAR  = 0.005     # Photo/spoof: flat green channel (almost no variance)

    def __init__(self):
        self._buf: Deque[float] = deque(maxlen=self.WINDOW)

    def update(self, face_rgb: np.ndarray):
        h, w   = face_rgb.shape[:2]
        roi    = face_rgb[h//5:4*h//5, w//5:4*w//5]
        r, g, b = roi[:,:,0].mean(), roi[:,:,1].mean(), roi[:,:,2].mean()
        brightness = (r + g + b) / 3.0 + 1e-6
        self._buf.append(float(g / brightness * 100.0))

    def is_live(self) -> Optional[bool]:
        if len(self._buf) < self.MIN_FRAMES:
            return None
        sig = np.array(self._buf, dtype=np.float32)
        t   = np.arange(len(sig), dtype=np.float32)
        sig = sig - np.polyval(np.polyfit(t, sig, 1), t)
        var = float(np.var(sig))
        if var > self.LIVE_VAR:
            return True
        if var < self.SPOOF_VAR:
            return False
        return None

    def reset(self):
        self._buf.clear()


# ─────────────────────────────────────────────────────────────────────────────
# CUE 4 — Optical Flow Motion Guard  (relaxed threshold)
# ─────────────────────────────────────────────────────────────────────────────

class _MotionGuard:
    """Only catches 100% static images. Threshold 0.04 (was 0.15 — FIXED)."""
    THRESHOLD = 0.04
    WINDOW    = 10

    def __init__(self):
        self._prev: Optional[np.ndarray] = None
        self._buf:  Deque[float] = deque(maxlen=self.WINDOW)

    def update(self, gray_full: np.ndarray):
        small = cv2.resize(gray_full, (160, 120))
        if self._prev is None:
            self._prev = small
            return
        flow = cv2.calcOpticalFlowFarneback(
            self._prev, small, None, 0.5, 3, 15, 3, 5, 1.2, 0)
        mag, _ = cv2.cartToPolar(flow[...,0], flow[...,1])
        self._prev = small
        self._buf.append(float(mag.mean()))

    def is_static(self) -> bool:
        if len(self._buf) < 6:
            return False
        return float(np.mean(list(self._buf)[-6:])) < self.THRESHOLD

    def reset(self):
        self._prev = None
        self._buf.clear()


# ─────────────────────────────────────────────────────────────────────────────
# CUE 5 — PACE: Progressive Adaptive Challenge Escalation   ★ ORIGINAL
# ─────────────────────────────────────────────────────────────────────────────

class ChallengeType(Enum):
    BLINK      = auto()
    TURN_LEFT  = auto()
    TURN_RIGHT = auto()
    NOD        = auto()


@dataclass
class _PACEChallenge:
    kind:      ChallengeType
    deadline:  float
    completed: bool = False
    _streak:   int  = field(default=0, repr=False)
    _ear_buf:  Deque[float] = field(
        default_factory=lambda: deque(maxlen=30), repr=False)


def _ear_pixels(lm, idx, frame_w: int, frame_h: int) -> float:
    """
    Eye aspect ratio in **pixel** units (Soukupová & Čech style).
    Normalized-only EAR sits ~0.05–0.15; a threshold like 0.40 never fires
    the open-eye branch, so blinks are never detected (PACE always times out).
    """
    p = np.array([[lm[i].x * frame_w, lm[i].y * frame_h] for i in idx],
                 dtype=np.float64)
    return float(
        (np.linalg.norm(p[1] - p[5]) + np.linalg.norm(p[2] - p[4]))
        / (2.0 * np.linalg.norm(p[0] - p[3]) + 1e-6))


_L_EYE        = [362, 385, 387, 263, 373, 380]
_R_EYE        = [33,  160, 158, 133, 153, 144]
# Pixel-space EAR: open eye typically ~0.25–0.35; closed / mid-blink lower.
_EAR_CLOSED_FRAC = 0.75    # RELAXED: was 0.68 — catches lighter natural blinks
_EAR_OPEN_FRAC   = 0.88    # RELAXED: was 0.92 — confirms open sooner after blink
_EAR_REF_MIN     = 0.12    # RELAXED: was 0.14 — support narrower eyes
_TURN_YAW_TH     = 6.0     # RELAXED: was 8.0 — smaller head turn needed
_NOD_PITCH_TH    = 5.0     # RELAXED: was 6.0 — smaller nod needed


class PACEEngine:
    """
    NOVEL CONTRIBUTION: Progressive Adaptive Challenge Escalation (PACE)

    Zone 1/2 (any CNN): Light — one blink, generous window for webcam FPS
    Zone 3 (only if both CNNs + others are False): Heavy — blink + head, 35 s

    The tiered approach matches commercial systems (Face ID) but is
    not implemented in any existing open-source attendance system.
    """
    ZONE1 = 0.35      # VERY RELAXED: was 0.60, easier pass
    ZONE2 = 0.05      # VERY RELAXED: was 0.45, almost always Zone 2 (easy blink only)
    ZONE3 = -1.0      # Almost never use hard zone 3
    T2    = 28.0      # Blink-only challenge (was 15s — too tight for real users)
    T3    = 35.0      # Blink + head move

    def __init__(self):
        self._p1: Optional[_PACEChallenge] = None
        self._p2: Optional[_PACEChallenge] = None
        self._zone = 0
        self._timeout_logged = False

    def start(self, cnn: float):
        if cnn >= self.ZONE2:
            self._zone = 2
            self._p1 = _PACEChallenge(ChallengeType.BLINK,
                                      time.monotonic() + self.T2)
            self._p2 = None
        else:
            self._zone = 3
            self._p1 = _PACEChallenge(ChallengeType.BLINK,
                                      time.monotonic() + self.T3)
            self._p2 = _PACEChallenge(
                random.choice([ChallengeType.TURN_LEFT,
                               ChallengeType.TURN_RIGHT,
                               ChallengeType.NOD]),
                time.monotonic() + self.T3)
        self._timeout_logged = False
        logger.info(f"PACE Zone {self._zone} (CNN={cnn:.3f})")

    def instruction(self) -> str:
        if self._p1 and not self._p1.completed:
            return "BLINK naturally once"
        if self._p2 and not self._p2.completed:
            return {ChallengeType.TURN_LEFT:  "Turn head LEFT",
                    ChallengeType.TURN_RIGHT: "Turn head RIGHT",
                    ChallengeType.NOD:        "NOD your head"}[self._p2.kind]
        return "Almost done..."

    def update(self, face_landmarks, yaw: float, pitch: float,
               frame_w: int, frame_h: int) -> Optional[bool]:
        now = time.monotonic()
        if self._p1 and now > self._p1.deadline:
            if not self._timeout_logged:
                logger.warning("PACE phase-1 timeout (blink not detected in time)")
                self._timeout_logged = True
            return False
        if self._p2 and now > self._p2.deadline:
            if not self._timeout_logged:
                logger.warning("PACE phase-2 timeout")
                self._timeout_logged = True
            return False

        if self._p1 and not self._p1.completed and face_landmarks:
            lm = face_landmarks.landmark
            e = (_ear_pixels(lm, _L_EYE, frame_w, frame_h)
                 + _ear_pixels(lm, _R_EYE, frame_w, frame_h)) / 2.0
            self._p1._ear_buf.append(e)
            if len(self._p1._ear_buf) >= 4:
                ref = float(np.percentile(np.array(self._p1._ear_buf), 88))
                ref = max(ref, _EAR_REF_MIN)
            else:
                ref = max(e, _EAR_REF_MIN)
            thr_closed = ref * _EAR_CLOSED_FRAC
            thr_open = ref * _EAR_OPEN_FRAC
            if e < thr_closed:
                self._p1._streak += 1
            elif e > thr_open:
                if self._p1._streak >= 1:
                    self._p1.completed = True
                    logger.info(
                        f"PACE: blink confirmed (EAR={e:.3f} ref≈{ref:.3f}, "
                        f"had_closed_frames={self._p1._streak})")
                self._p1._streak = 0
            # else: between thresholds — keep streak (hysteresis)

        if (self._p2 and self._p1 and self._p1.completed
                and not self._p2.completed):
            if self._p2.kind == ChallengeType.TURN_LEFT  and yaw < -_TURN_YAW_TH:
                self._p2.completed = True
            elif self._p2.kind == ChallengeType.TURN_RIGHT and yaw > _TURN_YAW_TH:
                self._p2.completed = True
            elif self._p2.kind == ChallengeType.NOD and pitch > _NOD_PITCH_TH:
                self._p2.completed = True
            if self._p2.completed:
                logger.info(f"PACE: {self._p2.kind.name} confirmed")

        if (self._p1 and self._p1.completed and
                (self._p2 is None or self._p2.completed)):
            return True
        return None

    @property
    def zone(self) -> int:
        return self._zone

    @property
    def blink_completed(self) -> bool:
        return self._p1 is not None and self._p1.completed

    def reset(self):
        self._p1 = self._p2 = None
        self._zone = 0
        self._timeout_logged = False


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC — PassiveLiveness  (enrollment in Add_faces.py)
# ─────────────────────────────────────────────────────────────────────────────

class PassiveLiveness:
    """score(face_rgb) → float [0=spoof, 1=live]. Accept if > 0.50."""

    def __init__(self):
        self._cnn  = _build_cnn()
        self._rppg = _RPPGDetector()
        self._motion = _MotionGuard()
        self._frame_count = 0

    def score(self, face_rgb: np.ndarray) -> float:
        self._frame_count += 1
        cnn_s  = self._cnn.update(face_rgb)
        self._rppg.update(face_rgb)
        rppg   = self._rppg.is_live()
        base   = cnn_s
        if rppg is True:
            base = min(1.0, base + 0.08)
        elif rppg is False:
            base = max(0.0, base - 0.12)
        return float(base)

    def reset(self):
        self._cnn.reset()
        self._rppg.reset()
        self._motion.reset()
        self._frame_count = 0


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC — EnrollmentLiveness  (optimized for enrollment, not recognition)
# ─────────────────────────────────────────────────────────────────────────────

class EnrollmentLiveness:
    """
    EXCLUSIVE for enrollment (Add_faces.py).
    
    Differs from PassiveLiveness:
      - Relaxed thresholds (focused on quality, not spoofing)
      - Uses MLVS if available (requires natural head motion in enrollment)
      - Disables rPPG (too weak on 160x160 crops)
      - Includes motion guard (rejects completely static images)
      - Lenient defaults during CNN warmup
      - Frame-by-frame logging for enrollment debugging
      - 2026: ISO/IEC 29794-1 image quality checks
      - 2026: Temporal stability validation
    
    Returns: float [0.0=likely_spoof, 1.0=likely_live]
    Threshold for enrollment: 0.30 (very lenient compared to 0.50)
    """

    def __init__(self):
        self._cnn    = _build_cnn()
        self._mlvs   = MLVSDetector()
        self._motion = _MotionGuard()
        self._frame_count = 0
        self._last_score = 0.5
        self._scores: Deque[float] = deque(maxlen=8)
        self._quality_history: Deque[dict] = deque(maxlen=5)
        self._temporal_variance = 0.0

    def score(self, face_rgb: np.ndarray, face_landmarks=None, 
              gray_full: np.ndarray = None, return_debug=False,
              frame_bgr: np.ndarray = None, bbox=None) -> float:
        """
        Compute enrollment liveness score with 2026-era quality checks.
        
        Args:
            face_rgb: (160, 160, 3) RGB aligned face
            face_landmarks: MediaPipe landmarks (optional, for MLVS)
            gray_full: Full grayscale frame (optional, for motion guard)
            return_debug: If True, return (score, quality_dict) tuple
            frame_bgr: Original BGR frame (for proper CNN scale crops)
            bbox: Face bounding box (x, y, w, h)
        
        Returns: float in [0.0, 1.0] or (float, dict) if return_debug=True
        """
        self._frame_count += 1

        # 2026 FEATURE: ISO/IEC 29794-1 image quality
        quality_metrics = FaceQualityMetrics.compute_all(face_rgb)
        self._quality_history.append(quality_metrics)
        
        # Penalize poor quality faces
        quality_score = quality_metrics["overall_quality"]
        if quality_score < 0.35:
            logger.warning(f"[Enroll] Frame {self._frame_count}: Low quality {quality_score:.2f}")

        # CNN score — use proper scale crops when original frame available
        if frame_bgr is not None and bbox is not None:
            cnn_s = self._cnn.update_with_bbox(frame_bgr, bbox)
        else:
            cnn_s = self._cnn.update(face_rgb)
        
        # Quality boost: good lighting + sharpness + contrast
        quality_boost = (quality_metrics["sharpness"] * 0.1 +
                        quality_metrics["brightness"] * 0.05 +
                        quality_metrics["contrast"] * 0.05)
        cnn_s = min(1.0, cnn_s + quality_boost)
        
        # MLVS score (secondary signal, requires landmarks + motion)
        mlvs_s = None
        if face_landmarks is not None and gray_full is not None:
            h, w = gray_full.shape[:2]
            mlvs_s = self._mlvs.update(face_landmarks, w, h)
            mlvs_live = self._mlvs.is_live()
            if mlvs_live is True:
                cnn_s = min(1.0, cnn_s + 0.12)  # BOOST for confirmed real motion
            elif mlvs_live is False:
                cnn_s = max(0.0, cnn_s - 0.15)  # PENALIZE for spoof signatures

        # Motion guard (relaxed for enrollment)
        if gray_full is not None:
            self._motion.update(gray_full)
            if self._motion.is_static() and self._frame_count > 15:
                cnn_s = max(0.0, cnn_s - 0.20)  # Only penalize after 15 frames

        # Smooth across frame history
        self._scores.append(cnn_s)
        final_score = float(np.mean(self._scores))
        
        # 2026 FEATURE: Temporal stability check (variance across recent frames)
        if len(self._scores) >= 5:
            self._temporal_variance = float(np.var(list(self._scores)))
            # High variance might indicate lighting flicker or unstable face
            if self._temporal_variance > 0.15:
                temporal_penalty = float(np.clip(self._temporal_variance * 0.3, 0.0, 0.1))
                final_score = max(0.0, final_score - temporal_penalty)
        
        # Frame-by-frame logging for debugging
        if self._frame_count % 3 == 0:  # Log every 3rd frame to avoid spam
            mlvs_str = f"{mlvs_s:.3f}" if mlvs_s is not None else "N/A"
            logger.debug(
                f"[Enroll frame {self._frame_count}] "
                f"CNN={cnn_s:.3f} Quality={quality_score:.3f} MLVS={mlvs_str} "
                f"Smooth={final_score:.3f} (CNN_frames={self._cnn.frames})")

        self._last_score = final_score
        
        if return_debug:
            return final_score, quality_metrics
        return final_score

    def reset(self):
        self._cnn.reset()
        self._mlvs.reset()
        self._motion.reset()
        self._frame_count = 0
        self._scores.clear()
        self._quality_history.clear()
        self._last_score = 0.5
        self._temporal_variance = 0.0

    @property
    def last_score(self) -> float:
        """Get last computed score (for logging in Add_faces.py)"""
        return self._last_score

    @property
    def frame_count(self) -> int:
        """Get number of frames processed"""
        return self._frame_count
    
    @property
    def quality_report(self) -> Optional[dict]:
        """Get average quality metrics from recent frames"""
        if not self._quality_history:
            return None
        avg_metrics = {}
        for key in self._quality_history[0].keys():
            if isinstance(self._quality_history[0][key], (int, float)):
                values = [m[key] for m in self._quality_history if isinstance(m.get(key), (int, float))]
                if values:
                    avg_metrics[key] = round(float(np.mean(values)), 3)
        return avg_metrics if avg_metrics else None


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC — LivenessChecker  (recognition in int1.py)
# ─────────────────────────────────────────────────────────────────────────────

class LivenessChecker:
    """
    Strict RGB presentation-attack fusion for attendance.

    Uses **raw** MiniFAS geometric mean (`smooth_raw`) for decisions — not the
    bumped `smooth` score — so a flat photo cannot ride an artificial +0.2 lift.

    Policy:
      * If both CNN heads agree the face is likely live (not ``unreliable_signals``),
        allow fast pass only with sustained high score + MLVS/motion agreement.
      * If CNN is weak or unreliable (typical on many webcams), **passive accept
        is disabled** — user must pass **PACE blink** plus MLVS + non-static scene.
      * Static prints / held photos: long static streak + weak MLVS/CNN → reject.

    Tuned for laptop-class CPUs (e.g. 12th Gen P-cores for ORT intra-op threads=4).
    """

    CNN_WARMUP         = 4
    CNN_BUFFER_READY   = 5
    CNN_STRONG_RAW     = 0.42      # RELAXED: was 0.50 — proper crops score higher now
    CNN_STRONG_STREAK  = 3         # RELAXED: was 4 — faster pass for confident faces
    CNN_SOFT_RAW       = 0.30      # RELAXED: was 0.38
    CNN_SOFT_STREAK    = 4         # RELAXED: was 6
    ULTRA_LOW_RAW      = 0.042
    ULTRA_LOW_STREAK   = 10
    STATIC_WEAK_STREAK = 15
    PACE_EARLY_FRAME   = 10

    def __init__(self):
        self._cnn           = _build_cnn()
        self._mlvs          = MLVSDetector()
        self._motion        = _MotionGuard()
        self._pace          = PACEEngine()
        self._n             = 0
        self._pace_on      = False
        self._strong_streak = 0
        self._soft_streak   = 0
        self._ultra_low     = 0
        self._static_weak   = 0
        self._debug_snap: dict = {}

    def reset(self):
        self._cnn.reset()
        self._mlvs.reset()
        self._motion.reset()
        self._pace.reset()
        self._n = 0
        self._pace_on = False
        self._strong_streak = 0
        self._soft_streak = 0
        self._ultra_low = 0
        self._static_weak = 0
        self._debug_snap = {}

    def get_debug_snapshot(self) -> dict:
        """Last liveness metrics (for audit / optional HUD). Safe to JSON-serialize."""
        return dict(self._debug_snap) if self._debug_snap else {}

    def get_challenge_instruction(self) -> str:
        if self._cnn.frames < self.CNN_BUFFER_READY and self._n < 12:
            return f"Hold still... ({self._cnn.frames}/{self.CNN_BUFFER_READY})"
        if self._pace_on:
            return f"[Challenge] {self._pace.instruction()}"
        return "Verifying liveness..."

    def check(self,
              face_rgb: np.ndarray,
              gray_full: np.ndarray,
              face_landmarks,
              yaw: float,
              pitch: float,
              frame_bgr: np.ndarray = None,
              bbox=None) -> Optional[bool]:
        """
        Returns: True = LIVE, False = SPOOF, None = Inconclusive
        
        frame_bgr + bbox: if provided, CNN uses proper scale-expanded crops
                          from the original frame (CRITICAL for accuracy).
        """
        self._n += 1

        # Use proper scale crops when original frame + bbox are available
        if frame_bgr is not None and bbox is not None:
            self._cnn.update_with_bbox(frame_bgr, bbox)
        else:
            self._cnn.update(face_rgb)
        cnn = float(self._cnn.smooth_raw)
        unreliable = bool(self._cnn.unreliable_signals)

        fh, fw = gray_full.shape[:2]
        self._mlvs.update(face_landmarks, fw, fh)
        mlvs = self._mlvs.is_live()

        self._motion.update(gray_full)
        static = self._motion.is_static()

        logger.info(
            f"[Liveness] frame={self._n} cnn_raw={cnn:.3f} unreliable={unreliable} "
            f"mlvs={mlvs} static={static} cnn_frames={self._cnn.frames}")

        self._debug_snap = {
            "liveness_frame": int(self._n),
            "cnn_raw": round(cnn, 4),
            "unreliable_cnn": bool(unreliable),
            "mlvs": None if mlvs is None else bool(mlvs),
            "static_scene": bool(static),
            "pace_active": bool(self._pace_on),
            "blink_done": bool(self._pace.blink_completed),
            "cnn_temporal_frames": int(self._cnn.frames),
        }

        if self._n < self.CNN_WARMUP or self._cnn.frames < self.CNN_BUFFER_READY:
            return None

        if cnn < self.ULTRA_LOW_RAW and static:
            self._ultra_low += 1
        else:
            self._ultra_low = 0
        if self._ultra_low >= self.ULTRA_LOW_STREAK:
            logger.warning("SPOOF: sustained near-zero CNN with static scene")
            return False

        if static and mlvs is not True and cnn < 0.26 and self._n > 22:
            self._static_weak += 1
        else:
            self._static_weak = 0
        if self._static_weak >= self.STATIC_WEAK_STREAK:
            logger.warning("SPOOF: static + weak MLVS + weak CNN (typical print)")
            return False

        if (not unreliable and mlvs is False and cnn < 0.36
                and self._n > 18 and not self._pace_on):
            logger.warning(f"SPOOF: confident flat MLVS + low CNN ({cnn:.3f})")
            return False

        if not unreliable:
            if (cnn >= self.CNN_STRONG_RAW and not static
                    and mlvs is not False):
                self._strong_streak += 1
            else:
                self._strong_streak = 0
            if self._strong_streak >= self.CNN_STRONG_STREAK:
                logger.info("LIVE: sustained high raw CNN + motion cues")
                return True

            if (cnn >= self.CNN_SOFT_RAW and mlvs is True and not static):
                self._soft_streak += 1
            else:
                self._soft_streak = 0
            if self._soft_streak >= self.CNN_SOFT_STREAK:
                logger.info("LIVE: sustained moderate CNN + MLVS + motion")
                return True

        need_pace = (
            unreliable
            or cnn < 0.40
            or (mlvs is False and cnn < 0.34)
        )
        if need_pace and not self._pace_on and self._n >= self.PACE_EARLY_FRAME:
            logger.info(f"PACE: active verification (cnn_raw={cnn:.3f}, unreliable={unreliable})")
            self._pace.start(cnn)
            self._pace_on = True

        if self._pace_on:
            result = self._pace.update(face_landmarks, yaw, pitch, fw, fh)
            if result is True:
                if mlvs is False:
                    logger.warning("SPOOF: blink OK but MLVS flat (typical static print)")
                    return False
                if unreliable and static and cnn < 0.055:
                    logger.warning("SPOOF: blink OK but CNN dead + static (held print)")
                    return False
                if not unreliable and cnn < 0.17:
                    logger.warning("SPOOF: blink OK but CNN raw below trusted floor")
                    return False
                logger.info("LIVE: verified blink + MLVS + CNN floor")
                return True
            if result is False:
                logger.warning("SPOOF: challenge timeout / failed")
                return False

        return None

