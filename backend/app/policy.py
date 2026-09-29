"""PPO allocation policy: numpy inference (no torch in the backend) + the feature vector shared with training.

Action (24 values in [-1, 1]): per station x fuel a priority weight (LP shortfall penalty 1..5) and a safety
factor (cover target x 0..2 sigma). The LP turns those into shipments, so every PPO action is feasible by
construction. Training: ml/rl/train.py. Versions: models/ppo/vN/{policy.npz, metrics.json}; ACTIVE = promoted one."""
import json
import math
import os
from pathlib import Path

import numpy as np

from .detect import depot_cover
from .world import FUELS, parse_time

MODELS = Path(os.getenv("MODELS_DIR", Path(__file__).resolve().parent.parent / "models")) / "ppo"
OBS_DIM, ACT_DIM = 110, 24


def features(snap, risks, paths):
    x = []
    for sid in sorted(snap["stations"]):
        s = snap["stations"][sid]
        for f in FUELS:
            r, cap = risks[(sid, f)], s["capacity"][f] or 1.0
            x += [s["inventory"][f] / cap, min(r["demand_4h"] / cap, 2.0), min(r["incoming"] / cap, 2.0), r["p_stockout"],
                  min((r["hours_to_stockout"] if r["hours_to_stockout"] is not None else 24) / 24, 1.0),
                  float(np.clip(s["demand_multiplier"] - 1, -1, 3)), float(s["status"] == "OPEN")]
    for did in sorted(snap["depots"]):
        d = snap["depots"][did]
        for f in FUELS:
            cover, need = depot_cover(snap, paths, did, f)
            x += [d["inventory"][f] / (d["capacity"][f] or 1.0), min(cover / max(need, 1), 3.0) / 3, min(need / 64, 2.0)]
    for rid in sorted(snap["routes"]):
        x.append(float(snap["routes"][rid]["status"] == "AVAILABLE"))
    h = parse_time(snap["sim_time"]).hour + parse_time(snap["sim_time"]).minute / 60
    x += [math.sin(2 * math.pi * h / 24), math.cos(2 * math.pi * h / 24)]
    return np.asarray(x, dtype=np.float32)


def decode(action, snap):
    """action in [-1,1]^24 -> ({(sid,f): weight}, {(sid,f): z})"""
    a = np.clip(np.asarray(action, dtype=np.float64), -1, 1)
    keys = [(sid, f) for sid in sorted(snap["stations"]) for f in FUELS]
    return ({k: 1 + 2 * (a[i] + 1) for i, k in enumerate(keys)},
            {k: float(a[12 + i] + 1) for i, k in enumerate(keys)})


class PPOPolicy:
    """SB3 MlpPolicy actor (Tanh, pi=[64,64]) evaluated deterministically: action = clip(mean, -1, 1)."""

    def __init__(self, path):
        w = np.load(path)
        self.layers = [(w[f"W{i}"], w[f"b{i}"]) for i in range(3)]
        self.path = str(path)

    def act(self, obs):
        h = np.asarray(obs, dtype=np.float32)
        for i, (W, b) in enumerate(self.layers):
            h = W @ h + b
            if i < 2:
                h = np.tanh(h)
        return np.clip(h, -1, 1)


def versions():
    if not MODELS.exists():
        return []
    out = []
    for p in sorted(MODELS.glob("v*/policy.npz"), key=lambda p: int(p.parent.name[1:])):
        m = p.parent / "metrics.json"
        out.append({"version": p.parent.name, "metrics": json.loads(m.read_text()) if m.exists() else None})
    return out


def active_version():
    f = MODELS / "ACTIVE"
    return f.read_text().strip() if f.exists() else None


def load_active():
    v = active_version()
    p = MODELS / v / "policy.npz" if v else None
    return (PPOPolicy(p), v) if p and p.exists() else (None, None)


def set_active(version):
    if not (MODELS / version / "policy.npz").exists():
        raise ValueError(f"unknown model version {version}")
    history = MODELS / "HISTORY"
    prev = active_version()
    if prev and prev != version:
        with open(history, "a") as f:
            f.write(prev + "\n")
    (MODELS / "ACTIVE").write_text(version)


def rollback():
    history = MODELS / "HISTORY"
    lines = history.read_text().split() if history.exists() else []
    if not lines:
        raise ValueError("no previous model to roll back to")
    prev = lines.pop()
    history.write_text("\n".join(lines) + ("\n" if lines else ""))
    (MODELS / "ACTIVE").write_text(prev)
    return prev
