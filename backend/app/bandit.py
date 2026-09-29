"""Online learner over algorithms: Beta-Bernoulli Thompson sampling per regime.

Every full tournament is an observation: each candidate "wins" if its twin score is within 2% of the best.
The posterior drives budget mode (which few candidates to run when there's no time for all of them) and is
exposed for inspection. ponytail: in-memory posteriors; persist to SQLite if restarts during a run matter."""
import random

from . import metrics as M


class Bandit:
    def __init__(self, arms, prior=(1.0, 1.0)):
        self.arms, self.prior = list(arms), prior
        self.post = {}  # (regime, arm) -> [alpha, beta]

    def _ab(self, regime, arm):
        return self.post.setdefault((regime, arm), list(self.prior))

    def update(self, regime, candidates):
        scored = [c for c in candidates if c.get("score") is not None]
        if not scored:
            return
        best = min(c["score"] for c in scored)
        for c in scored:
            ab = self._ab(regime, c["algorithm"])
            win = c["score"] <= best * 1.02 + 1
            ab[0 if win else 1] += 1
            M.BANDIT_MEAN.labels(regime, c["algorithm"]).set(ab[0] / (ab[0] + ab[1]))

    def top(self, regime, k, arms=None):
        """Thompson draw: the k arms most likely to win in this regime (explores while uncertain)."""
        arms = [a for a in (arms or self.arms)]
        draws = {a: random.betavariate(*self._ab(regime, a)) for a in arms}
        return sorted(arms, key=lambda a: -draws[a])[:k]

    def table(self):
        return {f"{r}/{a}": {"win_rate": round(ab[0] / (ab[0] + ab[1]), 3), "n": int(ab[0] + ab[1] - sum(self.prior))}
                for (r, a), ab in sorted(self.post.items())}
