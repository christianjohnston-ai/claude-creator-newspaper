#!/usr/bin/env python3
"""Measure how each new reel did against that creator's usual views.

The rule (all numbers live in config.json under "measure"):
  - A reel is judged once, at its 3-day reading: the first saved reading taken when the
    reel was at least 72 hours old. The label is then frozen in data/labels.json.
  - It is compared with the median views of that creator's OTHER judged reels (each one's
    first reading at 3 days or older). Baseline reels are usually older than 3 days when
    read, so they carry more views: the comparison is strict, never generous.
  - Breakout: 3x the median or more. Above usual: 1.5x to 3x. Usual: 0.5x to 1.5x.
    Below: under 0.5x. Fewer than 10 reels to compare with: "not enough history yet".
  - Younger reels are not judged: "too young: X views in Y hours".
  - "Taking off early" needs about two weeks of saved readings: a young reel whose views
    are at least 3x the creator's median at about the same age.
Every label is printed with the median it was compared with and how many reels that came from.

Usage:
  python3 scripts/measure.py              measure, save runs/<run>/measure.json, print the table
  python3 scripts/measure.py --creator NAME
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (data_dir, fmt_age, fmt_int, hours_between, iso, load_config, median, now_utc,  # noqa: E402
                    parse_time, read_json, resolve_run, run_main, runs_dir, write_json)

LABELS = ("Breakout", "Above usual", "Usual", "Below")


def label_for(ratio: float, m: Dict[str, Any]) -> str:
    if ratio >= float(m["breakout"]):
        return "Breakout"
    if ratio >= float(m["above"]):
        return "Above usual"
    if ratio >= float(m["below"]):
        return "Usual"
    return "Below"


def by_key(readings: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for rd in readings:
        out.setdefault(rd["key"], []).append(rd)
    for v in out.values():
        v.sort(key=lambda x: x.get("read_at") or "")
    return out


def settled_reading(rds: List[Dict[str, Any]], judge_h: float) -> Optional[Dict[str, Any]]:
    for rd in rds:
        if rd.get("age_hours") is not None and rd["age_hours"] >= judge_h and rd.get("views") is not None:
            return rd
    return None


def judge(key: str, creator_keys: List[str], rk: Dict[str, List[Dict[str, Any]]], m: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    judge_h = float(m["judge_at_hours"])
    mine = settled_reading(rk.get(key, []), judge_h)
    if not mine:
        return None
    # Only readings that existed when this reel was judged count, so a re-run gives the same answer.
    others = []
    for k in creator_keys:
        if k == key:
            continue
        s = settled_reading(rk.get(k, []), judge_h)
        if s and (s.get("read_at") or "") <= (mine.get("read_at") or ""):
            others.append(s["views"])
    n = len(others)
    out = {"views": mine["views"], "likes": mine.get("likes"), "comments": mine.get("comments"),
           "age_hours": mine["age_hours"], "read_at": mine["read_at"], "compared_with": n}
    if n < int(m["min_history"]):
        out.update({"label": "not enough history yet", "median": median(others) if others else None,
                    "ratio": None})
        return out
    med = median(others)
    if not med:
        out.update({"label": "not enough history yet", "median": med, "ratio": None,
                    "note": "the median is 0 views, so there is nothing to compare with"})
        return out
    ratio = mine["views"] / med
    out.update({"label": label_for(ratio, m), "median": med, "ratio": round(ratio, 2)})
    return out


def early_check(key: str, latest: Dict[str, Any], creator_keys: List[str], rk: Dict[str, List[Dict[str, Any]]],
                m: Dict[str, Any], span_days: float) -> Dict[str, Any]:
    judge_h = float(m["judge_at_hours"])
    need_days = float(m["early_after_days"])
    if span_days < need_days:
        return {"flag": False, "why": "early check starts after %d days of saved readings (%.1f so far)"
                % (need_days, span_days)}
    age = latest.get("age_hours")
    if age is None or latest.get("views") is None:
        return {"flag": False, "why": "no reading to check"}
    window = max(6.0, 0.25 * age)
    peers = []
    for k in creator_keys:
        if k == key:
            continue
        best = None
        for rd in rk.get(k, []):
            a = rd.get("age_hours")
            if a is None or a >= judge_h or rd.get("views") is None or abs(a - age) > window:
                continue
            if best is None or abs(a - age) < abs(best["age_hours"] - age):
                best = rd
        if best:
            peers.append(best["views"])
    if len(peers) < int(m["min_history"]):
        return {"flag": False, "why": "only %d reels with a reading near %s old (need %d)"
                % (len(peers), fmt_age(age), int(m["min_history"])), "compared_with": len(peers)}
    med = median(peers)
    ratio = (latest["views"] / med) if med else None
    flag = bool(ratio is not None and ratio >= float(m["early_multiple"]))
    return {"flag": flag, "median_at_age": med, "compared_with": len(peers), "ratio": round(ratio, 2) if ratio else None,
            "age_hours": age, "views": latest["views"]}


def status_text(item: Dict[str, Any]) -> str:
    lab = item.get("label")
    if lab:
        if lab["label"] in LABELS:
            return "%s: %s views at %s, %.2fx the median of %s (from %d reels)" % (
                lab["label"], fmt_int(lab["views"]), fmt_age(lab["age_hours"]), lab["ratio"],
                fmt_int(int(round(lab["median"]))), lab["compared_with"])
        return "not enough history yet: %s views at %s (only %d other judged reels, need %d)" % (
            fmt_int(lab["views"]), fmt_age(lab["age_hours"]), lab["compared_with"], item["_min"])
    lt = item.get("latest") or {}
    text = "too young: %s views in %s" % (fmt_int(lt.get("views")), fmt_age(lt.get("age_hours")))
    e = item.get("early") or {}
    if e.get("flag"):
        text += "; taking off early: %.2fx the usual %s at that age (from %d reels)" % (
            e["ratio"], fmt_int(int(round(e["median_at_age"]))), e["compared_with"])
    return text


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run")
    ap.add_argument("--creator")
    args = ap.parse_args()
    cfg = load_config()
    m = cfg["measure"]
    run_id = resolve_run(args.run)
    reels = read_json(data_dir() / "reels.json", {})
    readings = read_json(data_dir() / "readings.json", [])
    labels = read_json(data_dir() / "labels.json", {})
    rk = by_key(readings)
    now = now_utc()

    groups: Dict[str, List[str]] = {}
    for k, r in reels.items():
        groups.setdefault("%s:%s" % (r["platform"], r["creator"]), []).append(k)

    out_reels, out_creators = [], []
    newly_frozen = 0
    for g, keys in sorted(groups.items()):
        platform, handle = g.split(":", 1)
        if args.creator and handle != args.creator.lstrip("@").lower():
            continue
        times = [parse_time(rd["read_at"]) for k in keys for rd in rk.get(k, [])]
        times = [t for t in times if t]
        span_days = hours_between(min(times), now) / 24.0 if times else 0.0
        settled = [settled_reading(rk.get(k, []), float(m["judge_at_hours"])) for k in keys]
        settled_views = [s["views"] for s in settled if s]
        out_creators.append({"platform": platform, "handle": handle, "reels": len(keys),
                             "judged_reels": len(settled_views), "median_judged_views": median(settled_views),
                             "readings_span_days": round(span_days, 1)})
        for k in keys:
            r = reels[k]
            if r.get("role") != "new":
                continue
            rds = rk.get(k, [])
            latest = rds[-1] if rds else {}
            item = {"key": k, "platform": platform, "creator": handle, "url": r.get("url"),
                    "posted_at": r.get("posted_at"),
                    "latest": {f: latest.get(f) for f in ("views", "likes", "comments", "age_hours", "read_at")},
                    "readings": len(rds), "label": None, "early": None, "_min": int(m["min_history"])}
            if k in labels:
                item["label"] = labels[k]
            else:
                lab = judge(k, keys, rk, m)
                if lab:
                    lab["measured_at"] = iso(now)
                    labels[k] = lab
                    item["label"] = lab
                    newly_frozen += 1
            if not item["label"]:
                item["early"] = early_check(k, latest, keys, rk, m, span_days)
            item["status"] = status_text(item)
            del item["_min"]
            out_reels.append(item)

    write_json(data_dir() / "labels.json", labels)
    result = {"run": run_id, "measured_at": iso(now), "rule": m, "creators": out_creators, "reels": out_reels}
    write_json(runs_dir() / run_id / "measure.json", result)

    for c in out_creators:
        med = c["median_judged_views"]
        print("\n%s @%s: %d reels saved, %d judged, median of judged reels %s views"
              % (c["platform"], c["handle"], c["reels"], c["judged_reels"],
                 fmt_int(int(round(med))) if med is not None else "n/a"))
        rows = [x for x in out_reels if x["creator"] == c["handle"] and x["platform"] == c["platform"]]
        rows.sort(key=lambda x: x.get("posted_at") or "")
        print("  %-17s %11s %9s %9s %5s  %s" % ("posted (UTC)", "judged at", "views", "median", "n", "label"))
        for x in rows:
            lab = x["label"] or {}
            print("  %-17s %11s %9s %9s %5s  %s" % (
                (x["posted_at"] or "")[:16].replace("T", " "),
                fmt_age(lab.get("age_hours")) if lab else "-",
                fmt_int(lab.get("views")) if lab else fmt_int(x["latest"].get("views")),
                fmt_int(int(round(lab["median"]))) if lab and lab.get("median") is not None else "-",
                lab.get("compared_with", "-") if lab else "-", x["status"]))
    print("\nFroze %d new label(s). Saved %s" % (newly_frozen, runs_dir() / run_id / "measure.json"))
    return 0


if __name__ == "__main__":
    run_main(main)
