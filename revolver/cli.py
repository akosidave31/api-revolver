"""
Entry point. Usage:
  revolver setup            interactive key entry (nano-style)
  revolver dashboard        show tokens used/left and health per key
  revolver threshold 85     set proactive rotation threshold (% used)
  revolver enable <id|all>  put disabled / cooling-down keys back in rotation
  revolver selector [name]  show or set key selector: weighted | sequential
  revolver run "prompt"     send a prompt through the active key
"""
import sys


def _bar(pct, width=20):
    filled = int(width * min(pct, 100) / 100)
    return "#" * filled + "-" * (width - filled)


def cmd_dashboard():
    from .rotator import Rotator
    r = Rotator()
    rows = r.dashboard_rows()
    threshold = r.store["rotate_threshold_pct"]

    selector = r.store.get("selector", "weighted")
    print(f"\n=== API Revolver Dashboard (rotate at {threshold}%, selector: {selector}) ===\n")
    for row in rows:
        marker = "->" if row["active"] else "  "
        print(f"{marker} #{row['id']} {row['name']:<14} [{row['provider']}] {row.get('model', '')}")
        print(f"     tokens   [{_bar(row['pct'])}] {row['pct']:5.1f}%  "
              f"used {row['used']}/{row['limit']}  "
              f"remaining {row['remaining']} ({row['period']})")
        print(f"     requests [{_bar(row.get('req_pct', 0))}] "
              f"{row.get('req_pct', 0):5.1f}%  "
              f"used {row.get('req_used', 0)}/{row.get('req_limit', 0)}  "
              f"remaining {row.get('req_remaining', 0)}")
        if row.get("tpm_limit"):
            print(f"     tpm      limit {row['tpm_limit']}/min  "
                  f"last seen {row.get('tpm_remaining', '?')} left")
        if row.get("status", "ok") != "ok":
            print(f"     status   {row['status']}")
    print()


def cmd_threshold(args):
    from .rotator import Rotator
    if not args:
        print("Usage: revolver threshold <percent>")
        return
    pct = float(args[0])
    r = Rotator()
    r.set_threshold(pct)
    print(f"Rotation threshold set to {pct}%")


def cmd_selector(args):
    from .rotator import Rotator, SELECTORS
    r = Rotator()
    if not args:
        print(f"selector: {r.store.get('selector', 'weighted')}  (options: {', '.join(SELECTORS)})")
        return
    try:
        r.set_selector(args[0])
    except ValueError as e:
        print(e)
        return
    print(f"selector set to {args[0]}")


def cmd_enable(args):
    from .rotator import Rotator
    if not args:
        print("Usage: revolver enable <id|all>")
        return
    r = Rotator()
    try:
        key_id = None if args[0] == "all" else int(args[0])
    except ValueError:
        print("Usage: revolver enable <id|all>")
        return
    touched = r.enable(key_id)
    if touched:
        print(f"Enabled: {', '.join(touched)}")
    else:
        print(f"No key with id {args[0]}")


def cmd_run(args):
    from .client import chat, RevolverError
    from .rotator import AllKeysExhausted, NoKeysAvailable
    if not args:
        print('Usage: revolver run "your prompt here"')
        return
    prompt = " ".join(args)
    try:
        result = chat(prompt)
    except (RevolverError, AllKeysExhausted, NoKeysAvailable) as e:
        print(f"[revolver] error: {e}")
        sys.exit(1)
    print(f"\n[key used: {result['key_used']}, tokens this call: {result['tokens_used_this_call']}]\n")
    print(result["text"])


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return

    cmd, rest = args[0], args[1:]

    if cmd == "setup":
        from . import setup_wizard
        setup_wizard.run()
    elif cmd == "dashboard":
        cmd_dashboard()
    elif cmd == "threshold":
        cmd_threshold(rest)
    elif cmd == "enable":
        cmd_enable(rest)
    elif cmd == "selector":
        cmd_selector(rest)
    elif cmd == "run":
        cmd_run(rest)
    else:
        print(f"Unknown command: {cmd}\n")
        print(__doc__)


if __name__ == "__main__":
    main()
