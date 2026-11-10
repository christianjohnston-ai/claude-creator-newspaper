#!/usr/bin/env python3
"""Move the Apify token into the skill's .env and check it, without ever printing it.

  python3 scripts/check_token.py import PATH_TO_FILE [--delete-original]
      Reads the token from the text file you saved, writes it to .env as APIFY_TOKEN
      (readable only by you, chmod 600), compares the saved copy with the original by
      SHA-256, and with --delete-original removes the original file.
  python3 scripts/check_token.py verify
      Asks Apify who the token belongs to and prints only the username, the plan and
      this month's usage. Never the token, never the raw response.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import UserError, apify_token, env_path, load_env, run_main  # noqa: E402
from apify_api import Apify, ApifyError  # noqa: E402


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_token_file(path: Path) -> str:
    if not path.exists():
        raise UserError("There is no file at %s." % path)
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
    if not lines:
        raise UserError("%s is empty. Paste the token into it and save." % path)
    tok = lines[0]
    if tok.upper().startswith("APIFY_TOKEN="):
        tok = tok.split("=", 1)[1].strip().strip('"').strip("'")
    if " " in tok or len(tok) < 20:
        raise UserError("The first line of %s does not look like an Apify token (one word, usually starting "
                        "with apify_api_). Copy it again from Apify Console > Settings > API & Integrations." % path)
    return tok


def write_env(token: str) -> Path:
    path = env_path()
    keep = []
    if path.exists():
        keep = [ln for ln in path.read_text(encoding="utf-8").splitlines()
                if not ln.strip().startswith("APIFY_TOKEN=")]
    keep.append("APIFY_TOKEN=" + token)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(keep) + "\n")
    os.chmod(str(path), 0o600)
    return path


def git_ignored(path: Path) -> str:
    """'yes', 'NO', or 'not a git folder'."""
    try:
        r = subprocess.run(["git", "-C", str(path.parent), "rev-parse", "--is-inside-work-tree"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return "not a git folder"
        r = subprocess.run(["git", "-C", str(path.parent), "check-ignore", "-q", str(path)])
        return "yes" if r.returncode == 0 else "NO"
    except OSError:
        return "not a git folder"


def cmd_import(args) -> int:
    src = Path(args.file).expanduser().resolve()
    token = read_token_file(src)
    path = write_env(token)
    saved = load_env().get("APIFY_TOKEN", "")
    if sha(saved) != sha(token):
        raise UserError("The saved copy does not match the original. Nothing was deleted. Try again.")
    mode = oct(os.stat(str(path)).st_mode & 0o777)
    print("Token saved to %s as APIFY_TOKEN (permissions %s, readable only by you)." % (path, mode))
    print("Check: the saved copy matches your file (SHA-256 compared; the token was not shown).")
    ig = git_ignored(path)
    if ig == "NO":
        print("WARNING: git would commit %s. Add .env to .gitignore before any commit." % path)
    elif ig == "yes":
        print("git ignores .env, so it will never be committed.")
    if args.delete_original:
        if src == path.resolve():
            print("The original is the .env itself, so nothing else to delete.")
        else:
            src.unlink()
            print("Deleted the original file %s." % src)
    else:
        print("The original file is still at %s. Delete it once the check below passes." % src)
    return 0


def cmd_verify(_args) -> int:
    api = Apify(apify_token())
    try:
        me = api.me()
        lim = api.limits()
    except ApifyError as exc:
        raise UserError(exc.plain())
    plan = me.get("plan") or {}
    print("Apify accepted the token.")
    print("  username: %s" % me.get("username", "?"))
    print("  plan: %s%s" % (plan.get("id") or "?", " (paying)" if me.get("isPaying") else ""))
    cur = (lim.get("current") or {}).get("monthlyUsageUsd")
    mx = (lim.get("limits") or {}).get("maxMonthlyUsageUsd")
    cyc = lim.get("monthlyUsageCycle") or {}
    if cur is not None:
        print("  used this billing month: $%.4f of the account's $%s limit (month ends %s)"
              % (float(cur), mx if mx is not None else "?", str(cyc.get("endAt", "?"))[:10]))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("import")
    a.add_argument("file")
    a.add_argument("--delete-original", action="store_true")
    sub.add_parser("verify")
    args = ap.parse_args()
    return cmd_import(args) if args.cmd == "import" else cmd_verify(args)


if __name__ == "__main__":
    run_main(main)
