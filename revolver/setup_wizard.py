"""
Interactive key entry. Prompts API 1, API 2, ... until you're done.
This is the "revolver chamber loader."
"""
from . import storage


def run():
    store = storage.load()
    n = len(store["keys"]) + 1

    print("=== API Revolver: Key Setup ===")
    print("Enter your API keys one at a time. Type 'done' at the provider")
    print("prompt to stop.\n")

    while True:
        print(f"--- API {n} ---")
        provider = input(f"  provider (e.g. groq, openai) [done to stop]: ").strip()
        if provider.lower() == "done":
            break
        if not provider:
            print("  provider cannot be empty, try again.\n")
            continue

        name = input(f"  label/name for this key (e.g. groq-main): ").strip() or f"key{n}"
        api_key = input(f"  api key value: ").strip()
        if not api_key:
            print("  api key cannot be empty, skipping this slot.\n")
            continue
        model = input(f"  model (e.g. llama-3.3-70b-versatile): ").strip()

        limit_raw = input(f"  token limit for the period (e.g. 100000): ").strip()
        try:
            token_limit = int(limit_raw)
        except ValueError:
            print("  invalid number, defaulting to 100000")
            token_limit = 100000

        period = input(f"  period [daily/monthly] (default daily): ").strip().lower()
        if period not in ("daily", "monthly"):
            period = "daily"

        store = storage.add_key(store, provider, name, api_key, model, token_limit, period)
        print(f"  -> saved as key #{store['keys'][-1]['id']} ({name})\n")
        n += 1

    if store["keys"]:
        print(f"Done. {len(store['keys'])} key(s) loaded into the revolver.")
        print(f"Proactive rotation threshold: {store['rotate_threshold_pct']}% "
              f"(change with: revolver threshold <pct>)")
    else:
        print("No keys added.")


if __name__ == "__main__":
    run()
