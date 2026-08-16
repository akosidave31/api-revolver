"""
Thin client for Groq / any OpenAI-compatible chat completions endpoint.
Wraps calls with: pick active key -> call -> record real usage -> rotate
if needed -> retry once on 429 even if our local tracking missed it.
"""
import requests
from .rotator import Rotator, AllKeysExhausted

ENDPOINTS = {
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "openai": "https://api.openai.com/v1/chat/completions",
}


def chat(prompt, system=None, max_tokens=1024, temperature=0.7, _retried=False):
    rotator = Rotator()
    key = rotator.active_key()

    url = ENDPOINTS.get(key["provider"])
    if url is None:
        raise ValueError(f"Unknown provider '{key['provider']}'. "
                          f"Add its endpoint to ENDPOINTS in client.py")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

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

    if resp.status_code == 429:
        # our local tracking didn't catch it in time -> force rotate and retry once
        print(f"[revolver] 429 from '{key['name']}', forcing rotation.")
        if _retried:
            raise RuntimeError("Got 429 again immediately after rotating. "
                                "All keys may be rate-limited right now.")
        try:
            rotator.force_rotate()
        except AllKeysExhausted as e:
            raise RuntimeError(str(e))
        return chat(prompt, system, max_tokens, temperature, _retried=True)

    resp.raise_for_status()
    data = resp.json()

    usage = data.get("usage", {})
    total_tokens = usage.get("total_tokens", 0)
    rotator.record_usage(total_tokens)

    text = data["choices"][0]["message"]["content"]
    return {
        "text": text,
        "tokens_used_this_call": total_tokens,
        "key_used": key["name"],
    }
