"""Structural demand forecast (the simulator's documented formula) + per-series EWMA correction,
residual-based uncertainty, CUSUM change detection, and stockout risk."""
import math
from datetime import timedelta

import numpy as np

from . import metrics as M
from .world import ACTIVE_ALLOC, FUELS, PROFILES, base_rate, parse_time

ALPHA, BETA = 0.2, 0.05          # EWMA speeds: correction, residual variance
CUSUM_K, CUSUM_H = 0.5, 5.0
MULT_SIGMA = 0.08                # correlated multiplier uncertainty (level shift risk)


def _phi(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


class Forecaster:
    def __init__(self):
        self.reset()

    def reset(self):
        self.corr, self.var, self.cusum, self.alarm_tick = {}, {}, {}, {}
        self.last_tick = -1
        self.mape = None

    def noise(self, station):
        return PROFILES.get(station["demand_profile"], PROFILES["regional"])[1]

    def sigma(self, station, fuel):
        v = self.var.get((station["id"], fuel))
        return max(self.noise(station), math.sqrt(v)) if v else self.noise(station)

    def expected(self, snap, station, fuel, hour):
        rf = snap["regions"][station["region_id"]]["demand_factor"]
        return base_rate(station["demand_profile"], fuel, hour, rf, station["demand_multiplier"],
                         snap["tick_minutes"]) * self.corr.get((station["id"], fuel), 1.0)

    def ingest(self, rows, snap):
        """Update corrections from observed demand. Multiplier is taken from the current snapshot (lag <= 1 cycle)."""
        errs = []
        for r in sorted(rows, key=lambda r: r["tick"]):
            s = snap["stations"].get(r["station_id"])
            if not s or r["tick"] <= self.last_tick:
                continue
            key = (s["id"], r["fuel_type"])
            rf = snap["regions"][s["region_id"]]["demand_factor"]
            exp = base_rate(s["demand_profile"], r["fuel_type"], parse_time(r["sim_time"]).hour, rf,
                            s["demand_multiplier"], snap["tick_minutes"])
            if exp < 1e-6:
                continue
            ratio = r["demand_liters"] / exp
            c = self.corr.get(key, 1.0)
            errs.append(abs(ratio - c) / max(ratio, 1e-6))
            err = ratio - c
            sd = self.sigma(s, r["fuel_type"])
            self.corr[key] = min(3.0, max(0.3, c + ALPHA * err))
            self.var[key] = (1 - BETA) * self.var.get(key, sd ** 2) + BETA * err ** 2
            z = err / sd
            st = max(0.0, self.cusum.get(key, 0.0) + z - CUSUM_K)
            if st > CUSUM_H:
                self.alarm_tick[key], st = r["tick"], 0.0
            self.cusum[key] = st
        if rows:
            self.last_tick = max(self.last_tick, max(r["tick"] for r in rows))
        if errs:
            m = float(np.mean(errs))
            self.mape = m if self.mape is None else 0.9 * self.mape + 0.1 * m
            M.FORECAST_MAPE.set(self.mape)

    def _mult_path(self, snap, station, H):
        """Known future multiplier changes: scheduled spikes start, active spikes end."""
        m = np.full(H, 1.0)
        t0 = snap["tick"]
        ticks = np.arange(t0, t0 + H)  # path index i <-> processed tick t0+i
        for e in snap["events"]:
            if e["type"] != "demand_spike" or e["status"] == "RESOLVED":
                continue
            p = e["parameters"]
            sids, rids = p.get("station_ids") or [], p.get("region_ids") or []
            if (sids or rids) and station["id"] not in sids and station["region_id"] not in rids:
                continue
            f = float(p.get("multiplier", 1.5))
            if e["status"] == "SCHEDULED":
                m[(ticks >= e["start_tick"]) & (ticks <= e["end_tick"])] *= f  # end_tick inclusive (calibrated)
            elif e["status"] == "ACTIVE":
                m[ticks > e["end_tick"]] /= f
        return m

    def paths(self, snap, H):
        """P50 demand per (station, fuel); index i = demand of processed tick t0+i (current tick is processed next)."""
        t0 = parse_time(snap["sim_time"])
        hours = [(t0 + timedelta(minutes=snap["tick_minutes"] * k)).hour for k in range(H)]
        out = {}
        for s in snap["stations"].values():
            mp = self._mult_path(snap, s, H)
            for f in FUELS:
                out[(s["id"], f)] = np.array([self.expected(snap, s, f, h) for h in hours]) * mp
        return out


def arrivals(snap, H):
    """Liters landing at each (station, fuel) at k=1..H (index k) from in-flight allocations."""
    out = {}
    routes = snap["routes"]
    for a in snap["allocations"]:
        if a["status"] not in ACTIVE_ALLOC:
            continue
        if a["status"] == "IN_TRANSIT" and a["expected_arrival_tick"] is not None:
            k = a["expected_arrival_tick"] - snap["tick"] + 1
        else:  # departs when the current tick is processed, lands transit ticks later
            k = 1 + routes.get(a["route_id"], {}).get("transit_ticks", 2)
        k = max(1, k)
        if k <= H:
            out.setdefault((a["destination_station_id"], a["fuel_type"]), np.zeros(H + 1))[k] += a["quantity"]
    return out


def risks(snap, fc, paths, arr, H):
    """Per (station, fuel): time to stockout (P50) and P(stockout within H) via normal approx."""
    out = {}
    hours_per_tick = snap["tick_minutes"] / 60
    for s in snap["stations"].values():
        for f in FUELS:
            d = paths[(s["id"], f)]
            a = arr.get((s["id"], f), np.zeros(H + 1))[1:]
            inv = s["inventory"][f]
            cum_d, cum_a = np.cumsum(d), np.cumsum(a)
            pos = inv + cum_a - cum_d
            below = np.nonzero(pos < 0)[0]
            tts = int(below[0]) + 1 if below.size else None
            sig = fc.sigma(s, f)
            sd = np.sqrt(np.cumsum((sig * d) ** 2) + (MULT_SIGMA * cum_d) ** 2) + 1e-6
            p = float(max(1 - _phi(z) for z in (inv + cum_a - cum_d) / sd))
            if s["status"] != "OPEN":
                p = 1.0  # outage: every liter demanded is unmet
            sig_list = []
            if s["demand_multiplier"] > 1.05:
                sig_list.append(f"demand multiplier x{s['demand_multiplier']:.2f}")
            if fc.alarm_tick.get((s["id"], f), -99) >= snap["tick"] - 4:
                sig_list.append("unexplained demand shift (CUSUM)")
            if tts is not None:
                sig_list.append(f"P50 stockout in {tts * hours_per_tick:.1f}h")
            out[(s["id"], f)] = {
                "station_id": s["id"], "fuel": f, "inventory": round(inv, 1), "capacity": s["capacity"][f],
                "demand_4h": round(float(d[:int(4 / hours_per_tick)].sum()), 1),
                "incoming": round(float(a.sum()), 1),
                "hours_to_stockout": round(tts * hours_per_tick, 2) if tts is not None else None,
                "p_stockout": round(p, 3), "sigma": round(sig, 3),
                "correction": round(fc.corr.get((s["id"], f), 1.0), 3), "signals": sig_list,
            }
    return out
