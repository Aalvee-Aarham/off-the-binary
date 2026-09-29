"""Gymnasium env: the calibrated digital twin, one step = one decision (every 2 ticks), PPO action -> LP plan.

Domain randomization every episode: seed, supply quantities (x0.6-1.3), and 0-3 random crisis events of all six
types, so the policy sees the surprise conditions the organizers can inject."""
import random
import sys
from pathlib import Path

import gymnasium as gym
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app import config  # noqa: E402
from app.forecast import Forecaster, arrivals, risks  # noqa: E402
from app.policy import ACT_DIM, OBS_DIM, features  # noqa: E402
from app.solvers import algo_ppo, build_problem, check, finalize  # noqa: E402
from app.world import Twin, baseline_world  # noqa: E402

H = config.HORIZON_TICKS
EVERY = 2          # ticks per decision, like the live loop under risk
EP_TICKS = 300     # inside the supply window of the scenario


def random_events(rng, tw):
    kinds = ["demand_spike", "route_disruption", "station_outage", "depot_constraint", "shipment_delay", "supply_shortfall"]
    stations, routes, depots = list(tw.stations), list(tw.routes), list(tw.depots)
    for _ in range(rng.choice([0, 0, 1, 1, 2, 3])):
        k = rng.choice(kinds)
        start, dur = rng.randint(10, EP_TICKS - 40), rng.randint(10, 80)
        p = {"demand_spike": {"multiplier": round(rng.uniform(1.2, 2.5), 2),
                              "region_ids": rng.choice([[], ["region-dhaka"], ["region-chattogram"]])},
             "route_disruption": {"route_ids": rng.sample(routes, rng.randint(1, 2))},
             "station_outage": {"station_ids": [rng.choice(stations)]},
             "depot_constraint": {"depot_ids": [rng.choice(depots)]},
             "shipment_delay": {"delay_ticks": rng.randint(5, 60)},
             "supply_shortfall": {"factor": round(rng.uniform(0.3, 0.8), 2)}}[k]
        tw.add_event(k, start, 1 if k in ("shipment_delay", "supply_shortfall") else dur, p)


class FuelEnv(gym.Env):
    observation_space = gym.spaces.Box(-5, 5, (OBS_DIM,), np.float32)
    action_space = gym.spaces.Box(-1, 1, (ACT_DIM,), np.float32)

    def __init__(self, randomize=True, scenario=None):
        self.randomize, self.scenario = randomize, scenario

    def _observe(self):
        s = self.tw.snapshot()
        self.fc.ingest(self.tw.demand_log[self.seen:], s)
        self.seen = len(self.tw.demand_log)
        p, a = self.fc.paths(s, H), arrivals(s, H)
        r = risks(s, self.fc, p, a, H)
        self.ctx = (s, p, a, r)
        return features(s, r, p)

    def reset(self, seed=None, options=None):
        super().reset(seed=seed)
        rng = random.Random(seed if seed is not None else random.randrange(1 << 30))
        w = baseline_world()
        w["seed"] = rng.randrange(1 << 30)
        if self.randomize:
            for a in w["supply"]:
                a["quantity"] *= rng.uniform(0.6, 1.3)
        self.tw = Twin(w)
        if self.scenario:
            self.scenario(self.tw)
        elif self.randomize:
            random_events(rng, self.tw)
        self.fc, self.seen = Forecaster(), 0
        return self._observe(), {}

    def step(self, action):
        s, p, a, r = self.ctx
        before = dict(self.tw.totals)
        cost = 0.0
        try:
            ships, _ = check(finalize(algo_ppo(build_problem(s, self.fc, p, a, r, H), action), s), s)
        except Exception:
            ships = []  # infeasible LP: act as hold (penalized by unmet demand)
        for x in ships:
            _, err = self.tw.submit(x["source_depot_id"], x["destination_station_id"], x["route_id"], x["fuel_type"], x["quantity"])
            if not err:
                cost += x["quantity"] * s["routes"][x["route_id"]]["transit_ticks"]
        for _ in range(EVERY):
            self.tw.step()
        t = self.tw.totals
        unmet = t["unmet"] - before["unmet"]
        reward = -unmet / 100 - cost * 2e-5 - 2 * (t["failed"] - before["failed"]) - t["overflow"] / 1000 + before["overflow"] / 1000
        done = self.tw.tick >= EP_TICKS
        return self._observe(), float(reward), done, False, {"unmet": unmet}
