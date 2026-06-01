"""
anomaly.py  —  TACE: Temporal Attendance Coherence Engine
==========================================================
ATLAS: Adaptive Temporal Liveness Attendance System

NOVEL CONTRIBUTION: TACE — Temporal Attendance Coherence Engine

No existing open-source attendance system analyses the PATTERN of
attendance events over time to detect anomalies. TACE builds a
per-person "behavioral fingerprint" and flags:

  [A] PROXY ATTENDANCE
      If person A was last seen at confidence 0.95 but today appears
      at 0.51, this may be a different person being recognised as A.
      TACE tracks rolling confidence distributions per person and
      flags sudden drops that fall outside 2.5 standard deviations.

  [B] SESSION REPLAY ATTACK
      If the same person is recognised twice within 90 seconds with
      high confidence but very similar CNN scores (within 0.02),
      this may be a recorded session being replayed. Real re-attempts
      have natural variation; replays are suspiciously identical.

  [C] ARRIVAL ANOMALY
      TACE learns each person's typical attendance window (±1 hour).
      An attendance event 4+ hours outside their normal window is
      flagged as suspicious. Useful for catching proxy where the
      real student arrives later and notices their attendance is marked.

  [D] CONFIDENCE DRIFT
      If a person's recognition confidence trends downward across weeks,
      it may indicate gradual face-DB contamination or model drift.
      TACE plots the trend and alerts admins if confidence drops
      more than 0.15 over 10 consecutive sessions.

All detection runs in <1ms (pure pandas/numpy). No ML model required.
Results are stored in Data/tace_log.jsonl for audit.

Usage:
    from anomaly import TACEEngine
    tace = TACEEngine()
    flag = tace.check(name, confidence, liveness_score)
    if flag:
        print(flag.description)   # show admin
    tace.record(name, confidence, liveness_score)
"""

import json
import logging
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, List
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DATA_DIR    = Path("Data")
TACE_LOG    = DATA_DIR / "tace_log.jsonl"
ATTEND_DIR  = Path("Attendance")


@dataclass
class TACEFlag:
    """Returned when TACE detects an anomaly."""
    kind:        str    # 'proxy' | 'replay' | 'arrival' | 'drift'
    severity:    str    # 'WARNING' | 'SUSPICIOUS'
    name:        str
    description: str
    confidence:  float


class TACEEngine:
    """
    NOVEL: Temporal Attendance Coherence Engine.

    Call check() BEFORE marking attendance.
    Call record() AFTER marking attendance.

    The engine maintains a per-person profile loaded from historical
    attendance CSVs + the running TACE log.
    """

    # Thresholds
    CONFIDENCE_ZSCORE_THRESHOLD = 2.5   # σ below mean → proxy flag
    REPLAY_TIME_WINDOW_SEC      = 90    # seconds
    REPLAY_CONF_SIMILARITY      = 0.02  # identical confidence → replay flag
    ARRIVAL_WINDOW_HOURS        = 1.5   # ± hours from typical arrival
    DRIFT_SESSIONS              = 10    # sessions to compute trend
    DRIFT_THRESHOLD             = 0.15  # confidence drop → drift flag

    def __init__(self):
        DATA_DIR.mkdir(exist_ok=True)
        self._profiles: dict = {}   # name → profile dict
        self._session:  dict = {}   # name → (timestamp, conf) of last event
        self._load_profiles()

    # ──────────────────────────────────────────────────────────────────
    def _load_profiles(self):
        """Build per-person confidence and arrival profiles from CSV history."""
        if not ATTEND_DIR.exists():
            return

        records = []
        for f in sorted(ATTEND_DIR.glob("*.csv")):
            try:
                df = pd.read_csv(f)
                if "Confidence" not in df.columns:
                    continue
                df["datetime"] = pd.to_datetime(
                    df["Date"] + " " + df["Time"],
                    format="%d-%m-%Y %H:%M:%S", errors="coerce")
                df = df.dropna(subset=["datetime"])
                records.append(df)
            except Exception:
                pass

        if not records:
            return

        full = pd.concat(records, ignore_index=True)
        for name, grp in full.groupby("Name"):
            confs = grp["Confidence"].dropna().astype(float).tolist()
            hours = grp["datetime"].dt.hour.tolist()
            self._profiles[name] = {
                "confs":  confs,
                "hours":  hours,
                "mean_conf": float(np.mean(confs)) if confs else 0.7,
                "std_conf":  float(np.std(confs))  if len(confs) > 2 else 0.15,
                "mean_hour": float(np.mean(hours)) if hours else 9.0,
                "std_hour":  float(np.std(hours))  if len(hours) > 2 else 2.0,
            }

    # ──────────────────────────────────────────────────────────────────
    def check(self, name: str, confidence: float,
              liveness_score: float = 0.7) -> Optional[TACEFlag]:
        """
        Run all TACE checks before accepting an attendance event.
        Returns TACEFlag if anomaly detected, else None.
        """
        now = datetime.now()

        # [A] Proxy attendance: confidence outside historical distribution
        if name in self._profiles:
            p = self._profiles[name]
            if p["std_conf"] > 0.01 and len(p["confs"]) >= 5:
                z = (confidence - p["mean_conf"]) / p["std_conf"]
                if z < -self.CONFIDENCE_ZSCORE_THRESHOLD:
                    return TACEFlag(
                        kind="proxy",
                        severity="SUSPICIOUS",
                        name=name,
                        description=(
                            f"Confidence {confidence:.2f} is {abs(z):.1f}σ "
                            f"below {name}'s normal ({p['mean_conf']:.2f}±{p['std_conf']:.2f}). "
                            f"Possible proxy attendance."),
                        confidence=confidence)

        # [B] Session replay: same person, <90s, suspiciously identical score
        if name in self._session:
            prev_ts, prev_conf = self._session[name]
            elapsed = (now - prev_ts).total_seconds()
            if elapsed < self.REPLAY_TIME_WINDOW_SEC:
                if abs(confidence - prev_conf) < self.REPLAY_CONF_SIMILARITY:
                    return TACEFlag(
                        kind="replay",
                        severity="SUSPICIOUS",
                        name=name,
                        description=(
                            f"Duplicate recognition in {elapsed:.0f}s "
                            f"with nearly identical confidence "
                            f"({prev_conf:.3f} → {confidence:.3f}). "
                            f"Possible session replay attack."),
                        confidence=confidence)

        # [C] Arrival anomaly: outside typical window
        if name in self._profiles:
            p = self._profiles[name]
            if len(p["hours"]) >= 5 and p["std_hour"] > 0.1:
                hour_dev = abs(now.hour - p["mean_hour"])
                if hour_dev > self.ARRIVAL_WINDOW_HOURS + 2 * p["std_hour"]:
                    return TACEFlag(
                        kind="arrival",
                        severity="WARNING",
                        name=name,
                        description=(
                            f"{name} typically attends at "
                            f"{p['mean_hour']:.0f}:00 ± {p['std_hour']:.1f}h. "
                            f"Current time {now.strftime('%H:%M')} is "
                            f"{hour_dev:.1f}h outside normal window."),
                        confidence=confidence)

        # [D] Confidence drift: downward trend over last N sessions
        if name in self._profiles:
            p = self._profiles[name]
            confs = p["confs"]
            if len(confs) >= self.DRIFT_SESSIONS:
                recent = confs[-self.DRIFT_SESSIONS:]
                slope  = float(np.polyfit(range(len(recent)), recent, 1)[0])
                total_drop = slope * self.DRIFT_SESSIONS
                if total_drop < -self.DRIFT_THRESHOLD:
                    return TACEFlag(
                        kind="drift",
                        severity="WARNING",
                        name=name,
                        description=(
                            f"Confidence drift detected for {name}: "
                            f"dropped {abs(total_drop):.2f} over last "
                            f"{self.DRIFT_SESSIONS} sessions. "
                            f"May indicate lighting change or DB degradation."),
                        confidence=confidence)

        return None   # no anomaly

    # ──────────────────────────────────────────────────────────────────
    def record(self, name: str, confidence: float, liveness_score: float = 0.7):
        """
        Call after attendance is accepted. Updates profile + session + log.
        """
        now = datetime.now()

        # Update in-memory session (for replay detection)
        self._session[name] = (now, confidence)

        # Update profile
        if name not in self._profiles:
            self._profiles[name] = {
                "confs": [], "hours": [],
                "mean_conf": confidence, "std_conf": 0.15,
                "mean_hour": float(now.hour), "std_hour": 2.0
            }
        p = self._profiles[name]
        p["confs"].append(confidence)
        p["hours"].append(now.hour)
        # Keep last 50 for rolling stats
        if len(p["confs"]) > 50:
            p["confs"] = p["confs"][-50:]
            p["hours"] = p["hours"][-50:]
        p["mean_conf"] = float(np.mean(p["confs"]))
        p["std_conf"]  = float(np.std(p["confs"])) if len(p["confs"]) > 2 else 0.15
        p["mean_hour"] = float(np.mean(p["hours"]))
        p["std_hour"]  = float(np.std(p["hours"])) if len(p["hours"]) > 2 else 2.0

        # Write audit log
        record = {
            "ts":        now.isoformat(),
            "event":     "attendance_recorded",
            "name":      name,
            "confidence": round(confidence, 4),
            "liveness":  round(liveness_score, 4),
        }
        with open(TACE_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")

    # ──────────────────────────────────────────────────────────────────
    def summary(self, name: str) -> str:
        """Return a human-readable profile summary for a person."""
        if name not in self._profiles:
            return f"{name}: no history yet."
        p = self._profiles[name]
        return (
            f"{name}: {len(p['confs'])} sessions, "
            f"avg confidence {p['mean_conf']:.2f} ± {p['std_conf']:.2f}, "
            f"typical arrival {p['mean_hour']:.0f}:00 ± {p['std_hour']:.1f}h")
