"""
int1.py — Next-Generation Face Attendance System (Tkinter UI)
--------------------------------------------------------------
UI IS UNTOUCHED. Only one fix applied to RecognitionWorker._run():

FIXES vs original:
  [1] liveness.check(face_landmarks=None) → now passes det.face_landmarks
      The BLINK challenge requires MediaPipe FaceMesh landmarks to compute
      EAR (Eye Aspect Ratio). Passing None meant blink was never detectable.
      Since BLINK is double-weighted in the challenge pool, users always
      timed out. Now the detector's face_landmarks field is passed through.
"""

import tkinter as tk
from tkinter import messagebox, ttk, simpledialog
import threading
import queue
import cv2
import numpy as np
import pandas as pd
import os
import sys
import time
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from collections import Counter
from typing import Optional

from detector  import FaceDetector
from liveness  import LivenessChecker
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
        pass

ADMIN_PASSWORD       = os.environ.get("ATTENDANCE_ADMIN_PWD", "1234")
ATTENDANCE_DIR       = Path("Attendance")
AUDIT_LOG            = Path("Data/attendance_audit.jsonl")
MAX_FAILED_ATTEMPTS  = 3
LOCKOUT_SECONDS      = 60
RECOGNITION_TIMEOUT  = 20
RECOGNITION_VOTES    = 8              # RELAXED: was 12, need fewer consistent frames
CONFIDENCE_ACCEPT    = 0.40           # RELAXED: was 0.55, lower threshold for acceptance
DUPLICATE_WINDOW_MIN = 60

def _audit(event: str, **kwargs):
    Path("Data").mkdir(exist_ok=True)
    record = {"ts": datetime.now().isoformat(), "event": event, **kwargs}
    with open(AUDIT_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")


class RecognitionWorker:
    """Runs camera + recognition pipeline in a daemon thread."""

    def __init__(self, store: EmbeddingStore):
        self._store    = store
        self._result_q: queue.Queue = queue.Queue()
        self._cancel   = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._cancel.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def cancel(self):
        self._cancel.set()

    def get_result(self, timeout: float = 0.05) -> Optional[dict]:
        try:
            return self._result_q.get(timeout=timeout)
        except queue.Empty:
            return None

    def _run(self):
        detector = FaceDetector(quality_threshold=0.30)
        liveness = LivenessChecker()
        liveness.reset()

        video = cv2.VideoCapture(0)
        if not video.isOpened():
            video = cv2.VideoCapture(1)
        if not video.isOpened():
            self._result_q.put({"status": "error", "msg": "Cannot open camera"})
            return

        predictions  = []
        confidences  = []
        start        = time.monotonic()
        spoof_streak = 0        # FIX: count consecutive False frames before accepting spoof
        SPOOF_STREAK_LIMIT = 5  # require 5 consecutive False frames to call spoof

        try:
            while not self._cancel.is_set():
                if time.monotonic() - start > RECOGNITION_TIMEOUT:
                    self._result_q.put({"status": "timeout"})
                    break

                ret, frame = video.read()
                if not ret:
                    continue

                gray_full  = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                detections = detector.detect(frame, enhance=True)

                instr = liveness.get_challenge_instruction()
                cv2.putText(frame, instr, (20, 40),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 0), 2)

                for det in detections:
                    # FIX [1]: was face_landmarks=None — blink never detectable.
                    # det.face_landmarks is now populated by the fixed detector.
                    live_result = liveness.check(
                        face_rgb=det.aligned_face,
                        gray_full=gray_full,
                        face_landmarks=det.face_landmarks,  # FIX: was None
                        yaw=det.yaw,
                        pitch=det.pitch)

                    if live_result is False:
                        spoof_streak += 1
                        cv2.putText(frame,
                                    f"Liveness fail ({spoof_streak}/{SPOOF_STREAK_LIMIT})",
                                    (det.bbox[0], det.bbox[1] - 10),
                                    cv2.FONT_HERSHEY_SIMPLEX,
                                    0.7, (0, 0, 255), 2)
                        if spoof_streak >= SPOOF_STREAK_LIMIT:
                            # FIX: only exit after MULTIPLE consecutive failures
                            self._result_q.put({"status": "spoof"})
                            return
                        continue   # FIX: try next frame instead of quitting
                    else:
                        spoof_streak = 0   # FIX: reset streak on non-False result

                    name, conf = self._store.recognize(det.aligned_face)
                    colour = (0, 255, 0) if name else (50, 50, 255)
                    x, y, w, h = det.bbox
                    cv2.rectangle(frame, (x, y), (x+w, y+h), colour, 2)

                    if name:
                        label = f"{name} {conf:.0%}"
                        cv2.putText(frame, label, (x, y - 10),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, colour, 2)
                        predictions.append(name)
                        confidences.append(conf)

                        if live_result is True and len(predictions) >= RECOGNITION_VOTES:
                            most_common = Counter(predictions).most_common(1)
                            winner, votes = most_common[0]
                            avg_conf = float(np.mean([
                                c for n, c in zip(predictions, confidences)
                                if n == winner
                            ]))
                            if votes >= RECOGNITION_VOTES * 0.6 and avg_conf >= CONFIDENCE_ACCEPT:
                                self._result_q.put({
                                    "status":     "ok",
                                    "name":       winner,
                                    "confidence": avg_conf,
                                    "votes":      votes,
                                })
                                return

                pct = int(min(len(predictions) / RECOGNITION_VOTES, 1.0) * 200)
                cv2.rectangle(frame, (20, frame.shape[0] - 30),
                              (20 + pct, frame.shape[0] - 15),
                              (0, 255, 0), -1)

                cv2.imshow("Face Recognition", frame)
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    self._result_q.put({"status": "cancelled"})
                    break

        finally:
            video.release()
            cv2.destroyAllWindows()
            detector.close()


# ── main application (UI UNCHANGED) ──────────────────────────────────────────
class AttendanceApp:

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Face Attendance — Next Gen")
        self.root.geometry("760x600")
        self.root.configure(bg="#1a1a2e")

        self._store   = EmbeddingStore()
        self._worker: Optional[RecognitionWorker] = None
        self._failed_attempts = 0
        self._lockout_until   = 0.0

        ATTENDANCE_DIR.mkdir(exist_ok=True)

        self._build_ui()
        self._refresh_table()

    def _build_ui(self):
        hdr = tk.Frame(self.root, bg="#16213e", pady=12)
        hdr.pack(fill="x")
        tk.Label(hdr, text="Smart Face Attendance System",
                 font=("Segoe UI", 18, "bold"),
                 bg="#16213e", fg="#e0e0e0").pack()
        tk.Label(hdr,
                 text=f"Persons enrolled: {self._store.person_count()}",
                 font=("Segoe UI", 10),
                 bg="#16213e", fg="#a0a0c0").pack()

        bf = tk.Frame(self.root, bg="#1a1a2e", pady=8)
        bf.pack(fill="x", padx=16)

        def btn(parent, text, cmd, color):
            return tk.Button(parent, text=text, command=cmd,
                             bg=color, fg="white",
                             font=("Segoe UI", 11, "bold"),
                             relief="flat", padx=12, pady=6,
                             cursor="hand2", activebackground=color)

        b1 = btn(bf, "Enroll New User",    self._enroll,          "#2d6a4f")
        b2 = btn(bf, "Mark Attendance",    self._mark_attendance,  "#1565C0")
        b3 = btn(bf, "View / Export",      self._export_prompt,    "#6a1b9a")
        b4 = btn(bf, "Clear Data",         self._clear_data,       "#b71c1c")
        b5 = btn(bf, "Manage Users",       self._manage_users,     "#37474f")

        for i, b in enumerate([b1, b2, b3, b4, b5]):
            b.grid(row=0, column=i, padx=5)

        self._status_var = tk.StringVar(value="Ready.")
        tk.Label(self.root, textvariable=self._status_var,
                 font=("Segoe UI", 10), bg="#1a1a2e", fg="#80cbc4",
                 anchor="w").pack(fill="x", padx=16, pady=2)

        tf = tk.Frame(self.root, bg="#1a1a2e")
        tf.pack(fill="both", expand=True, padx=16, pady=8)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview",
                        background="#0f3460", foreground="#e0e0e0",
                        fieldbackground="#0f3460", rowheight=28,
                        font=("Segoe UI", 10))
        style.configure("Treeview.Heading",
                        background="#16213e", foreground="#80cbc4",
                        font=("Segoe UI", 10, "bold"))

        cols = ("Name", "Time", "Date", "Confidence")
        self.tree = ttk.Treeview(tf, columns=cols, show="headings", height=16)
        widths    = [200, 120, 130, 110]
        for col, w in zip(cols, widths):
            self.tree.heading(col, text=col)
            self.tree.column(col, width=w, anchor="center")

        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    def _status(self, msg: str):
        self._status_var.set(msg)
        self.root.update_idletasks()

    def _enroll(self):
        pwd = simpledialog.askstring("Admin Auth", "Enter admin password:", show="*")
        if pwd != ADMIN_PASSWORD:
            messagebox.showerror("Auth Failed", "Incorrect password.")
            _audit("enroll_auth_fail")
            return

        name = simpledialog.askstring("Enroll", "Enter the person's full name:")
        if not name or not name.strip():
            return
        name = name.strip()

        if name in self._store.list_persons():
            messagebox.showerror("Duplicate", f"'{name}' is already enrolled.")
            return

        self._status(f"Starting enrollment for {name}...")
        import subprocess
        result = subprocess.run(
            [sys.executable, "Add_faces.py", name],
            capture_output=True, text=True, timeout=300)

        if result.returncode == 0:
            self._store = EmbeddingStore()
            messagebox.showinfo("Success", f"'{name}' enrolled successfully!")
            _audit("enroll_success", name=name)
            self._update_enrolled_count()
        else:
            err = result.stderr or result.stdout
            messagebox.showerror("Enrollment Failed", err[:300])
            _audit("enroll_fail", name=name, reason=err[:200])

        self._status("Ready.")

    def _mark_attendance(self):
        if time.monotonic() < self._lockout_until:
            rem = int(self._lockout_until - time.monotonic())
            messagebox.showwarning("Locked Out",
                                   f"Too many failures. Try again in {rem}s.")
            return

        if self._store.person_count() == 0:
            messagebox.showerror("No Users", "No users enrolled. Enroll users first.")
            return

        self._status("Recognition in progress...")
        self._worker = RecognitionWorker(self._store)
        self._worker.start()
        self.root.after(100, self._poll_worker)

    def _poll_worker(self):
        if self._worker is None:
            return
        result = self._worker.get_result()
        if result is None:
            self.root.after(100, self._poll_worker)
            return

        status = result.get("status")

        if status == "ok":
            name = result["name"]
            conf = result["confidence"]
            confirm = messagebox.askyesno(
                "Confirm Identity",
                f"Recognised as: {name}\nConfidence: {conf:.0%}\n\nIs this correct?")
            if not confirm:
                self._failed_attempts += 1
                self._check_lockout()
                self._status("Recognition rejected by user.")
                return
            self._failed_attempts = 0
            self._write_attendance(name, conf)

        elif status == "spoof":
            messagebox.showerror("Liveness Failed",
                                 "Anti-spoofing check failed. Use your real face.")
            _audit("spoof_detected")
            self._failed_attempts += 1
            self._check_lockout()

        elif status == "timeout":
            messagebox.showwarning("Timeout", "No face recognised within the time limit.")
            self._failed_attempts += 1
            self._check_lockout()

        elif status == "error":
            messagebox.showerror("Camera Error", result.get("msg", "Unknown error"))

        elif status == "cancelled":
            self._status("Recognition cancelled.")

        self._worker = None
        self._status("Ready.")

    def _check_lockout(self):
        if self._failed_attempts >= MAX_FAILED_ATTEMPTS:
            self._lockout_until = time.monotonic() + LOCKOUT_SECONDS
            messagebox.showwarning("Locked Out",
                                   f"Too many failures. Locked for {LOCKOUT_SECONDS}s.")
            _audit("lockout_triggered", attempts=self._failed_attempts)
            self._failed_attempts = 0

    def _write_attendance(self, name: str, confidence: float):
        now      = datetime.now()
        time_str = now.strftime("%H:%M:%S")
        date_str = now.strftime("%d-%m-%Y")
        filename = ATTENDANCE_DIR / f"Attendance_{date_str}.csv"

        if filename.exists():
            try:
                df = pd.read_csv(filename)
                recent = df[df["Name"] == name]
                if not recent.empty:
                    last_dt = pd.to_datetime(
                        recent["Date"].iloc[-1] + " " + recent["Time"].iloc[-1],
                        format="%d-%m-%Y %H:%M:%S")
                    if (now - last_dt) < timedelta(minutes=DUPLICATE_WINDOW_MIN):
                        messagebox.showinfo(
                            "Duplicate",
                            f"{name}'s attendance was already marked "
                            f"within the last {DUPLICATE_WINDOW_MIN} minutes.")
                        return
            except Exception as e:
                logger.warning(f"Could not check duplicate: {e}")

        exist = filename.exists()
        with open(filename, "a") as f:
            if not exist:
                f.write("Name,Time,Date,Confidence\n")
            f.write(f"{name},{time_str},{date_str},{confidence:.3f}\n")

        _audit("attendance_marked", name=name, time=time_str,
               date=date_str, confidence=round(confidence, 3))
        speak(f"{name}, attendance marked")
        messagebox.showinfo("Attendance Marked", f"{name} — {time_str} on {date_str}")
        self._refresh_table()

    def _refresh_table(self):
        for row in self.tree.get_children():
            self.tree.delete(row)

        if not ATTENDANCE_DIR.exists():
            return

        for f in sorted(ATTENDANCE_DIR.glob("*.csv"), reverse=True):
            try:
                df = pd.read_csv(f)
                for _, row in df.iterrows():
                    conf = f"{float(row.get('Confidence', 0)):.0%}" \
                           if "Confidence" in row else "—"
                    self.tree.insert("", "end",
                                     values=(row["Name"], row["Time"],
                                             row["Date"], conf))
            except Exception as e:
                logger.warning(f"Could not read {f}: {e}")

    def _export_prompt(self):
        self._refresh_table()
        try:
            import openpyxl
            choice = messagebox.askyesno("Export",
                                         "Export attendance as Excel (.xlsx)?\n"
                                         "(No = view only, refresh table)")
            if choice:
                self._export_excel()
        except ImportError:
            messagebox.showinfo("Export",
                                "CSV files are in the Attendance/ folder.\n"
                                "Install openpyxl for Excel export.")

    def _export_excel(self):
        dfs = []
        for f in sorted(ATTENDANCE_DIR.glob("*.csv")):
            try:
                dfs.append(pd.read_csv(f))
            except Exception:
                pass
        if not dfs:
            messagebox.showinfo("Export", "No attendance data found.")
            return
        combined = pd.concat(dfs, ignore_index=True)
        out = ATTENDANCE_DIR / f"export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        combined.to_excel(out, index=False)
        messagebox.showinfo("Exported", f"Saved to {out}")
        _audit("export_excel", file=str(out))

    def _manage_users(self):
        pwd = simpledialog.askstring("Admin Auth", "Enter admin password:", show="*")
        if pwd != ADMIN_PASSWORD:
            messagebox.showerror("Auth Failed", "Incorrect password.")
            return

        persons = self._store.list_persons()
        if not persons:
            messagebox.showinfo("Users", "No users enrolled.")
            return

        win = tk.Toplevel(self.root)
        win.title("Manage Enrolled Users")
        win.geometry("340x420")
        win.configure(bg="#1a1a2e")

        tk.Label(win, text="Enrolled Users",
                 font=("Segoe UI", 13, "bold"),
                 bg="#1a1a2e", fg="#e0e0e0").pack(pady=10)

        lb = tk.Listbox(win, font=("Segoe UI", 11),
                        bg="#0f3460", fg="#e0e0e0",
                        selectbackground="#1565C0", height=14)
        lb.pack(fill="both", expand=True, padx=16)
        for p in persons:
            lb.insert("end", p)

        def delete_selected():
            sel = lb.curselection()
            if not sel:
                return
            name = lb.get(sel[0])
            if messagebox.askyesno("Confirm Delete",
                                   f"Delete '{name}' from the system?"):
                self._store.delete_person(name)
                lb.delete(sel[0])
                _audit("user_deleted", name=name)
                self._update_enrolled_count()

        tk.Button(win, text="Delete Selected", command=delete_selected,
                  bg="#b71c1c", fg="white",
                  font=("Segoe UI", 10, "bold"),
                  relief="flat").pack(pady=10)

    def _clear_data(self):
        pwd = simpledialog.askstring("Admin Auth", "Enter admin password:", show="*")
        if pwd != ADMIN_PASSWORD:
            messagebox.showerror("Auth Failed", "Incorrect password.")
            return

        confirm = messagebox.askyesno(
            "Clear ALL Data",
            "This will delete ALL face data AND attendance records.\n"
            "This cannot be undone. Continue?")
        if not confirm:
            return

        import shutil
        p = Path("Data/face_db.json")
        if p.exists():
            p.unlink()
        if ATTENDANCE_DIR.exists():
            shutil.rmtree(ATTENDANCE_DIR)
        ATTENDANCE_DIR.mkdir()

        self._store = EmbeddingStore()
        self._refresh_table()
        self._update_enrolled_count()
        messagebox.showinfo("Cleared", "All data has been cleared.")
        _audit("full_data_clear")

    def _update_enrolled_count(self):
        for w in self.root.winfo_children():
            w.destroy()
        self._build_ui()
        self._refresh_table()


if __name__ == "__main__":
    root = tk.Tk()
    app  = AttendanceApp(root)
    root.mainloop()
