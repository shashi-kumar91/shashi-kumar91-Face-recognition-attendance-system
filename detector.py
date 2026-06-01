"""
detector.py — Advanced Face Detection & Alignment Pipeline
----------------------------------------------------------
Uses: MediaPipe Tasks API (FaceLandmarker + FaceDetector)
      Replaces legacy mp.solutions which was removed in mediapipe>=0.10.30

Compatible with: Python 3.13, mediapipe>=0.10.30, numpy 2.x
Hardware target: HP Pavilion i5-1240P, 16GB RAM, Intel Iris Xe
"""

import cv2
import numpy as np
import sys
import logging
from dataclasses import dataclass, field
from typing import Optional, List, Tuple
from pathlib import Path

logger = logging.getLogger(__name__)

_MODELS_DIR = Path("models")
_LANDMARKER_MODEL = _MODELS_DIR / "face_landmarker.task"
_DETECTOR_MODEL = _MODELS_DIR / "blaze_face_short_range.tflite"

# ---------------------------------------------------------------------------
# Data container
# ---------------------------------------------------------------------------

@dataclass
class FaceDetection:
    """Carries everything downstream needs about one detected face."""
    aligned_face:   np.ndarray              # (160,160,3) RGB, aligned
    bbox:           Tuple[int,int,int,int]   # x,y,w,h in original frame
    landmarks:      np.ndarray              # (5,2) 5-point keypoints
    quality_score:  float                   # 0.0–1.0
    yaw:            float                   # degrees
    pitch:          float                   # degrees
    face_landmarks: object = field(default=None)  # landmarks list for blink/MLVS

# ---------------------------------------------------------------------------
# Quality helpers
# ---------------------------------------------------------------------------

def _laplacian_blur_score(gray: np.ndarray) -> float:
    score = cv2.Laplacian(gray, cv2.CV_64F).var()
    return float(min(score / 150.0, 1.0))

def _brightness_score(gray: np.ndarray) -> float:
    mean = float(gray.mean())
    if mean < 60:
        return mean / 60.0
    if mean > 220:
        return max(0.0, 1.0 - (mean - 220) / 35.0)
    return 1.0

def _pose_score(yaw: float, pitch: float) -> float:
    yaw_ok   = max(0.0, 1.0 - abs(yaw)   / 35.0)
    pitch_ok = max(0.0, 1.0 - abs(pitch) / 25.0)
    return (yaw_ok + pitch_ok) / 2.0

def composite_quality(gray: np.ndarray, yaw: float, pitch: float) -> float:
    return 0.4 * _laplacian_blur_score(gray) \
         + 0.3 * _brightness_score(gray) \
         + 0.3 * _pose_score(yaw, pitch)

# ---------------------------------------------------------------------------
# Landmark-based similarity-transform alignment
# ---------------------------------------------------------------------------

_REF_LANDMARKS_160 = np.array([
    [38.29, 51.69],
    [73.53, 51.50],
    [56.02, 71.74],
    [41.55, 92.37],
    [70.73, 92.20],
], dtype=np.float32)

def _estimate_yaw_pitch(lm5: np.ndarray) -> Tuple[float, float]:
    le, re, nose = lm5[0], lm5[1], lm5[2]
    eye_center = (le + re) / 2.0
    eye_width  = float(np.linalg.norm(re - le))
    if eye_width < 1e-6:
        return 0.0, 0.0
    yaw   = float(np.degrees(np.arctan2(nose[0] - eye_center[0], eye_width)) * 2)
    pitch = float(np.degrees(np.arctan2(nose[1] - eye_center[1], eye_width)) - 35)
    return yaw, pitch

def align_face(frame: np.ndarray, lm5: np.ndarray,
               output_size: int = 160) -> np.ndarray:
    scale = output_size / 160.0
    ref   = _REF_LANDMARKS_160 * scale
    M, _  = cv2.estimateAffinePartial2D(lm5, ref, method=cv2.LMEDS)
    if M is None:
        return cv2.resize(frame, (output_size, output_size))
    return cv2.warpAffine(frame, M, (output_size, output_size),
                          flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_REPLICATE)

# ---------------------------------------------------------------------------
# CLAHE pre-processing
# ---------------------------------------------------------------------------

_clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))

def enhance_frame(bgr: np.ndarray) -> np.ndarray:
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    lab[..., 0] = _clahe.apply(lab[..., 0])
    return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)


def configure_webcam(capture: cv2.VideoCapture) -> None:
    """
    Best-effort settings for built-in laptop cameras.
    Tuned for 720p HP Pavilion + Iris Xe: MJPEG lowers CPU, buffer 1 reduces stale frames.
    """
    if capture is None or not capture.isOpened():
        return
    try:
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            capture.set(cv2.CAP_PROP_FOURCC,
                          cv2.VideoWriter_fourcc(*"MJPG"))
        except Exception:
            pass
    try:
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        capture.set(cv2.CAP_PROP_FPS, 30)
    except Exception:
        pass


def read_webcam_fresh(capture: cv2.VideoCapture) -> Tuple[bool, Optional[np.ndarray]]:
    """
    Return the most recent frame: drain stale buffered frames first.
    On Windows DirectShow, the buffer often holds 2-3 frames; draining 3
    ensures liveness checks and MLVS see real-time motion.
    """
    if capture is None or not capture.isOpened():
        return False, None
    # Drain up to 3 stale frames from the buffer
    for _ in range(3):
        capture.grab()
    ret, frame = capture.retrieve()
    if ret and frame is not None:
        return True, frame
    return capture.read()


# ---------------------------------------------------------------------------
# Landmark wrapper — compatible adapter for blink/MLVS detection
# ---------------------------------------------------------------------------

class _LandmarkListAdapter:
    """
    Adapts MediaPipe Tasks FaceLandmarkerResult landmarks to the legacy
    NormalizedLandmarkList interface that liveness.py expects.
    Provides .landmark attribute as a list of objects with .x, .y, .z
    """
    def __init__(self, landmarks_list):
        self.landmark = landmarks_list


# ---------------------------------------------------------------------------
# Main detector class — MediaPipe Tasks API
# ---------------------------------------------------------------------------

class FaceDetector:
    """
    MediaPipe Tasks-based detector with 5-point landmark alignment and quality gating.
    Uses FaceLandmarker (478 landmarks) as primary, FaceDetector as fallback.
    """

    def __init__(self,
                 quality_threshold: float = 0.30,
                 min_detection_confidence: float = 0.6):
        self.quality_threshold = quality_threshold
        self._min_detection_confidence = min_detection_confidence

        # Lazy init flags
        self._landmarker = None
        self._fd_fallback = None
        self._landmarker_available = False
        self._fd_available = False
        self._init_attempted = False

    def _init_models(self):
        """Lazy initialization of MediaPipe Tasks models."""
        if self._init_attempted:
            return
        self._init_attempted = True

        import mediapipe as mp
        from mediapipe.tasks import python
        from mediapipe.tasks.python import vision

        # Primary: FaceLandmarker (gives 478 landmarks for blink/MLVS)
        if _LANDMARKER_MODEL.exists():
            try:
                base_opts = python.BaseOptions(
                    model_asset_path=str(_LANDMARKER_MODEL))
                opts = vision.FaceLandmarkerOptions(
                    base_options=base_opts,
                    running_mode=vision.RunningMode.IMAGE,
                    num_faces=4,
                    min_face_detection_confidence=self._min_detection_confidence,
                    min_face_presence_confidence=0.5,
                    min_tracking_confidence=0.5)
                self._landmarker = vision.FaceLandmarker.create_from_options(opts)
                self._landmarker_available = True
                logger.info("FaceLandmarker (Tasks API) initialized")
            except Exception as e:
                logger.warning(f"FaceLandmarker init failed: {e}")

        # Fallback: FaceDetector (6 keypoints, no mesh)
        if _DETECTOR_MODEL.exists():
            try:
                base_opts = python.BaseOptions(
                    model_asset_path=str(_DETECTOR_MODEL))
                opts = vision.FaceDetectorOptions(
                    base_options=base_opts,
                    running_mode=vision.RunningMode.IMAGE,
                    min_detection_confidence=self._min_detection_confidence - 0.1)
                self._fd_fallback = vision.FaceDetector.create_from_options(opts)
                self._fd_available = True
                logger.info("FaceDetector (Tasks API) initialized as fallback")
            except Exception as e:
                logger.warning(f"FaceDetector fallback init failed: {e}")

    def detect(self, bgr_frame: np.ndarray,
               enhance: bool = True) -> List[FaceDetection]:
        """Returns list of FaceDetection objects (may be empty)."""
        if not self._init_attempted:
            self._init_models()

        import mediapipe as mp

        proc = enhance_frame(bgr_frame) if enhance else bgr_frame
        rgb  = cv2.cvtColor(proc, cv2.COLOR_BGR2RGB)
        h, w = rgb.shape[:2]

        results: List[FaceDetection] = []

        # ── Primary: FaceLandmarker — gives full landmarks for blink EAR ──
        if self._landmarker_available and self._landmarker is not None:
            try:
                mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                lm_result = self._landmarker.detect(mp_image)

                if lm_result.face_landmarks:
                    for face_lms in lm_result.face_landmarks:
                        # 5-point keypoints: left eye outer, right eye outer,
                        # nose tip, left mouth, right mouth
                        KEY_IDX = [33, 362, 1, 61, 291]
                        lm5 = np.array([[face_lms[i].x * w, face_lms[i].y * h]
                                        for i in KEY_IDX], dtype=np.float32)

                        xs = [l.x * w for l in face_lms]
                        ys = [l.y * h for l in face_lms]
                        x1 = max(int(min(xs)) - 10, 0)
                        y1 = max(int(min(ys)) - 10, 0)
                        x2 = min(int(max(xs)) + 10, w)
                        y2 = min(int(max(ys)) + 10, h)
                        if bgr_frame[y1:y2, x1:x2].size == 0:
                            continue

                        yaw, pitch = _estimate_yaw_pitch(lm5)
                        aligned    = align_face(bgr_frame, lm5, output_size=160)
                        gray_crop  = cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY)
                        quality    = composite_quality(gray_crop, yaw, pitch)

                        if quality < self.quality_threshold:
                            logger.debug(f"Frame rejected: quality={quality:.2f}")
                            continue

                        # Wrap landmarks for blink/MLVS compatibility
                        face_lm_adapter = _LandmarkListAdapter(face_lms)

                        results.append(FaceDetection(
                            aligned_face=cv2.cvtColor(aligned, cv2.COLOR_BGR2RGB),
                            bbox=(x1, y1, x2 - x1, y2 - y1),
                            landmarks=lm5,
                            quality_score=quality,
                            yaw=yaw,
                            pitch=pitch,
                            face_landmarks=face_lm_adapter,
                        ))
            except Exception as e:
                logger.warning(f"FaceLandmarker processing failed: {e}")
                self._landmarker_available = False
                results = []

        # ── Fallback: FaceDetector (no mesh, face_landmarks=None) ──────
        if not results and self._fd_available and self._fd_fallback is not None:
            try:
                mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                fd_result = self._fd_fallback.detect(mp_image)

                if fd_result.detections:
                    for det in fd_result.detections:
                        bb = det.bounding_box
                        x1 = max(bb.origin_x - 5, 0)
                        y1 = max(bb.origin_y - 5, 0)
                        x2 = min(bb.origin_x + bb.width + 5, w)
                        y2 = min(bb.origin_y + bb.height + 5, h)
                        if bgr_frame[y1:y2, x1:x2].size == 0:
                            continue

                        # Use keypoints for 5-point alignment
                        kps = det.keypoints
                        if kps and len(kps) >= 5:
                            lm5 = np.array([
                                [kps[0].x * w, kps[0].y * h],
                                [kps[1].x * w, kps[1].y * h],
                                [kps[2].x * w, kps[2].y * h],
                                [kps[3].x * w, kps[3].y * h],
                                [kps[4].x * w, kps[4].y * h],
                            ], dtype=np.float32)
                        else:
                            # Estimate from bbox center
                            cx = (x1 + x2) / 2
                            cy = (y1 + y2) / 2
                            bw = x2 - x1
                            lm5 = np.array([
                                [cx - bw*0.2, cy - bw*0.1],
                                [cx + bw*0.2, cy - bw*0.1],
                                [cx, cy + bw*0.05],
                                [cx - bw*0.15, cy + bw*0.2],
                                [cx + bw*0.15, cy + bw*0.2],
                            ], dtype=np.float32)

                        yaw, pitch = _estimate_yaw_pitch(lm5)
                        aligned    = align_face(bgr_frame, lm5, output_size=160)
                        gray_crop  = cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY)
                        quality    = composite_quality(gray_crop, yaw, pitch)

                        if quality < self.quality_threshold:
                            continue

                        results.append(FaceDetection(
                            aligned_face=cv2.cvtColor(aligned, cv2.COLOR_BGR2RGB),
                            bbox=(x1, y1, x2 - x1, y2 - y1),
                            landmarks=lm5,
                            quality_score=quality,
                            yaw=yaw,
                            pitch=pitch,
                            face_landmarks=None,
                        ))
                    # Take first batch of results
            except Exception as e:
                logger.warning(f"FaceDetector fallback failed: {e}")

        return results

    def close(self):
        if self._landmarker is not None:
            try:
                self._landmarker.close()
            except Exception as e:
                logger.warning(f"Error closing FaceLandmarker: {e}")
        if self._fd_fallback is not None:
            try:
                self._fd_fallback.close()
            except Exception as e:
                logger.warning(f"Error closing FaceDetector: {e}")
