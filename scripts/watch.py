#!/usr/bin/env python3
"""Watch each new reel: download it, transcribe it, pull frames, make a contact sheet.

For every reel marked "new" that has not been watched yet:
  1. download the video right away (scraper video links expire within hours or days)
  2. transcribe it on this computer (mlx-whisper or faster-whisper, see transcribe_one.py)
  3. pull frames with ffmpeg: every 0.5 s in the first 3 s (the hook), then about
     every 2 s, plus a frame at each scene change
  4. tile the frames into one contact sheet JPG, and keep a hero frame for the paper
Everything lands beside the run: runs/<run>/videos/ and runs/<run>/watch/<reel>/.

Usage:
  python3 scripts/watch.py                 watch every unwatched new reel
  python3 scripts/watch.py --only KEY      one reel, e.g. instagram:DAbc123
  python3 scripts/watch.py --redo          watch again even if already done
  python3 scripts/watch.py --check         check ffmpeg and speech-to-text, change nothing
"""
from __future__ import annotations

import argparse
import math
import os
import platform as _platform
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (SCRIPTS_DIR, SKILL_DIR, UserError, apify_token, data_dir, home, iso,  # noqa: E402
                    load_config, now_utc, read_json, resolve_run, run_main, runs_dir,
                    safe_name, write_json)
from apify_api import API_HOST  # noqa: E402

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
MAX_VIDEO_BYTES = 400 * 1024 * 1024
SHEET_COLUMNS = 8
SHEET_MAX_FRAMES = 48


def rel(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(home()))
    except ValueError:
        return str(p)


def need(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise UserError("%s is not installed. On a Mac: brew install ffmpeg (it includes ffprobe). "
                        "Elsewhere see ffmpeg.org/download.html." % tool)
    return path


def stt_python(cfg: Dict[str, Any]) -> str:
    """The Python that has the speech-to-text package: config, then a .venv in the data
    folder or the skill folder, then the Python running this script."""
    configured = (cfg.get("transcribe") or {}).get("python") or ""
    if configured:
        return os.path.expanduser(configured)
    for base in (home(), SKILL_DIR):
        cand = base / ".venv" / "bin" / "python"
        if cand.exists():
            return str(cand)
    return sys.executable


def stt_engine(cfg: Dict[str, Any]) -> Tuple[str, str]:
    py = stt_python(cfg)
    eng = (cfg.get("transcribe") or {}).get("engine") or "auto"
    try:
        out = subprocess.run([py, str(SCRIPTS_DIR / "transcribe_one.py"), "-", "-", "--engine", eng, "--check"],
                             capture_output=True, text=True, timeout=120)
        return py, out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return py, ""


# ------------------------------------------------------------- download

def download(url: str, dest: Path, token: Optional[str]) -> int:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "file":
        if os.environ.get("CREATOR_NEWSPAPER_ALLOW_FILE_URLS") != "1":
            raise RuntimeError("refusing a file:// video link (only allowed in tests)")
    elif parsed.scheme != "https":
        raise RuntimeError("refusing a non-https video link")
    headers = {"User-Agent": BROWSER_UA}
    # The Apify token goes only to Apify (TikTok videos the actor stored for us), never to a CDN.
    if token and parsed.scheme == "https" and parsed.hostname == API_HOST:
        headers["Authorization"] = "Bearer " + token
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as fh:
                total = 0
                while True:
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_VIDEO_BYTES:
                        raise RuntimeError("video is over %d MB" % (MAX_VIDEO_BYTES // (1024 * 1024)))
                    fh.write(chunk)
            if total < 10 * 1024:
                raise RuntimeError("downloaded file is only %d bytes" % total)
            os.replace(tmp, dest)
            return total
        except urllib.error.HTTPError as exc:
            last = "HTTP %s" % exc.code
            if exc.code in (403, 404, 410):
                break  # an expired or removed link will not come back by retrying
        except Exception as exc:  # noqa: BLE001
            last = str(exc)
        time.sleep(3 * (attempt + 1))
    if tmp.exists():
        tmp.unlink()
    raise RuntimeError("could not download the video (%s). Links from the scraper expire; the next fetch "
                       "gets a fresh one." % last)


# ---------------------------------------------------------------- ffmpeg

def probe_duration(video: Path) -> Optional[float]:
    out = subprocess.run([need("ffprobe"), "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "format=duration", "-of", "default=nw=1:nk=1", str(video)],
                         capture_output=True, text=True)
    try:
        return float(out.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return None


def extract_audio(video: Path, wav: Path) -> bool:
    r = subprocess.run([need("ffmpeg"), "-v", "error", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar",
                        "16000", "-c:a", "pcm_s16le", str(wav)], capture_output=True, text=True)
    return r.returncode == 0 and wav.exists() and wav.stat().st_size > 1000


def scene_times(video: Path, threshold: float) -> List[float]:
    r = subprocess.run([need("ffmpeg"), "-hide_banner", "-nostats", "-i", str(video), "-an", "-vf",
                        "scale=320:-2,select='gt(scene,%s)',showinfo" % threshold, "-f", "null", "-"],
                       capture_output=True, text=True)
    return [float(m) for m in re.findall(r"pts_time:([0-9]+(?:\.[0-9]+)?)", r.stderr)]


def frame_plan(duration: float, fr: Dict[str, Any], scenes: List[float]) -> List[Tuple[float, str]]:
    dense_end = min(float(fr["dense_seconds"]), duration)
    dense_step = float(fr["dense_step"])
    step = float(fr["step"])
    plan: List[Tuple[float, str]] = []
    t = 0.0
    while t < dense_end - 1e-6:
        plan.append((round(max(t, 0.05), 2), "hook"))
        t += dense_step
    # About every 2 s; longer videos get a wider step so the sheet stays readable.
    room = SHEET_MAX_FRAMES - len(plan) - min(len(scenes), int(fr["max_scene_frames"]))
    body = max(duration - dense_end, 0.0)
    if body > 0 and room > 0:
        step = max(step, body / room)
    t = dense_end
    while t < duration - 0.25:
        plan.append((round(t, 2), "every %.0f s" % step))
        t += step
    taken = [p[0] for p in plan]
    added = 0
    for s in sorted(scenes):
        if added >= int(fr["max_scene_frames"]) or len(plan) >= SHEET_MAX_FRAMES:
            break
        if s >= duration - 0.1 or any(abs(s - x) < 0.6 for x in taken):
            continue
        plan.append((round(s + 0.04, 2), "scene change"))  # just after the cut, not on it
        taken.append(s)
        added += 1
    plan.sort(key=lambda p: p[0])
    return plan


_DRAWTEXT_OK: Optional[bool] = None


def grab_frame(video: Path, t: float, out: Path, width: int, label: Optional[str]) -> bool:
    global _DRAWTEXT_OK
    vf = "scale=%d:-2" % width
    if label and _DRAWTEXT_OK is not False:
        vf_l = vf + (",drawtext=text='%s':x=8:y=8:fontsize=%d:fontcolor=white:box=1:boxcolor=black@0.65:"
                     "boxborderw=6" % (label, max(14, width // 14)))
        r = subprocess.run([need("ffmpeg"), "-v", "error", "-y", "-ss", "%.2f" % t, "-i", str(video),
                            "-frames:v", "1", "-vf", vf_l, "-q:v", "4", str(out)], capture_output=True, text=True)
        if r.returncode == 0 and out.exists():
            _DRAWTEXT_OK = True
            return True
        _DRAWTEXT_OK = False  # this ffmpeg has no drawtext (no freetype); frames go unlabeled
    r = subprocess.run([need("ffmpeg"), "-v", "error", "-y", "-ss", "%.2f" % t, "-i", str(video),
                        "-frames:v", "1", "-vf", vf, "-q:v", "4", str(out)], capture_output=True, text=True)
    return r.returncode == 0 and out.exists()


def contact_sheet(labeled: List[Path], out: Path) -> bool:
    if not labeled:
        return False
    tiles = out.parent / "_tiles"
    if tiles.exists():
        shutil.rmtree(tiles)
    tiles.mkdir()
    for i, p in enumerate(labeled):
        shutil.copyfile(p, tiles / ("t_%03d.jpg" % i))
    cols = min(SHEET_COLUMNS, len(labeled))
    rows = int(math.ceil(len(labeled) / float(cols)))
    r = subprocess.run([need("ffmpeg"), "-v", "error", "-y", "-framerate", "1", "-i", str(tiles / "t_%03d.jpg"),
                        "-vf", "tile=%dx%d:margin=6:padding=4:color=white" % (cols, rows), "-frames:v", "1",
                        "-q:v", "4", str(out)], capture_output=True, text=True)
    shutil.rmtree(tiles, ignore_errors=True)
    return r.returncode == 0 and out.exists()


# ------------------------------------------------------------------ watch

def watch_one(reel: Dict[str, Any], run_id: str, cfg: Dict[str, Any], py: str, engine: str,
              token: Optional[str]) -> Dict[str, Any]:
    name = safe_name(reel["key"])
    run_dir = runs_dir() / run_id
    wdir = run_dir / "watch" / name
    fdir = wdir / "frames"
    fdir.mkdir(parents=True, exist_ok=True)
    video = run_dir / "videos" / (name + ".mp4")
    result: Dict[str, Any] = {"key": reel["key"], "run": run_id, "watched_at": iso(now_utc()), "status": "failed",
                              "problems": []}

    if not video.exists():
        if not reel.get("video_url"):
            result["problems"].append("no video link was in the scraper output")
            return result
        try:
            size = download(reel["video_url"], video, token)
            result["video_bytes"] = size
        except RuntimeError as exc:
            result["problems"].append(str(exc))
            return result
    result["video"] = rel(video)
    duration = probe_duration(video)
    if not duration:
        result["problems"].append("ffprobe could not read the video file")
        return result
    result["duration_s"] = round(duration, 2)

    # Transcript
    wav = wdir / "audio.wav"
    tjson = wdir / "transcript.json"
    if engine and extract_audio(video, wav):
        cmd = [py, str(SCRIPTS_DIR / "transcribe_one.py"), str(wav), str(tjson), "--engine", engine]
        model = (cfg.get("transcribe") or {}).get("model") or ""
        if model:
            cmd += ["--model", model]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0 and tjson.exists():
            t = read_json(tjson, {})
            (wdir / "transcript.txt").write_text(t.get("text", "") + "\n", encoding="utf-8")
            result["transcript"] = {"path": rel(tjson), "engine": t.get("engine"), "model": t.get("model"),
                                    "language": t.get("language"), "words": len(t.get("text", "").split())}
            if not t.get("text"):
                result["problems"].append("no speech found in the audio")
        else:
            result["problems"].append("transcription failed: %s" % (r.stderr.strip().splitlines()[-1:] or ["?"])[0])
    elif not engine:
        result["problems"].append("no speech-to-text tool installed, so no transcript (see README)")
    else:
        result["problems"].append("the video has no audio track")
    if wav.exists():
        wav.unlink()

    # Frames and contact sheet
    fr = cfg["frames"]
    width = int(fr.get("frame_width", 360))
    plan = frame_plan(duration, fr, scene_times(video, float(fr["scene_threshold"])))
    frames, labeled = [], []
    for t, why in plan:
        out = fdir / ("f_%07.2f.jpg" % t)
        lab = fdir / ("l_%07.2f.jpg" % t)
        if grab_frame(video, t, out, width, None):
            frames.append({"t": t, "why": why, "path": rel(out)})
            if grab_frame(video, t, lab, 200, "%.1fs" % t):
                labeled.append(lab)
    sheet = wdir / "contact_sheet.jpg"
    if contact_sheet(labeled, sheet):
        result["contact_sheet"] = rel(sheet)
        # Many ffmpeg builds (Homebrew's included) have no drawtext, so the tiles may carry no
        # time label. This map says which tile is which moment.
        cols = min(SHEET_COLUMNS, len(labeled))
        times = [f for f in frames if (fdir / ("l_%07.2f.jpg" % f["t"])).exists()]
        lines = ["Contact sheet for %s: tiles read left to right, top to bottom, %d per row." % (reel["url"], cols)]
        for row in range(0, len(times), cols):
            lines.append("Row %d: %s" % (row // cols + 1, ", ".join(
                "%.1fs (%s)" % (f["t"], f["why"]) for f in times[row:row + cols])))
        (wdir / "contact_sheet.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        result["contact_sheet_map"] = rel(wdir / "contact_sheet.txt")
        result["contact_sheet_labeled"] = bool(_DRAWTEXT_OK)
    for p in labeled:
        p.unlink()
    result["frames"] = frames
    hook = [f for f in frames if f["why"] == "hook"]
    hero = min(hook or frames, key=lambda f: abs(f["t"] - 1.0)) if frames else None
    result["hero_frame"] = hero["path"] if hero else None
    result["status"] = "done" if frames else "failed"
    if not frames:
        result["problems"].append("ffmpeg could not pull frames")
    write_json(wdir / "watch.json", result)
    return result


def cleanup_videos(days: float) -> int:
    if days is None or days < 0:
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for p in runs_dir().glob("*/videos/*.mp4"):
        if p.stat().st_mtime < cutoff:
            p.unlink()
            removed += 1
    return removed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run")
    ap.add_argument("--only")
    ap.add_argument("--redo", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    need("ffmpeg")
    need("ffprobe")
    py, engine = stt_engine(cfg)
    if args.check:
        print("ffmpeg: %s" % shutil.which("ffmpeg"))
        print("speech-to-text: %s (python %s)" % (engine or "NOT INSTALLED", py))
        print("this machine: %s %s" % (sys.platform, _platform.machine()))
        return 0 if engine else 4
    run_id = resolve_run(args.run)
    reels = read_json(data_dir() / "reels.json", {})
    todo = [r for r in reels.values() if r.get("role") == "new" and r.get("is_video", True)
            and (args.redo or (r.get("watch") or {}).get("status") != "done")]
    if args.only:
        todo = [r for r in todo if r["key"] == args.only]
    todo.sort(key=lambda r: r.get("posted_at") or "")
    removed = cleanup_videos(float(cfg.get("keep_videos_days", 7)))
    if removed:
        print("Removed %d video file(s) older than %s days." % (removed, cfg.get("keep_videos_days")))
    if not engine:
        print("NOTE: no speech-to-text tool found, so reels get frames but no transcript. See README.")
    token = apify_token(required=False)
    done = failed = 0
    for i, r in enumerate(todo, 1):
        print("[%d/%d] %s @%s %s" % (i, len(todo), r["platform"], r["creator"], r["url"]))
        res = watch_one(r, run_id, cfg, py, engine, token)
        r["watch"] = {"status": res["status"], "run": run_id,
                      "dir": rel(runs_dir() / run_id / "watch" / safe_name(r["key"])),
                      "problems": res["problems"]}
        write_json(data_dir() / "reels.json", reels)
        if res["status"] == "done":
            done += 1
            words = (res.get("transcript") or {}).get("words", 0)
            print("    ok: %d frames, %d transcript words, sheet %s" % (len(res["frames"]), words,
                                                                     res.get("contact_sheet")))
        else:
            failed += 1
        for p in res["problems"]:
            print("    problem: %s" % p)
    print("Watched %d reel(s), %d failed." % (done, failed))
    return 0


if __name__ == "__main__":
    run_main(main)
