"""
Thin client for Groq / OpenAI-compatible endpoints + Google Gemini.

Wraps calls with:
pick active key -> call -> record usage -> rotate if needed
-> retry once on 429.
"""

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


def chat(
    prompt,
    system=None,
    max_tokens=1024,
    temperature=0.7,
    _retried=False,
):
    rotator = Rotator()
    key = rotator.acquire_key()

    provider = key["provider"]

    # Gemini uses its own API format.
    if provider == "gemini":
        resp = _chat_gemini(
            key,
            prompt,
            system,
            max_tokens,
            temperature,
        )

    # Existing providers use OpenAI-compatible format.
    elif provider in ENDPOINTS:
        resp = _chat_openai_compatible(
            key,
            prompt,
            system,
            max_tokens,
            temperature,
        )

    else:
        raise ValueError(
            f"Unknown provider '{provider}'. "
            "Add its endpoint/handler to client.py"
        )

    # Rate limit / quota reached.
    if resp.status_code == 429:
        print(
            f"[revolver] 429 from '{key['name']}', "
            "forcing rotation."
        )

        if _retried:
            raise RuntimeError(
                "Got 429 again immediately after rotating. "
                "All keys may be rate-limited right now."
            )

        try:
            rotator.force_rotate(from_key_id=key["id"])
        except AllKeysExhausted as e:
            raise RuntimeError(str(e))

        return chat(
            prompt,
            system,
            max_tokens,
            temperature,
            _retried=True,
        )

    resp.raise_for_status()

    data = resp.json()

    # Gemini response format.
    if provider == "gemini":
        candidates = data.get("candidates", [])

        if not candidates:
            raise RuntimeError(
                f"Gemini returned no candidates: {data}"
            )

        parts = (
            candidates[0]
            .get("content", {})
            .get("parts", [])
        )

        text = "".join(
            part.get("text", "")
            for part in parts
        )

        usage = data.get("usageMetadata", {})

        total_tokens = usage.get(
            "totalTokenCount",
            0
        )

    # OpenAI-compatible response format.
    else:
        usage = data.get("usage", {})

        total_tokens = usage.get(
            "total_tokens",
            0
        )

        text = data["choices"][0]["message"]["content"]

    # Record real usage.
    rotator.record_usage(total_tokens, key_id=key["id"])

    return {
        "text": text,
        "tokens_used_this_call": total_tokens,
        "key_used": key["name"],
    }
