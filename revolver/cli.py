"""
Entry point. Usage:
  revolver setup            interactive key entry (nano-style)
  revolver dashboard        show tokens used/left per key
  revolver threshold 85     set proactive rotation threshold (% used)
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

    print(f"\n=== API Revolver Dashboard (rotate at {threshold}%) ===\n")
    for row in rows:
        marker = "->" if row["active"] else "  "
        print(f"{marker} #{row['id']} {row['name']:<14} [{row['provider']}]")
        print(f"     tokens   [{_bar(row['pct'])}] {row['pct']:5.1f}%  "
              f"used {row['used']}/{row['limit']}  "
              f"remaining {row['remaining']} ({row['period']})")
        print(f"     requests [{_bar(row.get('req_pct', 0))}] "
              f"{row.get('req_pct', 0):5.1f}%  "
              f"used {row.get('req_used', 0)}/{row.get('req_limit', 0)}  "
              f"remaining {row.get('req_remaining', 0)}")
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


def cmd_run(args):
    from .client import chat
    if not args:
        print('Usage: revolver run "your prompt here"')
        return
    prompt = " ".join(args)
    result = chat(prompt)
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
    elif cmd == "run":
        cmd_run(rest)
    else:
        print(f"Unknown command: {cmd}\n")
        print(__doc__)


if __name__ == "__main__":
    main()
