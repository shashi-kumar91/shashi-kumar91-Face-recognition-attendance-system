#!/usr/bin/env python3
"""
VALIDATION SCRIPT: Verify Spoof Detection Fixes
================================================

This script validates that all fixes are correctly applied.
Run this BEFORE enrolling to ensure system is working.

Usage:
    python validate_fixes.py

Expected Output:
    ✅ All checks passed - System ready for enrollment
"""

import sys
from pathlib import Path

def check_library(name: str, import_statement: str) -> bool:
    """Check if a library is installed."""
    try:
        exec(import_statement)
        print(f"[OK] {name:<20} OK")
        return True
    except ImportError as e:
        print(f"[!!] {name:<20} FAILED: {e}")
        return False

def check_model_file(path: Path, name: str) -> bool:
    """Check if a model file exists."""
    if path.exists():
        size_mb = path.stat().st_size / (1024 * 1024)
        print(f"[OK] {name:<30} OK ({size_mb:.1f} MB)")
        return True
    else:
        print(f"[!!] {name:<30} NOT FOUND")
        return False

def check_code_fix(filepath: Path, search_string: str, name: str) -> bool:
    """Check if a code fix is applied."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
            if search_string in content:
                print(f"[OK] {name:<40} APPLIED")
                return True
            else:
                print(f"[!!] {name:<40} NOT APPLIED")
                return False
    except Exception as e:
        print(f"[!!] {name:<40} ERROR: {e}")
        return False

def main():
    print("=" * 70)
    print("ATLAS SPOOF DETECTION FIX VALIDATION")
    print("=" * 70)
    print()

    all_ok = True

    # ─────────────────────────────────────────────────────────────────
    print("1. REQUIRED LIBRARIES")
    print("-" * 70)
    libs = [
        ("numpy", "import numpy as np"),
        ("cv2 (OpenCV)", "import cv2"),
        ("mediapipe", "import mediapipe as mp"),
        ("onnxruntime", "import onnxruntime as ort"),
    ]
    for name, stmt in libs:
        if not check_library(name, stmt):
            all_ok = False
    print()

    # ─────────────────────────────────────────────────────────────────
    print("2. MODEL FILES")
    print("-" * 70)
    models_dir = Path("models")
    model_files = [
        (models_dir / "2.7_80x80_MiniFASNetV2.onnx", "MiniFASNet V2"),
        (models_dir / "4_0_0_80x80_MiniFASNetV1SE.onnx", "MiniFASNet V1SE"),
        (models_dir / "w600k_r50.onnx", "ArcFace w600k_r50"),
    ]
    for path, name in model_files:
        if not check_model_file(path, name):
            all_ok = False
    print()

    # ─────────────────────────────────────────────────────────────────
    print("3. CODE FIXES APPLIED")
    print("-" * 70)

    liveness_py = Path("liveness.py")
    add_faces_py = Path("Add_faces.py")

    fixes = [
        (liveness_py, "def _crop_face_for_fas(",
         "CropImage scale-crop function"),
        
        (liveness_py, "_MODEL_SCALES = [2.7, 4.0]",
         "Model scale factors (2.7 / 4.0)"),
        
        (liveness_py, "def update_with_bbox(self",
         "CNN bbox-based update method"),
        
        (liveness_py, "class EnrollmentLiveness:",
         "EnrollmentLiveness Class"),
        
        (liveness_py, "frame_bgr: np.ndarray = None",
         "LivenessChecker accepts frame_bgr"),
        
        (add_faces_py, "from liveness  import EnrollmentLiveness",
         "Use EnrollmentLiveness"),
        
        (add_faces_py, "frame_bgr=frame",
         "Pass original frame for scale crops"),
        
        (add_faces_py, "bbox=det.bbox",
         "Pass bbox for scale crops"),
        
        (add_faces_py, "ENROLLMENT_LIVENESS_THRESH = 0.20",
         "Relaxed enrollment threshold"),
    ]

    for filepath, code_string, description in fixes:
        if not check_code_fix(filepath, code_string, description):
            all_ok = False
    print()

    # ─────────────────────────────────────────────────────────────────
    print("4. MEDIAPIPE TASKS MODELS")
    print("-" * 70)
    task_models = [
        (Path("models/face_landmarker.task"), "FaceLandmarker model"),
        (Path("models/blaze_face_short_range.tflite"), "BlazeFace detector"),
    ]
    for path, name in task_models:
        if path.exists():
            size_kb = path.stat().st_size / 1024
            print(f"[OK] {name:<40} OK ({size_kb:.1f} KB)")
        else:
            print(f"[!!] {name:<40} NOT FOUND")
            all_ok = False
    print()

    # ─────────────────────────────────────────────────────────────────
    print("5. DATA DIRECTORIES")
    print("-" * 70)
    dirs_needed = [
        (Path("Data"), "Data directory"),
        (Path("Attendance"), "Attendance directory"),
        (Path("models"), "Models directory"),
    ]
    for path, name in dirs_needed:
        if path.exists():
            print(f"[OK] {name:<40} OK")
        else:
            print(f"[--] {name:<40} WILL BE CREATED")
    print()

    # ─────────────────────────────────────────────────────────────────
    print("=" * 70)
    if all_ok:
        print("[OK] ALL CHECKS PASSED - System is ready for testing!")
        print()
        print("NEXT STEPS:")
        print("  1. Run:  python int1.py")
        print("  2. Click 'Enroll New User'")
        print("  3. Enter your name")
        print("  4. Move your head slowly (for MLVS)")
        print("  5. System should collect 15 samples")
        print("  6. Enrollment complete!")
        print()
        return 0
    else:
        print("[!!] Some checks failed - See above for details")
        print()
        print("TROUBLESHOOTING:")
        print("  - Missing libraries? Run: pip install -r requirements.txt")
        print("  - Missing models? Run: python download_liveness_models.py")
        print("  - Code not updated? Verify files were saved correctly")
        print()
        return 1

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[--] Validation cancelled")
        sys.exit(2)
    except Exception as e:
        print(f"\n[!!] Validation error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(3)
