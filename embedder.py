"""
embedder.py — ATLAS Embedding Store with APBC
==============================================
ATLAS: Adaptive Temporal Liveness Attendance System

NOVEL: APBC — Adaptive Per-Person Biometric Calibration
Existing systems use one global threshold. APBC derives a
person-specific threshold at enrollment time from 3 quality metrics:
  [1] Illumination Entropy  — dim lighting → widen threshold
  [2] Pose Spread           — frontal-only → widen threshold  
  [3] Embedding Compactness — noisy enrollment → widen threshold
"""

import json
import logging
import sys
import numpy as np
import cv2
import datetime
from typing import Dict, List, Optional, Tuple
from pathlib import Path

logger = logging.getLogger(__name__)
logging.getLogger("faiss").setLevel(logging.WARNING)

ONNX_MODEL_PATH = Path("models/w600k_r50.onnx")


def _onnx_providers_arcface() -> List[str]:
    """DirectML on Windows + Intel iGPU when available; else CPU (i5-1240P friendly)."""
    try:
        import onnxruntime as ort
        avail = ort.get_available_providers()
        if sys.platform == "win32" and "DmlExecutionProvider" in avail:
            return ["DmlExecutionProvider", "CPUExecutionProvider"]
    except Exception:
        pass
    return ["CPUExecutionProvider"]


class _OnnxArcFaceBackend:
    def __init__(self, model_path: Path):
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 4
        opts.inter_op_num_threads = 2
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        prov = _onnx_providers_arcface()
        self._session = ort.InferenceSession(str(model_path), sess_options=opts,
                                             providers=prov)
        self._input_name = self._session.get_inputs()[0].name
        logger.info(f"ArcFace ONNX loaded: {model_path}  providers={self._session.get_providers()[:2]}")

    def embed(self, face_rgb_160: np.ndarray) -> np.ndarray:
        bgr = cv2.cvtColor(face_rgb_160, cv2.COLOR_RGB2BGR)
        img = cv2.resize(bgr, (112, 112)).astype(np.float32)
        img = (img - 127.5) / 127.5
        img = img.transpose(2, 0, 1)[np.newaxis]
        feat = self._session.run(None, {self._input_name: img})[0][0].astype(np.float32)
        return feat / (np.linalg.norm(feat) + 1e-10)


class _FaceNetBackend:
    def __init__(self):
        from keras_facenet import FaceNet
        self._fn = FaceNet()
        logger.info("FaceNet fallback backend loaded")

    def embed(self, face_rgb_160: np.ndarray) -> np.ndarray:
        v = self._fn.embeddings([face_rgb_160])[0].astype(np.float32)
        return v / (np.linalg.norm(v) + 1e-10)


def _build_backend():
    if ONNX_MODEL_PATH.exists():
        try:
            return _OnnxArcFaceBackend(ONNX_MODEL_PATH)
        except Exception as e:
            logger.warning(f"ONNX backend failed: {e}")
    try:
        return _FaceNetBackend()
    except ImportError:
        raise RuntimeError("No embedding backend. Download ArcFace ONNX model.")


def _augment(face_rgb: np.ndarray) -> List[np.ndarray]:
    bright = np.clip(face_rgb.astype(np.int16) + 18, 0, 255).astype(np.uint8)
    dark   = np.clip(face_rgb.astype(np.int16) - 18, 0, 255).astype(np.uint8)
    return [face_rgb, cv2.flip(face_rgb, 1), bright, dark]


# ---------------------------------------------------------------------------
# APBC: Adaptive Per-Person Biometric Calibration  NOVEL CONTRIBUTION
# ---------------------------------------------------------------------------

def compute_apbc_threshold(
        face_samples: List[np.ndarray],
        yaw_angles:   List[float],
        embeddings:   np.ndarray) -> Tuple[float, dict]:
    """
    NOVEL: Computes a person-specific recognition threshold at enrollment.

    Three quality metrics determine how strict the threshold is:
      Illumination entropy: bright varied light -> can afford tighter threshold
      Pose spread:          diverse head angles -> more robust -> tighter
      Embedding compactness: tight cluster -> confident identity -> tighter

    Returns (threshold in [0.28, 0.78], quality_dict for storage/audit).
    """
    base = 0.35      # RELAXED: was 0.42, allow lower base for relaxed enrollment

    # Metric 1: Illumination entropy
    entropies = []
    for face in face_samples:
        gray = cv2.cvtColor(face, cv2.COLOR_RGB2GRAY)
        hist, _ = np.histogram(gray.flatten(), bins=256, range=(0, 256), density=True)
        h = hist[hist > 0]
        entropies.append(float(-np.sum(h * np.log2(h))))
    mean_entropy = float(np.mean(entropies))
    illum_q = float(np.clip(mean_entropy / 7.5, 0.0, 1.0))
    illum_bonus = 0.08 * illum_q      # RELAXED: was 0.10

    # Metric 2: Pose spread
    pose_spread = float(np.std(yaw_angles)) if len(yaw_angles) >= 3 else 0.0
    pose_q = float(np.clip(pose_spread / 20.0, 0.0, 1.0))
    pose_bonus = 0.06 * pose_q        # RELAXED: was 0.08

    # Metric 3: Embedding compactness
    n = len(embeddings)
    sims = [float(embeddings[i] @ embeddings[j])
            for i in range(n) for j in range(i+1, n)]
    mean_sim = float(np.mean(sims)) if sims else 0.6
    std_sim  = float(np.std(sims))  if sims else 0.1
    compact_bonus = float(np.clip((mean_sim - 0.5) * 0.2, -0.06, 0.08))  # RELAXED: was 0.3, -0.08, 0.12

    threshold = float(np.clip(base - illum_bonus - pose_bonus - compact_bonus, 0.28, 0.55))  # RELAXED: upper bound 0.55, was 0.78

    quality = {
        "illumination_entropy": round(mean_entropy, 3),
        "illumination_quality": round(illum_q, 3),
        "pose_spread_deg":      round(pose_spread, 2),
        "pose_quality":         round(pose_q, 3),
        "embedding_mean_sim":   round(mean_sim, 4),
        "embedding_std_sim":    round(std_sim, 4),
        "base_threshold":       round(base, 3),
        "illum_bonus":          round(illum_bonus, 4),
        "pose_bonus":           round(pose_bonus, 4),
        "compact_bonus":        round(compact_bonus, 4),
        "final_threshold":      round(threshold, 4),
    }
    logger.info(f"APBC: illum={illum_q:.2f} pose={pose_q:.2f} "
                f"compact={mean_sim:.3f} -> threshold={threshold:.4f}")
    return threshold, quality


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------

class _NumpyIndex:
    def __init__(self):
        self._vecs:   List[np.ndarray] = []
        self._labels: List[str]        = []

    def add(self, vectors: np.ndarray, labels: List[str]):
        for v, l in zip(vectors, labels):
            self._vecs.append(v.astype(np.float32))
            self._labels.append(l)

    def search(self, query: np.ndarray, k: int = 5) -> List[Tuple[str, float]]:
        if not self._vecs: return []
        mat  = np.stack(self._vecs)
        sims = mat @ query.astype(np.float32)
        top  = np.argsort(sims)[::-1][:k]
        return [(self._labels[i], float(sims[i])) for i in top]

    def reset(self):
        self._vecs.clear(); self._labels.clear()

    @property
    def size(self) -> int:
        return len(self._labels)


def _build_index(dim: int = 512):
    try:
        import faiss
        idx = faiss.IndexFlatIP(dim)
        class _FW:
            def __init__(self, i):
                self._i = i; self._l: List[str] = []
            def add(self, v, l):
                self._i.add(np.ascontiguousarray(v.astype(np.float32))); self._l.extend(l)
            def search(self, q, k=5):
                if self._i.ntotal == 0: return []
                qq = np.ascontiguousarray(q[np.newaxis].astype(np.float32))
                s, ix = self._i.search(qq, min(k, self._i.ntotal))
                return [(self._l[i], float(sc)) for i, sc in zip(ix[0], s[0]) if i >= 0]
            def reset(self): self._i.reset(); self._l.clear()
            @property
            def size(self): return len(self._l)
        return _FW(idx)
    except ImportError:
        return _NumpyIndex()


# ---------------------------------------------------------------------------
# EmbeddingStore
# ---------------------------------------------------------------------------

DATA_DIR = Path("Data")
FACE_DB  = DATA_DIR / "face_db.json"


class EmbeddingStore:
    """Face database with APBC per-person threshold calibration."""

    def __init__(self):
        DATA_DIR.mkdir(exist_ok=True)
        self._backend    = _build_backend()
        self._index      = _build_index()
        self._db: Dict   = {"schema_version": 3, "persons": {}}
        self._thresholds: Dict[str, float] = {}
        self._load()

    def _load(self):
        if FACE_DB.exists():
            try:
                with open(FACE_DB) as f:
                    self._db = json.load(f)
                self._rebuild_index()
                logger.info(f"Loaded {len(self._db['persons'])} person(s)")
            except Exception as e:
                logger.error(f"DB load failed: {e}")

    def _save(self):
        tmp = FACE_DB.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(self._db, f, indent=2)
        tmp.replace(FACE_DB)

    def _rebuild_index(self):
        self._index.reset()
        self._thresholds.clear()
        for name, data in self._db["persons"].items():
            vecs = np.array(data["embeddings"], dtype=np.float32)
            self._index.add(vecs, [name] * len(vecs))
            self._thresholds[name] = float(data.get("threshold", 0.40))

    def enroll(self, name: str, face_samples: List[np.ndarray],
               yaw_angles: Optional[List[float]] = None) -> bool:
        """Enroll with APBC calibration. Pass yaw_angles from Add_faces.py."""
        if name in self._db["persons"]:
            logger.warning(f"'{name}' already enrolled")
            return False

        yaw_angles = yaw_angles or [0.0] * len(face_samples)

        per_sample_embs = []
        for face in face_samples:
            variants = _augment(face)
            embs     = [self._backend.embed(v) for v in variants]
            mean_e   = np.mean(embs, axis=0).astype(np.float32)
            mean_e  /= (np.linalg.norm(mean_e) + 1e-10)
            per_sample_embs.append(mean_e)

        if self._index.size >= 5:
            probe  = np.mean(per_sample_embs, axis=0).astype(np.float32)
            probe /= np.linalg.norm(probe) + 1e-10
            hits   = self._index.search(probe, k=3)
            if hits and hits[0][1] > 0.72:
                logger.warning(f"Anti-clone: matches '{hits[0][0]}' ({hits[0][1]:.3f})")
                return False

        emb_matrix = np.array(per_sample_embs, dtype=np.float32)
        threshold, quality = compute_apbc_threshold(face_samples, yaw_angles, emb_matrix)

        self._db["persons"][name] = {
            "embeddings":   emb_matrix.tolist(),
            "threshold":    float(threshold),
            "enrolled_at":  datetime.datetime.now().isoformat(),
            "sample_count": len(face_samples),
            "yaw_angles":   [round(y, 2) for y in yaw_angles],
            "apbc_quality": quality,
        }
        self._save()
        self._rebuild_index()
        logger.info(f"Enrolled '{name}' with APBC threshold={threshold:.4f}")
        return True

    def recognize(self, face_rgb: np.ndarray,
                  top_k: int = 3) -> Tuple[Optional[str], float]:
        if self._index.size == 0:
            return None, 0.0
        variants = _augment(face_rgb)
        embs     = [self._backend.embed(v) for v in variants]
        query    = np.mean(embs, axis=0).astype(np.float32)
        query   /= (np.linalg.norm(query) + 1e-10)
        hits = self._index.search(query, k=min(top_k * 3, self._index.size))
        if not hits:
            return None, 0.0
        vote_scores: Dict[str, List[float]] = {}
        for name, sim in hits:
            vote_scores.setdefault(name, []).append(sim)
        best_name, best_sim = None, -1.0
        for name, scores in vote_scores.items():
            avg_sim = float(np.mean(scores))
            thr     = self._thresholds.get(name, 0.40)
            if avg_sim > thr and avg_sim > best_sim:
                best_name, best_sim = name, avg_sim
        if best_name is None:
            return None, 0.0
        thr  = self._thresholds.get(best_name, 0.40)
        conf = float(np.clip((best_sim - thr) / (1.0 - thr + 1e-9), 0.0, 1.0))
        return best_name, conf

    def get_apbc_report(self, name: str) -> Optional[dict]:
        p = self._db["persons"].get(name)
        return p.get("apbc_quality") if p else None

    def list_persons(self) -> List[str]:
        return list(self._db["persons"].keys())

    def delete_person(self, name: str) -> bool:
        if name not in self._db["persons"]: return False
        del self._db["persons"][name]
        self._save(); self._rebuild_index()
        return True

    def person_count(self) -> int:
        return len(self._db["persons"])
