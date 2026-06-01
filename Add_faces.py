"""
Add_faces.py — Enrollment Pipeline
------------------------------------
FIXES vs original:
  [1] CRITICAL: 'for det in detections:' block was dedented to the same
      level as the 'while' loop, so it ran ONCE after while exited — not
      once per frame. The face capture body (liveness, pose, samples.append)
      never executed during recording. Fixed by adding correct indentation.
"""

import cv2
import sys
import os
import time
import logging
import numpy as np
from pathlib import Path

from detector  import FaceDetector, configure_webcam, read_webcam_fresh
from liveness  import EnrollmentLiveness  # Updated: Use enrollment-specific version
from embedder  import EmbeddingStore

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

def speak(text: str):
    try:
        import pyttsx3
        eng = pyttsx3.init()
        eng.say(text)
        eng.runAndWait()
    except Exception:
        print(f"[TTS] {text}")

TARGET_SAMPLES       = 10       # was 15 — 10 is enough for robust enrollment
QUALITY_THRESHOLD    = 0.25
ENROLLMENT_LIVENESS_THRESH = 0.20
MAX_POSE_SIM         = 0.94
CAPTURE_INTERVAL_SEC = 0.7      # was 1.0 — faster capture

def _pose_vector(yaw: float, pitch: float) -> np.ndarray:
    v = np.array([yaw, pitch], dtype=np.float32)
    n = np.linalg.norm(v)
    return v / (n + 1e-6)

def _is_pose_diverse(yaw: float, pitch: float, existing_poses) -> bool:
    if not existing_poses:
        return True
    new_v = _pose_vector(yaw, pitch)
    for ey, ep in existing_poses:
        old_v = _pose_vector(ey, ep)
        if float(np.dot(new_v, old_v)) > MAX_POSE_SIM:
            return False
    return True

def enroll(name: str) -> bool:
    try:
        store = EmbeddingStore()

        if name in store.list_persons():
            logger.error(f"User '{name}' already enrolled.")
            print(f"ERROR: User '{name}' already enrolled.")
            return False

        detector = FaceDetector(quality_threshold=QUALITY_THRESHOLD)
        liveness = EnrollmentLiveness()  # Updated: Use enrollment-specific liveness

        video = cv2.VideoCapture(0)
        if not video.isOpened():
            video = cv2.VideoCapture(1)
        if not video.isOpened():
            logger.error("Could not open any camera.")
            print("ERROR: Could not open camera (index 0 or 1)")
            return False

        configure_webcam(video)

        for _ in range(5):
            video.read()

        samples: list       = []
        pose_log: list      = []
        last_capture: float = 0.0

        speak(f"Starting enrollment for {name}. Please vary your head pose slowly.")
        print("\n" + "="*60)
        print(f" ENROLLING: {name}")
        print(f" Target: {TARGET_SAMPLES} diverse samples")
        print(f" Tips: tilt left/right, look up/down, vary lighting")
        print("="*60 + "\n")

        try:
            while len(samples) < TARGET_SAMPLES:
                ret, frame = read_webcam_fresh(video)
                if not ret or frame is None:
                    logger.warning("Camera read failed")
                    continue

                detections = detector.detect(frame, enhance=True)

                # FIX [1]: this for-loop was at the SAME indent as while,
                # running only once AFTER the loop exited. Now correctly inside.
                for det in detections:
                    # ── enrollment liveness check (with proper scale crops) ──────
                    gray_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                    live_score = liveness.score(
                        det.aligned_face,
                        face_landmarks=det.face_landmarks,
                        gray_full=gray_frame,
                        frame_bgr=frame,
                        bbox=det.bbox)
                    
                    if live_score < ENROLLMENT_LIVENESS_THRESH:
                        cv2.putText(frame, f"Liveness check: {live_score:.2f} (need {ENROLLMENT_LIVENESS_THRESH})",
                                    (30, 50), cv2.FONT_HERSHEY_SIMPLEX,
                                    0.6, (0, 165, 255), 2)
                        continue

                    # ── pose diversity check ────────────────────────────
                    if not _is_pose_diverse(det.yaw, det.pitch, pose_log):
                        cv2.putText(frame, "Similar pose - adjust head",
                                    (30, 80), cv2.FONT_HERSHEY_SIMPLEX,
                                    0.6, (0, 165, 255), 2)
                        continue

                    # ── rate limiting ───────────────────────────────────
                    now = time.monotonic()
                    if now - last_capture < CAPTURE_INTERVAL_SEC:
                        continue

                    # ── accept sample ───────────────────────────────────
                    samples.append(det.aligned_face.copy())
                    pose_log.append((det.yaw, det.pitch))
                    last_capture = now

                    n = len(samples)
                    speak(f"Sample {n}")
                    print(f"  Sample {n:2d}/{TARGET_SAMPLES}  "
                          f"quality={det.quality_score:.2f}  "
                          f"yaw={det.yaw:+.1f} pitch={det.pitch:+.1f}  "
                          f"liveness={live_score:.2f}")

                    cv2.putText(frame, f"Sample {n}/{TARGET_SAMPLES}",
                                (30, 50), cv2.FONT_HERSHEY_SIMPLEX,
                                1.0, (0, 255, 0), 2)
                    x, y, w, h = det.bbox
                    cv2.rectangle(frame, (x, y), (x+w, y+h), (0, 255, 0), 2)

                # ── quality overlay (outside for, inside while) ─────────
                if detections:
                    d = detections[0]
                    cv2.putText(frame,
                                f"Q={d.quality_score:.2f} Yaw={d.yaw:+.0f}",
                                (30, frame.shape[0] - 20),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                                (255, 255, 255), 1)

                cv2.imshow("Enrollment", frame)
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    print("Enrollment cancelled by user.")
                    return False

        finally:
            video.release()
            cv2.destroyAllWindows()
            detector.close()

        if len(samples) < TARGET_SAMPLES:
            logger.error(f"Only {len(samples)} samples — enrollment aborted.")
            print(f"ERROR: Only {len(samples)}/{TARGET_SAMPLES} samples collected")
            return False

        print(f"\nProcessing {len(samples)} samples...")
        success = store.enroll(name, samples)

        if success:
            speak(f"{name} enrolled successfully.")
            print(f"  {name} enrolled. Total persons in DB: {store.person_count()}")
        else:
            print("  Enrollment failed (possible duplicate face).")

        return success

    except Exception as e:
        logger.error(f"Enrollment error: {e}", exc_info=True)
        print()
        print("=" * 60)
        print(f"❌ ENROLLMENT ERROR: {e}")
        print("=" * 60)
        import traceback
        print("Full traceback:")
        traceback.print_exc()
        print("=" * 60)
        return False


if __name__ == "__main__":
    if len(sys.argv) > 1:
        name = sys.argv[1].strip()
    else:
        name = input("Enter name to enroll: ").strip()

    if not name:
        print("Error: name cannot be empty.")
        sys.exit(1)

    sys.exit(0 if enroll(name) else 1)
