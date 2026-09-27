#!/usr/bin/env python3
"""
bench_v0_2_3.py -- which key selector should the revolver use?

Simulates 10 Groq keys with the REAL limits read from your headers:
  requests: 1000/day, refilled continuously (1 back every 86.4s)
  tokens:   8000/minute, refilled continuously (~133/s)

Compares three selectors, each with the revolver's v0.2.2 logic around it
(429 -> cooldown by retry-after, pre-emptive cooldown when TPM < max_tokens,
skip keys >= 90% of daily requests):
  sequential  current revolver: stay on the active key, advance when unusable
  weighted    pacing.py: random pick weighted by remaining headroom,
              danger penalty < 20%, hard cutoff <= 10%
  lru         least-recently-used usable key (plain spreading)

...and two ways of knowing a key's daily requests:
  stale  v0.2.2: count only updates when the key is called
  est    count + refill estimated from elapsed time

Metrics per scenario (avg of seeds):
  served%  requests answered
  429s     wasted round trips that hit a real rate limit
  failed   requests returned to Phone B as errors (no usable key)
  spread   max-min daily requests used across keys (lower = more even)

Stdlib only. No network. Run:  python bench_v0_2_3.py
"""
import random

N_KEYS = 10
TPM, RPD = 8000, 1000
TPM_RATE, RPD_RATE = TPM / 60.0, RPD / 86400.0
MAX_TOKENS = 1024          # reserve used for pre-emptive TPM cooldown
THRESHOLD = 0.90           # revolver rotate_threshold_pct
SEEDS = (1, 2, 3)


class Key:
    def __init__(self, i):
        self.i = i
        self.tok, self.req, self.t = float(TPM), float(RPD), 0.0   # provider truth
        self.seen_tok, self.seen_req, self.seen_at = float(TPM), float(RPD), 0.0
        self.cool_until = 0.0
        self.last_used = -1e18
        self.used_total = 0

    def _refill(self, now):
        dt = now - self.t
        self.tok = min(TPM, self.tok + dt * TPM_RATE)
        self.req = min(RPD, self.req + dt * RPD_RATE)
        self.t = now

    def call(self, now, tokens):
        """Provider side. Returns (ok, retry_after_s, tok_reset_s)."""
        self._refill(now)
        if self.req < 1 or self.tok < tokens:
            wait_t = (tokens - self.tok) / TPM_RATE if self.tok < tokens else 0.0
            wait_r = (1 - self.req) / RPD_RATE if self.req < 1 else 0.0
            self._see(now)
            return False, max(1.0, wait_t, wait_r), 0.0
        self.tok -= tokens
        self.req -= 1
        self.used_total += 1
        self._see(now)
        return True, 0.0, (TPM - self.tok) / TPM_RATE

    def _see(self, now):   # what the headers tell the revolver
        self.seen_tok, self.seen_req, self.seen_at = self.tok, self.req, now


def req_left(k, now, est, day_start):
    if est:
        return min(RPD, k.seen_req + (now - k.seen_at) * RPD_RATE)
    # stale: v0.2.2 local midnight reset is the only refresh
    return RPD if k.seen_at < day_start else k.seen_req


def tok_left(k, now):   # revolver knows tpm_reset_at, so TPM is always estimable
    return min(TPM, k.seen_tok + (now - k.seen_at) * TPM_RATE)


def usable(k, now, est, day_start):
    return now >= k.cool_until and req_left(k, now, est, day_start) > (1 - THRESHOLD) * RPD


class Sequential:
    def __init__(self):
        self.active = 0

    def pick(self, keys, now, est, ds, rng):
        for step in range(N_KEYS):
            k = keys[(self.active + step) % N_KEYS]
            if usable(k, now, est, ds):
                self.active = k.i
                return k
        return None


class Weighted:          # pacing.py tier 1 + tier 2 (danger penalty)
    def pick(self, keys, now, est, ds, rng):
        cands, weights = [], []
        for k in keys:
            if not usable(k, now, est, ds):
                continue
            frac = min(req_left(k, now, est, ds) / RPD, tok_left(k, now) / TPM)
            if frac <= 0.10:
                continue
            w = frac * (0.15 if frac < 0.20 else 1.0)
            cands.append(k)
            weights.append(w)
        return rng.choices(cands, weights=weights, k=1)[0] if cands else None


class LRU:
    def pick(self, keys, now, est, ds, rng):
        cands = [k for k in keys if usable(k, now, est, ds)]
        return min(cands, key=lambda k: k.last_used) if cands else None


def arrivals(kind, rate_per_min, hours, rng):
    end = hours * 3600.0
    if kind == "burst":                       # 20 at once every 30s
        t = 0.0
        while t < end:
            for _ in range(20):
                yield t
            t += 30.0
        return
    t = 0.0
    lam = rate_per_min / 60.0
    while True:
        t += rng.expovariate(lam)
        if t >= end:
            return
        yield t


def run(selector_cls, est, kind, rate, hours, avg_tokens, seed):
    rng = random.Random(seed)
    keys = [Key(i) for i in range(N_KEYS)]
    sel = selector_cls()
    served = n429 = failed = total = 0
    for now in arrivals(kind, rate, hours, rng):
        total += 1
        day_start = (now // 86400) * 86400
        tokens = int(min(4000, max(100, rng.gauss(avg_tokens, 0.4 * avg_tokens))))
        done = False
        for _ in range(N_KEYS):
            k = sel.pick(keys, now, est, day_start, rng)
            if k is None:
                break
            k.last_used = now
            ok, retry_after, tok_reset = k.call(now, tokens)
            if ok:
                if k.seen_tok < MAX_TOKENS:          # v0.2.2 pre-emptive cooldown
                    k.cool_until = max(k.cool_until, now + tok_reset)
                served += 1
                done = True
                break
            n429 += 1
            k.cool_until = now + retry_after
        if not done:
            failed += 1
    used = [k.used_total for k in keys]
    return dict(served=100.0 * served / max(total, 1), n429=n429,
                failed=failed, spread=max(used) - min(used), total=total)


SCENARIOS = [
    ("light     5/min for 1h", "poisson", 5, 1),
    ("busy     40/min for 1h", "poisson", 40, 1),
    ("over cap 80/min for 1h", "poisson", 80, 1),
    ("bursts 20 every 30s, 1h", "burst", 40, 1),
    ("all day  20/min for 24h", "poisson", 20, 24),
]
SELECTORS = [("sequential", Sequential), ("weighted", Weighted), ("lru", LRU)]


def main():
    print(__doc__.split("Stdlib only")[0].strip().splitlines()[0])
    print(f"10 keys x ({RPD} req/day, {TPM} tok/min), avg 1500 tok/request, "
          f"seeds {SEEDS}\n")
    for label, kind, rate, hours in SCENARIOS:
        print(f"=== {label} ===")
        print(f"  {'selector':<11} {'rpd':<6} {'served%':>8} {'429s':>7} "
              f"{'failed':>7} {'spread':>7}")
        for est in (False, True):
            for name, cls in SELECTORS:
                rs = [run(cls, est, kind, rate, hours, 1500, s) for s in SEEDS]
                avg = {m: sum(r[m] for r in rs) / len(rs)
                       for m in ("served", "n429", "failed", "spread")}
                print(f"  {name:<11} {'est' if est else 'stale':<6} "
                      f"{avg['served']:8.2f} {avg['n429']:7.1f} "
                      f"{avg['failed']:7.1f} {avg['spread']:7.1f}")
        print()


if __name__ == "__main__":
    main()
