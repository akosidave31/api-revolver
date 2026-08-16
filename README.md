# API Revolver

A CLI that holds multiple API keys and rotates through them like a
revolver's cylinder — proactively, before any single key hits its
rate limit, not after.

## Why

If you run `domain-runtime`'s `llm.py` against Groq (or any provider)
enough times in a day, you hit the token cap and everything stalls.
This tool sits in front of the API call: it tracks real token usage
per key, and the moment a key crosses a threshold (default 90%), it
silently switches to the next key so the process never stops.

## Install (Termux)

```bash
cd api-revolver
pip install -e .
```

This installs the `revolver` command and pulls in `requests` (the
only dependency).

## 1. Load the cylinder

```bash
revolver setup
```

This is the "nano-style" part — it asks for API 1, then API 2, and
so on:

```
--- API 1 ---
  provider (e.g. groq, openai) [done to stop]: groq
  label/name for this key (e.g. groq-main): groq-primary
  api key value: gsk_xxxxxxxx
  model (e.g. llama-3.3-70b-versatile): llama-3.3-70b-versatile
  token limit for the period (e.g. 100000): 100000
  period [daily/monthly] (default daily): daily
  -> saved as key #1 (groq-primary)

--- API 2 ---
  provider (e.g. groq, openai) [done to stop]: groq
  label/name for this key (e.g. groq-main): groq-backup
  api key value: gsk_yyyyyyyy
  ...
  -> saved as key #2 (groq-backup)

--- API 3 ---
  provider ... [done to stop]: done
```

Keys are stored in `~/.revolver/keys.json`. Add as many as you want —
this is your revolver's cylinder capacity.

## 2. Use it

```bash
revolver run "explain constraint propagation in one paragraph"
```

Every call:
1. Picks the currently active key.
2. Sends the request.
3. Reads the real `usage.total_tokens` from the response.
4. Adds it to that key's running total.
5. If that pushes the key past the threshold, **rotates to the next
   key automatically** — so your *next* call already uses a fresh key.
6. If a provider returns 429 anyway (local tracking missed it, or
   usage came from elsewhere), it force-rotates and retries once.

## 3. Watch the tokens

```bash
revolver dashboard
```

```
=== API Revolver Dashboard (rotate at 90%) ===

-> #1 groq-primary   [groq]
     [##################--] 92.0%  used 920/1000  remaining 80 (daily)
   #2 groq-backup    [groq]
     [--------------------]  0.0%  used 0/1000  remaining 1000 (daily)
```

The `->` marks the key currently in use.

## 4. Tune the trigger point

```bash
revolver threshold 85
```

Lower this if you want more headroom before rotation, raise it to
squeeze more out of each key before switching.

## Wiring it into `domain-runtime`

In `llm.py`, replace the direct Groq POST with:

```python
from revolver.client import chat
result = chat(prompt, system=system_prompt, max_tokens=512)
text = result["text"]
```

That's it — `llm.py` no longer needs to know which key is active or
track its own usage; the revolver handles rotation transparently.

## Adding another provider

`client.py` has an `ENDPOINTS` dict mapping provider name to its
OpenAI-compatible chat completions URL. Add a line, and `revolver
setup` will accept that provider name.

## Notes on the "limit" you enter

Token limits are self-reported by you (Groq's free tier limits
aren't exposed via a clean API), so set `token_limit` to whatever
your plan's daily/monthly cap actually is. The tool tracks real
usage against that number — it doesn't guess.

## Files

```
revolver/
  storage.py       keys.json read/write (atomic writes)
  setup_wizard.py  nano-style interactive key entry
  rotator.py       threshold-based proactive rotation logic
  client.py        Groq/OpenAI-compatible call wrapper
  cli.py           `revolver` command entry point
setup.py           pip install -e .
```
