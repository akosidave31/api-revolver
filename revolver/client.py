"""
Thin client for Groq / OpenAI-compatible endpoints + Google Gemini.

Wraps calls with:
pick active key -> call -> record usage -> rotate if needed
-> retry once on 429.
"""

import re
import requests
from .rotator import Rotator, AllKeysExhausted

ENDPOINTS = {
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "openai": "https://api.openai.com/v1/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
}

def _chat_openai_compatible(
    key,
    prompt,
    system,
    max_tokens,
    temperature,
):
    url = ENDPOINTS[key["provider"]]

    messages = []

    if system:
        messages.append({
            "role": "system",
            "content": system
        })

    messages.append({
        "role": "user",
        "content": prompt
    })

    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {key['api_key']}",
            "Content-Type": "application/json",
        },
        json={
            "model": key["model"],
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
        timeout=60,
    )

    return resp


def _chat_gemini(
    key,
    prompt,
    system,
    max_tokens,
    temperature,
):
    url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        f"models/{key['model']}:generateContent"
    )

    contents = [{
        "role": "user",
        "parts": [{
            "text": prompt
        }]
    }]

    body = {
        "contents": contents,
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature": temperature,
        },
    }

    if system:
        body["systemInstruction"] = {
            "parts": [{
                "text": system
            }]
        }

    resp = requests.post(
        url,
        params={
            "key": key["api_key"]
        },
        headers={
            "Content-Type": "application/json"
        },
        json=body,
        timeout=60,
    )

    return resp


class RevolverError(RuntimeError):
    """A failure that trying other keys won't fix: bad request, retired
    model, unknown provider, or no network. (revolver v0.2.1)"""


COOLDOWN_5XX_S = 30
COOLDOWN_TIMEOUT_S = 30
DEFAULT_RETRY_AFTER_S = 60


def _retry_after_seconds(resp):
    """Seconds the provider asked us to wait, clamped to 1s..1h."""
    raw = resp.headers.get("retry-after")
    try:
        secs = float(raw)
    except (TypeError, ValueError):
        secs = DEFAULT_RETRY_AFTER_S
    return min(max(secs, 1.0), 3600.0)


def _short(resp):
    return resp.text[:200].replace("\n", " ")


def _is_bad_key(key, resp):
    if resp.status_code in (401, 403):
        return True
    # Gemini reports an invalid key as 400, not 401
    if (key["provider"] == "gemini" and resp.status_code == 400
            and "API_KEY_INVALID" in resp.text):
        return True
    return False


# ---- revolver v0.2.2: provider rate-limit headers ----
_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")
_UNIT_S = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def _parse_duration(value):
    """'1m26.4s' -> 86.4, '134ms' -> 0.134, '7' -> 7.0, junk -> None."""
    if value is None:
        return None
    value = str(value).strip()
    try:
        return float(value)
    except ValueError:
        pass
    parts = _DURATION.findall(value)
    if not parts:
        return None
    return sum(float(n) * _UNIT_S[u] for n, u in parts)


def _to_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _ratelimits(resp):
    """Read x-ratelimit-* headers. Returns only the fields present,
    so providers without them give {} and nothing is changed.
    Groq: *-requests = per day, *-tokens = per minute."""
    h = resp.headers
    rl = {
        "req_limit": _to_int(h.get("x-ratelimit-limit-requests")),
        "req_remaining": _to_int(h.get("x-ratelimit-remaining-requests")),
        "tok_limit": _to_int(h.get("x-ratelimit-limit-tokens")),
        "tok_remaining": _to_int(h.get("x-ratelimit-remaining-tokens")),
        "tok_reset_s": _parse_duration(h.get("x-ratelimit-reset-tokens")),
    }
    return {k: v for k, v in rl.items() if v is not None}


def _send(key, prompt, system, max_tokens, temperature):
    provider = key["provider"]
    # Gemini uses its own API format.
    if provider == "gemini":
        return _chat_gemini(key, prompt, system, max_tokens, temperature)
    # Existing providers use OpenAI-compatible format.
    if provider in ENDPOINTS:
        return _chat_openai_compatible(key, prompt, system, max_tokens, temperature)
    raise RevolverError(
        f"Unknown provider '{provider}'. Add its endpoint/handler to client.py"
    )


def _parse(provider, data):
    """Return (text, total_tokens) from a successful response body."""
    # Gemini response format.
    if provider == "gemini":
        candidates = data.get("candidates", [])
        if not candidates:
            raise RevolverError(f"Gemini returned no candidates: {data}")
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(part.get("text", "") for part in parts)
        total_tokens = data.get("usageMetadata", {}).get("totalTokenCount", 0)
        return text, total_tokens

    # OpenAI-compatible response format.
    total_tokens = data.get("usage", {}).get("total_tokens", 0)
    text = data["choices"][0]["message"]["content"]
    return text, total_tokens


def chat(
    prompt,
    system=None,
    max_tokens=1024,
    temperature=0.7,
    _retried=False,   # unused since v0.2.1, kept for old callers
):
    """Send one prompt through the revolver. Tries each usable key at most
    once. Raises AllKeysExhausted when no key is usable right now, or
    RevolverError for problems another key won't fix."""
    rotator = Rotator()

    for _ in range(rotator.key_count()):
        key = rotator.acquire_key()   # raises AllKeysExhausted if none usable
        name = key["name"]
        provider = key["provider"]

        try:
            resp = _send(key, prompt, system, max_tokens, temperature)
        except requests.ConnectionError as e:
            # our network, not the key: don't punish it
            raise RevolverError(
                f"Network unreachable ({e.__class__.__name__}). "
                "Check the phone's internet connection."
            ) from e
        except requests.Timeout:
            print(f"[revolver] timeout on '{name}', cooling down {COOLDOWN_TIMEOUT_S}s.")
            rotator.mark_cooldown(key["id"], COOLDOWN_TIMEOUT_S, "timeout")
            continue

        code = resp.status_code

        # Rate limit / quota reached.
        if code == 429:
            secs = _retry_after_seconds(resp)
            print(f"[revolver] 429 from '{name}', cooling down {secs:.0f}s.")
            rotator.mark_cooldown(key["id"], secs, "429 rate limited")
            rotator.sync_limits(key["id"], _ratelimits(resp))
            continue

        # Provider-side outage.
        if code >= 500:
            print(f"[revolver] {code} from '{name}', cooling down {COOLDOWN_5XX_S}s.")
            rotator.mark_cooldown(key["id"], COOLDOWN_5XX_S, f"{code} server error")
            continue

        # Dead / revoked key.
        if _is_bad_key(key, resp):
            print(f"[revolver] {code} from '{name}': key rejected, disabling it.")
            rotator.mark_disabled(key["id"], f"{code} {_short(resp)}")
            continue

        # Config problem: every key with this model will fail the same way.
        if code == 404:
            raise RevolverError(
                f"404 from {provider} for model '{key['model']}' (key '{name}'): "
                f"{_short(resp)}\nThe model is probably retired or renamed. "
                "Update 'model' in ~/.revolver/keys.json."
            )

        if code >= 400:
            raise RevolverError(f"{code} from {provider} (key '{name}'): {_short(resp)}")

        text, total_tokens = _parse(provider, resp.json())

        # Record real usage.
        rotator.record_usage(total_tokens, key_id=key["id"])
        # provider's own numbers override our local estimate (v0.2.2)
        rotator.sync_limits(key["id"], _ratelimits(resp), reserve_tokens=max_tokens)

        return {
            "text": text,
            "tokens_used_this_call": total_tokens,
            "key_used": name,
        }

    # Every key was tried once and failed: report why none is usable now.
    rotator.acquire_key()   # raises AllKeysExhausted with the reason
    raise RevolverError("Every key failed once in a row. Run: revolver dashboard")
