"""Train PPO on the digital twin and export the actor as numpy weights for the backend.

    .venv-ml/Scripts/python ml/rl/train.py --steps 100000 --envs 4
Then evaluate + promote (backend venv, no torch):  cd backend && python -m scripts.ppo_eval vN --promote"""
import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
from stable_baselines3 import PPO
from stable_baselines3.common.env_util import make_vec_env
from stable_baselines3.common.vec_env import SubprocVecEnv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from env import FuelEnv  # noqa: E402

MODELS = Path(__file__).resolve().parents[2] / "backend" / "models" / "ppo"


def export(model, out):
    pol = model.policy
    lins = [m for m in pol.mlp_extractor.policy_net if hasattr(m, "weight")] + [pol.action_net]
    arrays = {}
    for i, lin in enumerate(lins):
        arrays[f"W{i}"] = lin.weight.detach().cpu().numpy()
        arrays[f"b{i}"] = lin.bias.detach().cpu().numpy()
    np.savez(out, **arrays)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", type=int, default=100_000)
    ap.add_argument("--envs", type=int, default=4)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    existing = [int(p.name[1:]) for p in MODELS.glob("v*") if p.name[1:].isdigit()]
    out = MODELS / f"v{max(existing, default=0) + 1}"
    out.mkdir(parents=True)
    env = make_vec_env(FuelEnv, n_envs=a.envs, seed=a.seed, vec_env_cls=SubprocVecEnv if a.envs > 1 else None)
    model = PPO("MlpPolicy", env, n_steps=256, batch_size=256, learning_rate=3e-4, gamma=0.97, gae_lambda=0.95,
                ent_coef=0.0, policy_kwargs={"net_arch": {"pi": [64, 64], "vf": [64, 64]}}, seed=a.seed, verbose=1)
    t0 = time.time()
    model.learn(total_timesteps=a.steps, progress_bar=False)
    export(model, out / "policy.npz")
    model.save(out / "sb3_model")
    (out / "train.json").write_text(json.dumps({"steps": a.steps, "envs": a.envs, "seed": a.seed,
                                                "minutes": round((time.time() - t0) / 60, 1)}, indent=2))
    print("exported", out)


if __name__ == "__main__":
    main()
