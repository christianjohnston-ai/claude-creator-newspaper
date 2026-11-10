#!/usr/bin/env python3
"""Run the night shift by itself on a Mac (launchd). Only set this up if the person asks.

  python3 scripts/schedule.py run                  the night shift: fetch, watch, measure
  python3 scripts/schedule.py install --at 04:30   run it every night at 04:30
  python3 scripts/schedule.py install --at 04:30 --print   show the job file, install nothing
  python3 scripts/schedule.py status               is it installed, and the last log lines
  python3 scripts/schedule.py remove               turn it off and delete the job file

The night shift pulls, downloads, transcribes and measures, so the morning only needs
Claude to read and write: say "make my newspaper" and it prints the paper from what is
ready. The Mac must be on: if it is asleep at that time, macOS runs the job when it wakes.
"""
from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import SCRIPTS_DIR, UserError, home, run_main  # noqa: E402

LABEL = "com.creator-newspaper.nightly"
PLIST = Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")


def log_path() -> Path:
    return home() / "logs" / "nightly.log"


def cmd_run(_args) -> int:
    log_path().parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    for step in ("fetch.py", "watch.py", "measure.py"):
        print("== %s" % step, flush=True)
        r = subprocess.run([sys.executable, str(SCRIPTS_DIR / step)], env=env)
        if r.returncode != 0:
            print("== %s stopped with code %d; the rest waits for the morning" % (step, r.returncode), flush=True)
            return r.returncode
    print("== night shift done; say \"make my newspaper\" in Claude Code", flush=True)
    return 0


def plist_for(hour: int, minute: int) -> dict:
    path_dirs = []
    for tool in ("ffmpeg", "git"):
        p = shutil.which(tool)
        if p and os.path.dirname(p) not in path_dirs:
            path_dirs.append(os.path.dirname(p))
    path_dirs += [d for d in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin") if d not in path_dirs]
    env = {"PATH": ":".join(path_dirs)}
    if os.environ.get("CREATOR_NEWSPAPER_HOME"):
        env["CREATOR_NEWSPAPER_HOME"] = os.environ["CREATOR_NEWSPAPER_HOME"]
    return {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, str(SCRIPTS_DIR / "schedule.py"), "run"],
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "StandardOutPath": str(log_path()),
        "StandardErrorPath": str(log_path()),
        "EnvironmentVariables": env,
        "RunAtLoad": False,
    }


def launchctl(*args: str) -> int:
    return subprocess.run(["launchctl"] + list(args), capture_output=True, text=True).returncode


def cmd_install(args) -> int:
    if sys.platform != "darwin":
        raise UserError("This sets up macOS launchd. On Linux use cron: 30 4 * * * %s %s run"
                        % (sys.executable, SCRIPTS_DIR / "schedule.py"))
    try:
        hh, mm = [int(x) for x in args.at.split(":")]
        assert 0 <= hh < 24 and 0 <= mm < 60
    except (ValueError, AssertionError):
        raise UserError("--at must look like 04:30")
    data = plistlib.dumps(plist_for(hh, mm))
    if args.print:
        sys.stdout.write(data.decode("utf-8"))
        return 0
    log_path().parent.mkdir(parents=True, exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    uid = str(os.getuid())
    if PLIST.exists():
        launchctl("bootout", "gui/%s/%s" % (uid, LABEL))
    PLIST.write_bytes(data)
    if launchctl("bootstrap", "gui/%s" % uid, str(PLIST)) != 0 and launchctl("load", "-w", str(PLIST)) != 0:
        raise UserError("launchctl did not accept %s. Check it with: plutil -lint %s" % (PLIST, PLIST))
    print("Installed: the night shift runs every day at %02d:%02d." % (hh, mm))
    print("Job file: %s\nLog: %s" % (PLIST, log_path()))
    print("Turn it off any time: python3 %s remove" % (SCRIPTS_DIR / "schedule.py"))
    return 0


def cmd_remove(_args) -> int:
    uid = str(os.getuid())
    if launchctl("bootout", "gui/%s/%s" % (uid, LABEL)) != 0 and PLIST.exists():
        launchctl("unload", "-w", str(PLIST))
    if PLIST.exists():
        PLIST.unlink()
        print("Turned off and deleted %s." % PLIST)
    else:
        print("No nightly job was installed.")
    return 0


def cmd_status(_args) -> int:
    print("Job file: %s (%s)" % (PLIST, "installed" if PLIST.exists() else "not installed"))
    if PLIST.exists():
        info = plistlib.loads(PLIST.read_bytes())
        t = info.get("StartCalendarInterval", {})
        print("Runs at %02d:%02d every day." % (t.get("Hour", 0), t.get("Minute", 0)))
    lp = log_path()
    if lp.exists():
        print("Last lines of %s:" % lp)
        for line in lp.read_text(encoding="utf-8", errors="replace").splitlines()[-12:]:
            print("  " + line)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    i = sub.add_parser("install")
    i.add_argument("--at", default="04:30")
    i.add_argument("--print", action="store_true")
    sub.add_parser("remove")
    sub.add_parser("status")
    a = ap.parse_args()
    return {"run": cmd_run, "install": cmd_install, "remove": cmd_remove, "status": cmd_status}[a.cmd](a)


if __name__ == "__main__":
    run_main(main)
