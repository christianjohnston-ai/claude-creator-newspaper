#!/usr/bin/env python3
"""Pull new reels and their numbers from Apify, within the monthly budget.

What one run does:
  1. Plans the Apify calls:
     - a creator seen for the first time: their last `baseline_reels` reels (30), once.
       This is the baseline used for their median. Reels from that pull posted inside
       `first_window_days` go into the first paper.
     - every later run: only reels posted since the last run (the actor's date filter).
     - every reel that has turned 3 days old: one re-read by its link (the 3-day reading).
  2. Estimates the cost from config.json's prices BEFORE calling anything, and stops
     if this month's spend plus the estimate would pass budget.monthly_limit_usd.
  3. Runs the calls, saves the raw output in runs/<run>/raw/, and saves every reading
     with the time it was read in data/readings.json.

Usage:
  python3 scripts/fetch.py               plan, check the budget, run
  python3 scripts/fetch.py --dry-run     show the exact requests and the estimate, call nothing
  python3 scripts/fetch.py --budget      show the monthly budget math for config.json
  python3 scripts/fetch.py --import-raw FILE --platform tiktok --kind baseline --read-at ISO
                                         load actor output you already have (no Apify call)
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (UserError, apify_token, data_dir, fmt_age, fmt_int, hours_between, iso,  # noqa: E402
                    load_config, local_date, normalize_handle, now_utc, parse_time, read_json,
                    run_main, runs_dir, to_float, to_int, write_json)
from apify_api import Apify, ApifyError, actor_path_id, build_url, run_params  # noqa: E402

KINDS = ("baseline", "newer", "day3", "videos")
NAMES = {"instagram": "Instagram", "tiktok": "TikTok"}


# ------------------------------------------------------------- state files

def load_state() -> Dict[str, Any]:
    return read_json(data_dir() / "state.json", {"creators": {}, "pending_runs": []})


def load_reels() -> Dict[str, Dict[str, Any]]:
    return read_json(data_dir() / "reels.json", {})


def load_readings() -> List[Dict[str, Any]]:
    return read_json(data_dir() / "readings.json", [])


def load_spend() -> Dict[str, Any]:
    return read_json(data_dir() / "spend.json", {"calls": []})


def creator_key(platform: str, handle: str) -> str:
    return "%s:%s" % (platform, handle)


# ----------------------------------------------------------- normalizers
# Field names are the ones the actors' own docs list (checked 2026-11-10).

def norm_instagram(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """apify/instagram-reel-scraper item -> our reel + reading fields."""
    code = item.get("shortCode")
    if not code:
        return None
    plays = to_int(item.get("videoPlayCount"))
    views = to_int(item.get("videoViewCount"))
    # Instagram's app shows plays as "views" now, so plays are used when present.
    use_plays = plays is not None
    return {
        "platform": "instagram",
        "id": str(code),
        "url": item.get("url") or "https://www.instagram.com/reel/%s/" % code,
        "creator": normalize_handle(str(item.get("ownerUsername") or "")),
        "posted_at": iso(parse_time(item.get("timestamp"))),
        "caption": item.get("caption") or "",
        "duration_s": to_float(item.get("videoDuration")),
        "video_url": item.get("videoUrl") or None,
        "is_video": bool(item.get("videoUrl")) or item.get("type") in (None, "Video"),
        "pinned": bool(item.get("isPinned")),
        "views": plays if use_plays else views,
        "views_field": "videoPlayCount" if use_plays else "videoViewCount",
        "likes": to_int(item.get("likesCount")),
        "comments": to_int(item.get("commentsCount")),
    }


def norm_tiktok(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """clockworks/tiktok-scraper item -> our reel + reading fields."""
    vid = item.get("id")
    if not vid:
        return None
    author = (item.get("authorMeta") or {}).get("name") or ""
    meta = item.get("videoMeta") or {}
    media = [u for u in (item.get("mediaUrls") or []) if isinstance(u, str)]
    video = next((u for u in media if ".mp4" in u.lower()), media[0] if media else None)
    posted = item.get("createTimeISO") or item.get("createTime")
    return {
        "platform": "tiktok",
        "id": str(vid),
        "url": item.get("webVideoUrl") or "https://www.tiktok.com/@%s/video/%s" % (author, vid),
        "creator": normalize_handle(str(author)),
        "posted_at": iso(parse_time(posted)),
        "caption": item.get("text") or "",
        "duration_s": to_float(meta.get("duration")),
        "video_url": video,
        "is_video": not bool(item.get("isSlideshow")),
        "pinned": bool(item.get("isPinned")),
        "views": to_int(item.get("playCount")),
        "views_field": "playCount",
        "likes": to_int(item.get("diggCount")),
        "comments": to_int(item.get("commentCount")),
    }


NORMALIZERS = {"instagram": norm_instagram, "tiktok": norm_tiktok}


def item_problem(item: Dict[str, Any]) -> Optional[str]:
    for k in ("error", "errorDescription", "errorMessage"):
        if item.get(k):
            target = item.get("url") or item.get("inputUrl") or item.get("username") or item.get("input") or ""
            return "%s %s" % (str(item.get(k))[:200], target)
    return None


# ------------------------------------------------------------- planning

def actor_input(platform: str, kind: str, handles: List[str], urls: List[str],
                limit: int, since_iso: Optional[str]) -> Dict[str, Any]:
    """The exact JSON body sent to the actor (input field names from the actor docs)."""
    if platform == "instagram":
        if kind in ("day3", "videos"):
            return {"username": urls, "skipPinnedPosts": True}
        body: Dict[str, Any] = {"username": handles, "resultsLimit": limit, "skipPinnedPosts": True}
        if kind == "newer" and since_iso:
            body["onlyPostsNewerThan"] = since_iso[:19]  # ISO without zone, read as UTC by the actor
        return body
    # TikTok
    if kind in ("day3", "videos"):
        body = {"postURLs": urls}
        if kind == "videos":
            body["shouldDownloadVideos"] = True
        return body
    body = {"profiles": handles, "resultsPerPage": limit, "profileSorting": "latest",
            "excludePinnedPosts": True}
    if kind == "newer":
        if since_iso:
            body["oldestPostDateUnified"] = since_iso[:10]  # a date; extra results are de-duplicated
        body["shouldDownloadVideos"] = True
    return body


def price_per_item(prices: Dict[str, Any], platform: str, kind: str) -> float:
    p = prices[platform]
    if platform == "instagram":
        return float(p["per_reel"])
    each = float(p["per_result"])
    if kind == "newer":
        each += float(p.get("date_filter_per_result", 0)) + float(p.get("video_download_per_video", 0))
    if kind == "videos":
        each += float(p.get("video_download_per_video", 0))
    return each


def posts_per_day(reels: Dict[str, Dict[str, Any]], platform: str, handle: str, now) -> float:
    cutoff = 30 * 24.0
    times = [parse_time(r.get("posted_at")) for r in reels.values()
             if r["platform"] == platform and r["creator"] == handle and r.get("posted_at")]
    recent = [t for t in times if t and hours_between(t, now) <= cutoff]
    return len(recent) / 30.0


def plan(cfg: Dict[str, Any], state: Dict[str, Any], reels: Dict[str, Dict[str, Any]],
         readings: List[Dict[str, Any]], now) -> List[Dict[str, Any]]:
    prices = cfg["prices_usd"]
    judge_h = float(cfg["measure"]["judge_at_hours"])
    cap = int(cfg["max_new_per_creator_per_run"])
    calls: List[Dict[str, Any]] = []
    read_at_age: Dict[str, List[float]] = {}
    for rd in readings:
        if rd.get("age_hours") is not None:
            read_at_age.setdefault(rd["key"], []).append(rd["age_hours"])

    for platform in ("instagram", "tiktok"):
        creators = [c["handle"] for c in cfg["creators"] if c["platform"] == platform]
        cs = state.get("creators", {})
        fresh = [h for h in creators if not cs.get(creator_key(platform, h), {}).get("baseline_done_at")]
        known = [h for h in creators if h not in fresh]
        start = float(prices[platform]["per_run_start"])

        if fresh:
            n = len(fresh) * int(cfg["baseline_reels"])
            each = price_per_item(prices, platform, "baseline")
            calls.append({"platform": platform, "kind": "baseline", "handles": fresh, "urls": [],
                          "limit": int(cfg["baseline_reels"]), "since": None,
                          "expected_items": n, "max_items": n,
                          "estimate_usd": round(n * each + start, 4), "max_usd": round(n * each + start, 4)})
        if known:
            lasts = [parse_time(cs[creator_key(platform, h)].get("last_fetch_at")) for h in known]
            lasts = [t for t in lasts if t]
            since = min(lasts) if lasts else now
            # One hour of overlap so a reel posted during the last run is not missed.
            since_iso = iso(since - datetime.timedelta(hours=1))
            days = max(hours_between(since, now) / 24.0, 1.0 / 24)
            expected = 0
            for h in known:
                rate = posts_per_day(reels, platform, h, now)
                expected += min(cap, max(1, int(math.ceil(rate * days)))) if rate > 0 else 1
            each = price_per_item(prices, platform, "newer")
            mx = len(known) * cap
            calls.append({"platform": platform, "kind": "newer", "handles": known, "urls": [],
                          "limit": cap, "since": since_iso, "expected_items": expected, "max_items": mx,
                          "estimate_usd": round(expected * each + start, 4),
                          "max_usd": round(mx * each + start, 4)})

        # Reels that have turned 3 days old and have no 3-day reading yet.
        due = []
        for key, r in reels.items():
            if r["platform"] != platform or r.get("role") != "new":
                continue
            posted = parse_time(r.get("posted_at"))
            if not posted or hours_between(posted, now) < judge_h:
                continue
            if any(a >= judge_h for a in read_at_age.get(key, [])):
                continue
            due.append(r["url"])
        if due:
            each = price_per_item(prices, platform, "day3")
            calls.append({"platform": platform, "kind": "day3", "handles": [], "urls": due,
                          "limit": 0, "since": None, "expected_items": len(due), "max_items": len(due),
                          "estimate_usd": round(len(due) * each + start, 4),
                          "max_usd": round(len(due) * each + start, 4)})
    for c in calls:
        c["actor"] = prices[c["platform"]]["actor"]
        c["input"] = actor_input(c["platform"], c["kind"], c["handles"], c["urls"], c["limit"], c["since"])
    return calls


def tiktok_video_call(cfg: Dict[str, Any], urls: List[str]) -> Dict[str, Any]:
    """After a TikTok baseline: one more call that downloads only the window reels' videos
    (the baseline itself does not pay to download 30 videos per creator)."""
    prices = cfg["prices_usd"]
    each = price_per_item(prices, "tiktok", "videos")
    start = float(prices["tiktok"]["per_run_start"])
    c = {"platform": "tiktok", "kind": "videos", "handles": [], "urls": urls, "limit": 0, "since": None,
         "expected_items": len(urls), "max_items": len(urls),
         "estimate_usd": round(len(urls) * each + start, 4), "max_usd": round(len(urls) * each + start, 4),
         "actor": prices["tiktok"]["actor"]}
    c["input"] = actor_input("tiktok", "videos", [], urls, 0, None)
    return c


# --------------------------------------------------------------- budget

def month_spent_local(spend: Dict[str, Any], now) -> float:
    month = now.strftime("%Y-%m")
    total = 0.0
    for c in spend.get("calls", []):
        if str(c.get("at", "")).startswith(month):
            total += float(c.get("charged_usd") if c.get("charged_usd") is not None else c.get("estimate_usd", 0))
    return round(total, 4)


def budget_math(cfg: Dict[str, Any], posts_per_day_assumed: float) -> str:
    prices = cfg["prices_usd"]
    limit = float(cfg["budget"]["monthly_limit_usd"])
    base = int(cfg["baseline_reels"])
    lines = ["Budget math (prices from config.json, checked on %s; check apify.com for today's prices)"
             % prices.get("checked_on", "?"),
             "Monthly limit: $%.2f" % limit,
             "Assumption: each creator posts %.1f reels a day; each reel is read twice "
             "(when new, then at 3 days); a new creator's last %d reels are read once." % (posts_per_day_assumed, base)]
    for platform in ("instagram", "tiktok"):
        p = prices[platform]
        first = price_per_item(prices, platform, "baseline")
        newer = price_per_item(prices, platform, "newer")
        day3 = price_per_item(prices, platform, "day3")
        reads_a_month = limit / first
        per_month_reels = 30 * posts_per_day_assumed
        starts = 30 * 2 * float(p["per_run_start"])  # about two calls a night
        ongoing = per_month_reels * (newer + day3)
        first_month = ongoing + base * first
        fit_first = int((limit - starts) // first_month) if first_month > 0 else 0
        fit_after = int((limit - starts) // ongoing) if ongoing > 0 else 0
        lines.append("")
        lines.append("%s (%s)" % (NAMES[platform], p["actor"]))
        lines.append("  $%.4f per reel read, so $%.2f buys about %s reel readings a month (about %s a night)"
                     % (first, limit, fmt_int(int(reads_a_month)), fmt_int(int(reads_a_month / 30))))
        if platform == "tiktok":
            lines.append("  nightly reads of new reels cost $%.4f each (result + date filter + video download)" % newer)
        lines.append("  one creator costs about $%.2f in its first month and $%.2f a month after that"
                     % (first_month, ongoing))
        lines.append("  so $%.2f covers about %d creators in the first month, %d after that "
                     "(if all your creators are on %s)" % (limit, fit_first, fit_after, NAMES[platform]))
    return "\n".join(lines)


# ------------------------------------------------------------- applying

def apply_items(platform: str, kind: str, items: List[Dict[str, Any]], read_at, run_id: str,
                reels: Dict[str, Dict[str, Any]], readings: List[Dict[str, Any]],
                cfg: Dict[str, Any], requested_handles: List[str]) -> Dict[str, Any]:
    norm = NORMALIZERS[platform]
    window_h = float(cfg["first_window_days"]) * 24.0
    summary = {"items": len(items), "new": [], "updated": [], "problems": [], "by_creator": {}}
    for item in items:
        prob = item_problem(item)
        if prob:
            summary["problems"].append("%s: %s" % (platform, prob))
            continue
        n = norm(item)
        if not n:
            continue
        if n["pinned"] and kind in ("baseline", "newer"):
            continue
        key = "%s:%s" % (platform, n["id"])
        posted = parse_time(n["posted_at"])
        age = hours_between(posted, read_at) if posted else None
        r = reels.get(key)
        if r is None:
            if kind == "baseline":
                role = "new" if (age is not None and age <= window_h) else "baseline"
            else:
                role = "new"
            r = {"key": key, "platform": platform, "id": n["id"], "url": n["url"],
                 "creator": n["creator"], "posted_at": n["posted_at"], "caption": n["caption"],
                 "duration_s": n["duration_s"], "is_video": n["is_video"], "role": role,
                 "first_seen_at": iso(read_at), "first_seen_run": run_id, "views_field": n["views_field"],
                 "video_url": None, "video_url_read_at": None}
            reels[key] = r
            summary["new"].append(key)
        else:
            summary["updated"].append(key)
            for f in ("caption", "duration_s", "url"):
                if n.get(f) and not r.get(f):
                    r[f] = n[f]
            if n["creator"] and not r.get("creator"):
                r["creator"] = n["creator"]
        if n.get("video_url"):
            r["video_url"] = n["video_url"]
            r["video_url_read_at"] = iso(read_at)
        readings.append({"key": key, "read_at": iso(read_at),
                         "age_hours": round(age, 2) if age is not None else None,
                         "views": n["views"], "likes": n["likes"], "comments": n["comments"],
                         "run": run_id, "kind": kind})
        summary["by_creator"].setdefault(r["creator"], 0)
        summary["by_creator"][r["creator"]] += 1
    for h in requested_handles:
        if not summary["by_creator"].get(h):
            summary["problems"].append(
                "%s @%s: no reels came back (the account may be private, renamed, have no reels in the "
                "time asked for, or the scraper was blocked)" % (platform, h))
    return summary


def new_run_id(now) -> str:
    base = local_date(now)
    rid, i = base, 2
    while (runs_dir() / rid / "fetch.json").exists():
        rid = "%s_%d" % (base, i)
        i += 1
    return rid


def print_table(keys: List[str], reels: Dict[str, Dict[str, Any]], readings: List[Dict[str, Any]], now) -> None:
    latest: Dict[str, Dict[str, Any]] = {}
    for rd in readings:
        latest[rd["key"]] = rd
    rows = sorted((reels[k] for k in keys if k in reels), key=lambda r: (r["creator"], r.get("posted_at") or ""))
    if not rows:
        print("  (no reels)")
        return
    print("  %-10s %-24s %-17s %10s %10s %8s %8s  %s" % ("platform", "creator", "posted (UTC)", "age",
                                                        "views", "likes", "comments", "caption"))
    for r in rows:
        rd = latest.get(r["key"], {})
        cap = (r.get("caption") or "").strip().splitlines()
        print("  %-10s %-24s %-17s %10s %10s %8s %8s  %s" % (
            r["platform"], "@" + r["creator"][:23], (r.get("posted_at") or "")[:16].replace("T", " "),
            fmt_age(rd.get("age_hours")), fmt_int(rd.get("views")), fmt_int(rd.get("likes")),
            fmt_int(rd.get("comments")), (cap[0] if cap else "")[:50]))


def describe_call(c: Dict[str, Any], max_charge: Optional[float]) -> str:
    url = build_url("/acts/%s/runs" % actor_path_id(c["actor"]), run_params(max_charge))
    return ("POST %s\n  headers: Content-Type: application/json, Authorization: Bearer <your token, hidden>\n"
            "  body: %s\n  expected items: %d (at most %d), estimate $%.4f (at most $%.4f)"
            % (url, json.dumps(c["input"]), c["expected_items"], c["max_items"], c["estimate_usd"], c["max_usd"]))


# ----------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="show the plan and the exact requests, call nothing")
    ap.add_argument("--budget", action="store_true", help="show the monthly budget math")
    ap.add_argument("--posts-per-day", type=float, default=1.5, help="assumption for --budget")
    ap.add_argument("--max-usd", type=float, default=None, help="spend at most this much on this run")
    ap.add_argument("--import-raw", help="actor output JSON file to load instead of calling Apify")
    ap.add_argument("--platform", choices=("instagram", "tiktok"))
    ap.add_argument("--kind", choices=KINDS, default="baseline")
    ap.add_argument("--read-at", help="ISO time the imported items were read (default: now)")
    ap.add_argument("--if-older-than", type=float, default=None, metavar="HOURS",
                    help="skip when every creator was fetched less than HOURS ago (the night shift ran)")
    args = ap.parse_args()

    cfg = load_config()
    if args.budget:
        print(budget_math(cfg, args.posts_per_day))
        return 0

    now = now_utc()
    state, reels, readings, spend = load_state(), load_reels(), load_readings(), load_spend()

    if args.import_raw:
        return import_raw(args, cfg, state, reels, readings, now)

    if not cfg["creators"]:
        raise UserError("config.json has no creators yet.")
    if args.if_older_than is not None:
        cs = state.get("creators", {})
        lasts = [parse_time(cs.get(creator_key(c["platform"], c["handle"]), {}).get("last_fetch_at"))
                 for c in cfg["creators"]]
        if lasts and all(lasts):
            age = hours_between(min(lasts), now)
            if age < args.if_older_than:
                print("Skipped: every creator was fetched %.1f hours ago (under %.0f). Nothing was charged."
                      % (age, args.if_older_than))
                return 0
    calls = plan(cfg, state, reels, readings, now)
    limit = float(cfg["budget"]["monthly_limit_usd"])
    spent = month_spent_local(spend, now)
    expected = round(sum(c["estimate_usd"] for c in calls), 4)
    worst = round(sum(c["max_usd"] for c in calls), 4)
    # A TikTok baseline is followed by a video call for its window reels; budget for it up front.
    for c in calls:
        if c["platform"] == "tiktok" and c["kind"] == "baseline":
            guess = len(c["handles"]) * min(int(cfg["baseline_reels"]), 3 * int(cfg["first_window_days"]))
            extra = tiktok_video_call(cfg, ["x"] * guess)
            expected = round(expected + extra["estimate_usd"], 4)
            worst = round(worst + extra["max_usd"], 4)

    if args.dry_run:
        print("DRY RUN: nothing is sent to Apify.")
        print("This month so far (from data/spend.json): $%.4f of your $%.2f limit." % (spent, limit))
        for i, c in enumerate(calls, 1):
            print("\nCall %d: %s %s" % (i, NAMES[c["platform"]], c["kind"]))
            print(describe_call(c, max(0.0, limit - spent)))
            if c["platform"] == "tiktok" and c["kind"] == "baseline":
                print("  then: one more TikTok call with postURLs = the reels from your first window and "
                      "shouldDownloadVideos = true, so only those videos are paid for and downloaded")
        print("\nEstimated cost of this run: $%.4f (at most $%.4f)." % (expected, worst))
        if expected > limit - spent + 1e-9:
            print("This run would be REFUSED: it would pass your monthly limit.")
        if not calls:
            print("Nothing to fetch right now.")
        return 0

    if not calls:
        print("Nothing to fetch right now (no creators due, no reels turning 3 days old).")
        return 0

    def refuse(room_left: float) -> int:
        print("NOT RUNNING: this run would cost about $%.4f and only $%.4f is left under your $%.2f "
              "monthly limit (spent so far: $%.4f).\nTo go ahead: raise budget.monthly_limit_usd in "
              "config.json, remove creators, or lower max_new_per_creator_per_run."
              % (expected, max(room_left, 0), limit, spent), file=sys.stderr)
        return 3

    room = limit - spent
    if args.max_usd is not None:
        room = min(room, args.max_usd)
    if expected > room + 1e-9:  # refuse before anything is sent
        return refuse(room)

    token = apify_token()
    api = Apify(token)
    try:
        lim = api.limits()
        apify_used = float((lim.get("current") or {}).get("monthlyUsageUsd") or 0.0)
    except ApifyError as exc:
        raise UserError(exc.plain())
    # Apify's own count includes anything else this account ran this month.
    spent = max(spent, apify_used)
    room = limit - spent
    if args.max_usd is not None:
        room = min(room, args.max_usd)
    print("Estimated cost of this run: $%.4f (at most $%.4f). Spent this month: $%.4f of $%.2f."
          % (expected, worst, spent, limit))
    if expected > room + 1e-9:
        return refuse(room)

    run_id = new_run_id(now)
    run_dir = runs_dir() / run_id
    (run_dir / "raw").mkdir(parents=True, exist_ok=True)
    summary = {"run": run_id, "started_at": iso(now), "calls": [], "new_keys": [], "day3_keys": [],
               "problems": [], "estimate_usd": expected, "imported": False}
    queue = list(calls)
    while queue:
        c = queue.pop(0)
        cap_usd = max(0.0, min(room, c["max_usd"]))
        print("\n%s %s: %d item(s) expected, estimate $%.4f" % (c["platform"], c["kind"], c["expected_items"],
                                                              c["estimate_usd"]))
        read_at = now_utc()
        try:
            run, items = api.run_actor(c["actor"], c["input"], cap_usd,
                                       on_wait=lambda r: print("  waiting, status %s" % r.get("status")))
        except ApifyError as exc:
            summary["problems"].append("%s %s call failed: %s" % (c["platform"], c["kind"], exc.plain()))
            print("  FAILED: %s" % exc.plain())
            continue
        name = "%s_%s" % (c["platform"], c["kind"])
        write_json(run_dir / "raw" / (name + ".json"), items)
        write_json(run_dir / "raw" / (name + ".run.json"),
                   {k: run.get(k) for k in ("id", "status", "startedAt", "finishedAt", "usageTotalUsd",
                                            "chargedEventCounts", "defaultDatasetId", "statusMessage")})
        charged = to_float(run.get("usageTotalUsd"))
        spend.setdefault("calls", []).append({"at": iso(read_at), "run": run_id, "platform": c["platform"],
                                              "kind": c["kind"], "apify_run_id": run.get("id"),
                                              "estimate_usd": c["estimate_usd"], "charged_usd": charged,
                                              "events": run.get("chargedEventCounts")})
        room -= charged if charged is not None else c["estimate_usd"]
        if run.get("status") != "SUCCEEDED":
            summary["problems"].append("%s %s run ended %s: %s" % (c["platform"], c["kind"], run.get("status"),
                                                                  run.get("statusMessage") or ""))
        s = apply_items(c["platform"], c["kind"], items, read_at, run_id, reels, readings, cfg, c["handles"])
        summary["problems"].extend(s["problems"])
        summary["new_keys"].extend(k for k in s["new"] if reels[k]["role"] == "new")
        if c["kind"] == "day3":
            summary["day3_keys"].extend(s["new"] + s["updated"])
        summary["calls"].append({"platform": c["platform"], "kind": c["kind"], "items": len(items),
                                 "apify_run_id": run.get("id"), "status": run.get("status"),
                                 "estimate_usd": c["estimate_usd"], "charged_usd": charged})
        if c["kind"] in ("baseline", "newer") and run.get("status") == "SUCCEEDED":
            for h in c["handles"]:
                ck = state.setdefault("creators", {}).setdefault(creator_key(c["platform"], h), {})
                if c["kind"] == "baseline":
                    ck["baseline_done_at"] = iso(read_at)
                ck["last_fetch_at"] = iso(read_at)
        if c["platform"] == "tiktok" and c["kind"] == "baseline":
            want = [reels[k]["url"] for k in s["new"] if reels[k]["role"] == "new" and reels[k].get("is_video")]
            if want:
                queue.insert(0, tiktok_video_call(cfg, want))
        write_json(data_dir() / "reels.json", reels)
        write_json(data_dir() / "readings.json", readings)
        write_json(data_dir() / "spend.json", spend)
        write_json(data_dir() / "state.json", state)

    return finish(summary, run_dir, state, reels, readings, spend, now)


def finish(summary, run_dir, state, reels, readings, spend, now) -> int:
    summary["finished_at"] = iso(now_utc())
    summary["charged_usd"] = round(sum((c.get("charged_usd") or 0.0) for c in summary["calls"]), 4)
    summary["new_keys"] = sorted(set(summary["new_keys"]))
    summary["day3_keys"] = sorted(set(summary["day3_keys"]))
    write_json(run_dir / "fetch.json", summary)
    pend = state.setdefault("pending_runs", [])
    if summary["run"] not in pend:
        pend.append(summary["run"])
    write_json(data_dir() / "state.json", state)
    print("\nNew reels for the paper: %d" % len(summary["new_keys"]))
    print_table(summary["new_keys"], reels, readings, now)
    if summary["day3_keys"]:
        print("\n3-day readings taken: %d" % len(summary["day3_keys"]))
        print_table(summary["day3_keys"], reels, readings, now)
    for p in summary["problems"]:
        print("PROBLEM: %s" % p)
    if not summary.get("imported"):
        print("\nCost of this run: $%.4f charged by Apify (estimate was $%.4f)."
              % (summary["charged_usd"], summary["estimate_usd"]))
    print("Saved: %s" % (run_dir / "fetch.json"))
    return 0


def import_raw(args, cfg, state, reels, readings, now) -> int:
    if not args.platform:
        raise UserError("--import-raw needs --platform instagram or tiktok.")
    items = read_json(Path(args.import_raw).expanduser(), None)
    if not isinstance(items, list):
        raise UserError("%s is not a JSON list of actor items." % args.import_raw)
    read_at = parse_time(args.read_at) if args.read_at else now
    if read_at is None:
        raise UserError("--read-at %r is not a time." % args.read_at)
    run_id = new_run_id(now)
    run_dir = runs_dir() / run_id
    (run_dir / "raw").mkdir(parents=True, exist_ok=True)
    write_json(run_dir / "raw" / ("%s_%s.imported.json" % (args.platform, args.kind)), items)
    handles = [c["handle"] for c in cfg["creators"] if c["platform"] == args.platform] \
        if args.kind in ("baseline", "newer") else []
    s = apply_items(args.platform, args.kind, items, read_at, run_id, reels, readings, cfg, handles)
    if args.kind in ("baseline", "newer"):
        for h in handles:
            if s["by_creator"].get(h):
                ck = state.setdefault("creators", {}).setdefault(creator_key(args.platform, h), {})
                if args.kind == "baseline":
                    ck["baseline_done_at"] = iso(read_at)
                ck["last_fetch_at"] = iso(read_at)
    write_json(data_dir() / "reels.json", reels)
    write_json(data_dir() / "readings.json", readings)
    summary = {"run": run_id, "started_at": iso(now), "imported": True, "calls": [
        {"platform": args.platform, "kind": args.kind, "items": len(items), "source": str(args.import_raw)}],
        "new_keys": [k for k in s["new"] if reels[k]["role"] == "new"],
        "day3_keys": (s["new"] + s["updated"]) if args.kind == "day3" else [],
        "problems": s["problems"], "estimate_usd": 0.0}
    return finish(summary, run_dir, state, reels, readings, {}, read_at)


if __name__ == "__main__":
    run_main(main)
