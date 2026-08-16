"""
The revolver's cylinder. Decides which key is "up" and rotates
BEFORE a key hits its limit, not after it fails.
"""
from datetime import date, timedelta
from . import storage


class NoKeysAvailable(Exception):
    pass


class AllKeysExhausted(Exception):
    pass


def _period_expired(key):
    start = date.fromisoformat(key["period_start"])
    today = date.today()
    if key["period"] == "daily":
        return today > start
    if key["period"] == "monthly":
        return (today.year, today.month) != (start.year, start.month)
    return False


def _maybe_reset(key):
    if _period_expired(key):
        key["tokens_used"] = 0
        key["requests_used"] = 0
        key["period_start"] = str(date.today())


def _pct_used(key):
    if key["token_limit"] <= 0:
        return 0.0
    return 100.0 * key["tokens_used"] / key["token_limit"]


def _request_pct_used(key):
    limit = key.get("request_limit", 0)
    if limit <= 0:
        return 0.0
    return 100.0 * key.get("requests_used", 0) / limit


def _worst_pct(key):
    """Whichever dimension -- tokens or requests -- is closer to its
    cap is what actually determines when this key runs out."""
    return max(_pct_used(key), _request_pct_used(key))


def _over_threshold(key, threshold_pct):
    return _worst_pct(key) >= threshold_pct


class Rotator:
    def __init__(self):
        self.store = storage.load()
        if not self.store["keys"]:
            raise NoKeysAvailable("No API keys configured. Run: revolver setup")
        # reset any keys whose period rolled over
        for k in self.store["keys"]:
            _maybe_reset(k)
        storage.save(self.store)

    def _keys(self):
        return self.store["keys"]

    def active_key(self, auto_rotate=True):
        """Return the key that should be used right now, rotating
        proactively if the current one is past the threshold."""
        keys = self._keys()
        idx = self.store["active_index"] % len(keys)
        threshold = self.store["rotate_threshold_pct"]

        if auto_rotate and _over_threshold(keys[idx], threshold):
            idx = self._advance(idx)

        return keys[idx]

    def _advance(self, start_idx):
        """Walk the cylinder looking for a key under threshold.
        Raises AllKeysExhausted if every key is past threshold."""
        keys = self._keys()
        threshold = self.store["rotate_threshold_pct"]
        n = len(keys)
        for step in range(1, n + 1):
            candidate = (start_idx + step) % n
            _maybe_reset(keys[candidate])
            if not _over_threshold(keys[candidate], threshold):
                self.store["active_index"] = candidate
                storage.save(self.store)
                print(f"[revolver] switched to key '{keys[candidate]['name']}' "
                      f"(was at {_worst_pct(keys[start_idx]):.1f}% on previous key "
                      f"-- tokens {_pct_used(keys[start_idx]):.1f}%, "
                      f"requests {_request_pct_used(keys[start_idx]):.1f}%)")
                return candidate
        raise AllKeysExhausted(
            "All configured keys are past the rotation threshold. "
            "Add more keys with: revolver setup"
        )

    def record_usage(self, tokens_used):
        """Call this after every API response with the actual token
        count consumed (prompt + completion). Also counts as one request
        toward the daily request-count cap."""
        keys = self._keys()
        idx = self.store["active_index"] % len(keys)
        keys[idx]["tokens_used"] += tokens_used
        keys[idx]["requests_used"] = keys[idx].get("requests_used", 0) + 1
        storage.save(self.store)

        # proactively check now, so the *next* call already rotates
        if _over_threshold(keys[idx], self.store["rotate_threshold_pct"]):
            self._advance(idx)

    def force_rotate(self):
        """Manual override, e.g. when a provider returns 429 despite
        our local tracking (headers lied, or usage came from elsewhere)."""
        idx = self.store["active_index"] % len(self._keys())
        return self._advance(idx)

    def set_threshold(self, pct):
        self.store["rotate_threshold_pct"] = pct
        storage.save(self.store)

    def dashboard_rows(self):
        keys = self._keys()
        active_idx = self.store["active_index"] % len(keys)
        rows = []
        for i, k in enumerate(keys):
            _maybe_reset(k)
            pct = _pct_used(k)
            remaining = max(k["token_limit"] - k["tokens_used"], 0)
            req_limit = k.get("request_limit", 0)
            req_used = k.get("requests_used", 0)
            req_pct = _request_pct_used(k)
            req_remaining = max(req_limit - req_used, 0)
            rows.append({
                "active": i == active_idx,
                "id": k["id"],
                "name": k["name"],
                "provider": k["provider"],
                "used": k["tokens_used"],
                "limit": k["token_limit"],
                "remaining": remaining,
                "pct": pct,
                "req_used": req_used,
                "req_limit": req_limit,
                "req_remaining": req_remaining,
                "req_pct": req_pct,
                "period": k["period"],
            })
        storage.save(self.store)
        return rows
