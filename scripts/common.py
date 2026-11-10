"""Shared helpers for the creator-newspaper scripts.

Standard library only, and written to run on Python 3.9 or newer, because the
python3 that ships with macOS (Xcode command line tools) is 3.9.

Where the data lives: everything (config.json, .env, data/, runs/, editions/)
sits in one HOME folder. By default HOME is the skill folder itself (the folder
that holds SKILL.md). Set CREATOR_NEWSPAPER_HOME to use another folder, which
is how tests keep their output out of the skill folder.
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPTS_DIR.parent

# Long dashes are banned in everything the newspaper writes. They are written
# here as escapes so this file itself contains none.
EM_DASH = "\u2014"
EN_DASH = "\u2013"
LONG_DASHES = (EM_DASH, EN_DASH)


class UserError(Exception):
    """A problem the person can fix. Printed in plain words, no traceback."""


def home() -> Path:
    override = os.environ.get("CREATOR_NEWSPAPER_HOME", "").strip()
    return Path(override).expanduser().resolve() if override else SKILL_DIR


def data_dir() -> Path:
    return home() / "data"


def runs_dir() -> Path:
    return home() / "runs"


def editions_dir() -> Path:
    return home() / "editions"


def config_path() -> Path:
    return home() / "config.json"


def env_path() -> Path:
    return home() / ".env"


# ---------------------------------------------------------------- json files

def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, value: Any) -> None:
    """Write atomically so a crash never leaves half a file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(value, fh, indent=2, ensure_ascii=False, sort_keys=False)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# ------------------------------------------------------------------- config

DEFAULTS: Dict[str, Any] = {
    "paper_name": "The Morning Reel",
    "niche": "",
    "what_to_learn": "",
    "creators": [],
    "budget": {"monthly_limit_usd": 5.0},
    "first_window_days": 3,
    "baseline_reels": 30,
    "max_new_per_creator_per_run": 10,
    "measure": {
        "judge_at_hours": 72,
        "min_history": 10,
        "breakout": 3.0,
        "above": 1.5,
        "below": 0.5,
        "early_after_days": 14,
        "early_multiple": 3.0,
    },
    "prices_usd": {
        "checked_on": "2026-11-10",
        "instagram": {
            "actor": "apify/instagram-reel-scraper",
            "per_reel": 0.0026,
            "per_run_start": 0.001,
        },
        "tiktok": {
            "actor": "clockworks/tiktok-scraper",
            "per_result": 0.0037,
            "date_filter_per_result": 0.0013,
            "video_download_per_video": 0.0013,
            "per_run_start": 0.001,
        },
    },
    "transcribe": {"engine": "auto", "model": "", "python": ""},
    "frames": {
        "dense_seconds": 3.0,
        "dense_step": 0.5,
        "step": 2.0,
        "scene_threshold": 0.3,
        "max_scene_frames": 12,
        "frame_width": 360,
    },
    "keep_videos_days": 7,
}


def _merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(required: bool = True) -> Dict[str, Any]:
    path = config_path()
    if not path.exists():
        if required:
            raise UserError(
                "No config.json yet at %s. Run the setup first (say \"set up my newspaper\" to Claude), "
                "or copy config.example.json to config.json and fill it in." % path
            )
        return _merge(DEFAULTS, {})
    try:
        raw = read_json(path, {})
    except json.JSONDecodeError as exc:
        raise UserError("config.json is not valid JSON (line %d): %s" % (exc.lineno, exc.msg))
    cfg = _merge(DEFAULTS, raw)
    clean = []
    for c in cfg.get("creators", []):
        platform = str(c.get("platform", "")).strip().lower()
        handle = normalize_handle(str(c.get("handle", "")))
        if platform not in ("instagram", "tiktok"):
            raise UserError("config.json: creator %r has platform %r; use \"instagram\" or \"tiktok\"." % (c, platform))
        if not handle:
            raise UserError("config.json: a creator is missing its handle: %r" % (c,))
        clean.append({"platform": platform, "handle": handle})
    cfg["creators"] = clean
    return cfg


def normalize_handle(handle: str) -> str:
    """Accept '@name', 'name', or a profile link, and return 'name'."""
    h = handle.strip()
    m = re.search(r"(?:instagram\.com|tiktok\.com)/@?([A-Za-z0-9._]+)", h)
    if m:
        h = m.group(1)
    return h.lstrip("@").strip().strip("/").lower()


# ----------------------------------------------------------------- the token

def load_env() -> Dict[str, str]:
    values: Dict[str, str] = {}
    path = env_path()
    if not path.exists():
        return values
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip().strip('"').strip("'")
    return values


def apify_token(required: bool = True) -> Optional[str]:
    token = os.environ.get("APIFY_TOKEN") or load_env().get("APIFY_TOKEN")
    if not token and required:
        raise UserError(
            "No Apify token found. Save your token in a text file and ask Claude to set it up "
            "(it moves it into %s as APIFY_TOKEN, readable only by you)." % env_path()
        )
    return token


# -------------------------------------------------------------------- times

def now_utc() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)


def parse_time(value: Any) -> Optional[_dt.datetime]:
    """Parse ISO text (with or without Z) or a unix number into an aware UTC time."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        if value > 1e12:  # milliseconds
            value = value / 1000.0
        return _dt.datetime.fromtimestamp(float(value), tz=_dt.timezone.utc)
    text = str(value).strip()
    if re.fullmatch(r"\d{9,13}", text):
        return parse_time(int(text))
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # Python 3.9 fromisoformat does not accept fractional seconds of any length.
    text = re.sub(r"(\.\d{1,6})\d*", r"\1", text)
    try:
        dt = _dt.datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = _dt.datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.astimezone(_dt.timezone.utc)


def iso(dt: Optional[_dt.datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def hours_between(a: _dt.datetime, b: _dt.datetime) -> float:
    return (b - a).total_seconds() / 3600.0


def local_date(dt: Optional[_dt.datetime] = None) -> str:
    dt = dt or now_utc()
    return dt.astimezone().strftime("%Y-%m-%d")


# ------------------------------------------------------------ small helpers

def median(values: List[float]) -> Optional[float]:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    if n % 2:
        return float(vals[mid])
    return (vals[mid - 1] + vals[mid]) / 2.0


def to_int(value: Any) -> Optional[int]:
    """Counts from scrapers can be None, strings, or -1 for hidden. Hidden means unknown."""
    if value is None or isinstance(value, bool):
        return None
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def to_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def safe_name(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", key)


def fmt_int(n: Optional[int]) -> str:
    return "n/a" if n is None else "{:,}".format(int(n))


def fmt_age(hours: Optional[float]) -> str:
    if hours is None:
        return "n/a"
    if hours < 48:
        h = int(round(hours))
        return "1 hour" if h == 1 else "%d hours" % h
    return "%.1f days" % (hours / 24.0)


def strip_long_dashes(text: str) -> str:
    """For quoted creator text (captions, transcripts) shown in the paper.
    The paper itself never prints a long dash, so quoted ones become a hyphen."""
    if not text:
        return text
    return text.replace(EM_DASH, "-").replace(EN_DASH, "-")


def find_long_dash(text: str) -> Optional[int]:
    for i, ch in enumerate(text or ""):
        if ch in LONG_DASHES:
            return i
    return None


def latest_run_id() -> Optional[str]:
    rd = runs_dir()
    if not rd.exists():
        return None
    runs = sorted(p.name for p in rd.iterdir() if p.is_dir() and (p / "fetch.json").exists())
    return runs[-1] if runs else None


def resolve_run(run_id: Optional[str]) -> str:
    rid = run_id or latest_run_id()
    if not rid:
        raise UserError("No run found in %s. Run scripts/fetch.py first." % runs_dir())
    if not (runs_dir() / rid / "fetch.json").exists():
        raise UserError("Run %s has no fetch.json in %s." % (rid, runs_dir()))
    return rid


def run_main(fn) -> None:
    """Run a script's main() and print UserError in plain words."""
    try:
        code = fn()
    except UserError as exc:
        print("STOPPED: %s" % exc, file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("Stopped by you.", file=sys.stderr)
        sys.exit(130)
    sys.exit(code or 0)
