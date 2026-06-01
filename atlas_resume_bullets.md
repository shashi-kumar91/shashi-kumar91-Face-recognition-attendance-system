# ATLAS — Resume Bullet Point Reference

> Pick and adapt bullets from each section below for your resume.
> Each bullet is written in **action verb + quantified result** format for ATS optimization.

---

## 🔹 System-Level (High-Impact Summary Bullets)

- Engineered an end-to-end face recognition attendance system with 5-cue anti-spoofing fusion, achieving 95%+ recognition accuracy and 100% printed-photo rejection on consumer-grade hardware (i5 CPU, no GPU)
- Designed and deployed a real-time biometric attendance pipeline processing 720p webcam frames at ~18 FPS with <55 ms per-frame latency using ONNX Runtime, MediaPipe, and OpenCV
- Built a production-grade desktop application (Tkinter) with threaded recognition, admin authentication, audit logging, and automated CSV/Excel attendance export
- Implemented 4 original algorithmic contributions (MLVS, PACE, APBC, TACE) addressing documented gaps in open-source RGB liveness and attendance systems

---

## 🔹 Face Detection & Alignment (detector.py)

- Built a face detection pipeline using MediaPipe Tasks API (FaceLandmarker, 478 landmarks) with automatic fallback to BlazeFace (6 keypoints) for robustness
- Implemented similarity-transform face alignment using 5-point landmarks, producing normalized 160×160 crops for downstream recognition and liveness
- Designed a composite quality scoring function (blur 40%, brightness 30%, pose 30%) to gate low-quality frames before expensive biometric processing
- Engineered stale-frame draining logic to discard 3 buffered webcam frames, ensuring liveness checks operate on real-time data (critical for motion-based anti-spoofing)
- Applied CLAHE (Contrast Limited Adaptive Histogram Equalization) preprocessing to normalize illumination across varying lighting conditions

---

## 🔹 Anti-Spoofing / Liveness Detection (liveness.py — 1,100+ lines)

- Architected a multi-cue liveness fusion engine combining CNN texture analysis, landmark dynamics, blood flow estimation, optical flow, and active challenges into a single True/False/None decision
- Deployed a MiniFASNet CNN ensemble (2 ONNX models at scale factors 2.7× and 4.0×) with proper scale-expanded cropping from the original frame, achieving 100% printed-photo and >95% screen-replay rejection
- Fixed critical CNN preprocessing bugs (RGB→BGR conversion, /255 normalization without ImageNet mean subtraction, live class = index 1) that previously caused 0.01 scores on real faces
- Implemented 8-frame temporal smoothing with geometric mean aggregation to stabilize CNN predictions and prevent single-frame false rejects
- Built an LBP + FFT rule-based fallback detector for environments where CNN models are unavailable, maintaining baseline anti-spoofing capability
- Integrated ISO/IEC 29794-1:2024 face image quality metrics (sharpness, brightness, contrast, face size, illumination entropy) as a preprocessing gate for liveness checks

### ★ MLVS — Micro-Landmark Velocity Signature (Novel)

- Invented MLVS: a novel anti-spoofing signal that tracks 10 anatomically stable facial landmarks across 16 frames, computes per-frame velocity magnitudes, and analyzes FFT bio-motion band energy (0.5–15 Hz)
- Achieved clear separation between live faces (bio_ratio > 0.08), static prints (≈0.00), and screen replays (0.05–0.15 with single-frequency peak dominance), at <3 ms computational cost
- Implemented Hann windowing and peak-dominance penalization (>0.7) to detect display refresh artifacts aliased into webcam capture

### ★ PACE — Progressive Adaptive Challenge Escalation (Novel)

- Designed PACE: a graduated active-liveness challenge system that scales verification demand to CNN confidence — instant pass for high-confidence faces, blink challenge for moderate, blink + head movement for uncertain, hard reject for clear spoofs
- Implemented pixel-space Eye Aspect Ratio (EAR) blink detection with adaptive reference thresholds (75% closed / 88% open relative to personal baseline), fixing prior normalized-EAR bug that prevented blink detection
- Built randomized head-turn/nod challenges with relaxed angular thresholds (6–8°) and generous timeout windows (28–35s) to minimize false rejections on real users

### rPPG — Remote Photoplethysmography

- Implemented green-channel variance analysis from forehead/cheek ROI to detect blood flow periodicity (real face variance > 0.025 vs. photo < 0.005)

### Motion Guard — Optical Flow

- Built a Farneback optical flow motion detector (160×120 downscaled) to catch 100% static images with a relaxed threshold of 0.04

---

## 🔹 Face Recognition & Enrollment (embedder.py)

- Deployed ArcFace (w600k_r50, 512-d embeddings) via ONNX Runtime with DirectML acceleration on Intel Iris Xe, achieving robust identity matching across lighting and pose variations
- Implemented 4× data augmentation at inference time (original, horizontal flip, brightness ±18) with mean-pooled embeddings to improve recognition stability
- Built a FAISS-based (IndexFlatIP) vector similarity search with vote-based aggregation across top-K matches for robust multi-frame recognition
- Designed an anti-clone guard that rejects new enrollments if probe similarity > 0.72 with any existing person, preventing duplicate identity registration

### ★ APBC — Adaptive Per-Person Biometric Calibration (Novel)

- Invented APBC: computes a person-specific recognition threshold at enrollment using 3 quality metrics — illumination entropy, pose spread (yaw σ), and embedding compactness (mean pairwise cosine similarity)
- Eliminated the "one global threshold" problem: well-enrolled users (bright light, diverse poses) get tight thresholds (~0.28), poorly-enrolled users (dim, frontal-only) get lenient thresholds (~0.55), equalizing false-reject rates across users

---

## 🔹 Enrollment Pipeline (Add_faces.py)

- Built a 10-sample enrollment pipeline with pose diversity enforcement (cosine similarity < 0.94 between pose vectors), liveness gating, and rate-limited capture (0.7s intervals)
- Integrated enrollment-specific liveness checker (relaxed thresholds, MLVS + motion guard, ISO quality checks) to ensure only genuine face samples enter the database
- Fixed a critical indentation bug where the face capture loop body was dedented to the same level as the while loop, causing zero samples to be collected during recording

---

## 🔹 Temporal Anomaly Detection (anomaly.py)

### ★ TACE — Temporal Attendance Coherence Engine (Novel)

- Designed TACE: a statistical anomaly detection engine that builds per-person behavioral fingerprints from historical attendance and flags 4 types of anomalies in <1 ms (pure pandas/numpy)
- Implemented proxy attendance detection using confidence Z-scores (flag if >2.5σ below personal historical mean)
- Built session replay detection by flagging same-person recognition within 90 seconds with suspiciously identical confidence scores (Δ < 0.02)
- Developed arrival window anomaly detection using per-person time-of-day distributions (flag if >2σ + 1.5h outside normal attendance window)
- Implemented confidence drift monitoring via linear regression slope over 10 sessions, alerting admins to potential face-DB contamination or model degradation

---

## 🔹 Application & Security (int1.py)

- Built a multi-threaded Tkinter desktop application with daemon-thread recognition pipeline keeping UI responsive during real-time camera processing
- Implemented admin-authenticated enrollment, user deletion, and data clearing with password-protected access control
- Designed a 60-second lockout mechanism after 3 consecutive failed attempts, with full event logging to JSONL audit trail
- Built duplicate attendance prevention with a configurable 60-minute deduplication window using timestamp comparison
- Implemented vote-based recognition requiring 5 consecutive confident+live frames with ≥60% name agreement before accepting identity
- Added real-time HUD overlay displaying CNN scores, PACE status, blink state, MLVS signal, and static-scene detection for debugging
- Integrated pyttsx3 text-to-speech for audible attendance confirmation feedback

---

## 🔹 Infrastructure & DevOps

- Configured ONNX Runtime with DirectML execution provider for GPU-accelerated inference on Intel Iris Xe iGPU when available, with automatic CPU fallback
- Optimized webcam capture for Windows DirectShow (MJPG codec, buffer size 1, 720p/30fps target) to minimize CPU overhead and frame latency
- Implemented atomic JSON database writes (write-to-tmp + rename) to prevent data corruption on crash
- Built a pre-flight validation script (`validate_fixes.py`) checking library availability, model file integrity, code fix application, and directory structure

---

## 🔹 Tech Stack (for Skills Section)

**Core:** Python, OpenCV, MediaPipe, ONNX Runtime, NumPy, Pandas
**AI/ML:** ArcFace (InsightFace), MiniFASNet (Silent-Face-Anti-Spoofing), FaceNet (fallback), FAISS
**Techniques:** CNN Ensemble, FFT Signal Analysis, Optical Flow, rPPG, Similarity Transform, CLAHE, LBP
**Application:** Tkinter, Threading, pyttsx3, JSONL Audit Logging
**Hardware Optimization:** DirectML, Intel Iris Xe, ONNX Graph Optimization
