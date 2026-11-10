#!/usr/bin/env python3
"""Get everything Claude needs to write the paper, and a pre-filled editorial file.

Prints, for every reel in the coming edition: its numbers and label, the start of its
transcript, and the paths of its contact sheet, tile map and hook frame (Claude opens
those images to see the reel). Writes runs/<run>/editorial_template.json with one entry
for each Breakout and each watched reel, so nothing is forgotten.

Usage:
  python3 scripts/brief.py            print the brief, write the template
  python3 scripts/brief.py --full     print whole transcripts
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import fmt_age, fmt_int, home, load_config, resolve_run, run_main, runs_dir, write_json  # noqa: E402
from render import NAMES, select_edition  # noqa: E402
from measure import status_text  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run")
    ap.add_argument("--full", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    run_id = resolve_run(args.run)
    ed = select_edition(cfg)
    print("NEXT EDITION: %d new reels, %d breakouts, %d earlier reels with a new label"
          % (len(ed["new_keys"]), len(ed["breakouts"]), len(ed["since_keys"])))
    print("Niche: %s. What the reader wants to learn: %s." % (cfg.get("niche") or "?", cfg.get("what_to_learn") or "?"))
    if ed["breakouts"]:
        print("Breakouts (each needs hook_quote + why_it_worked): %s" % ", ".join(ed["breakouts"]))
    template = {"headline": "", "lead_story": "", "lead_reel_keys": [],
                "breakouts": [], "themes": [], "reel_notes": {}, "since_last": []}
    for k in ed["keys"]:
        r = ed["reels"][k]
        w = ed["watch"].get(k) or {}
        lab = ed["labels"].get(k)
        lt = ed["latest"].get(k, {})
        item = {"label": lab, "latest": lt, "early": None, "_min": int(cfg["measure"]["min_history"])}
        print("\n== %s  @%s on %s  %s" % (k, r["creator"], NAMES[r["platform"]], r["url"]))
        print("   posted %s, latest reading %s views, %s likes, %s comments at %s"
              % ((r.get("posted_at") or "")[:16], fmt_int(lt.get("views")), fmt_int(lt.get("likes")),
                 fmt_int(lt.get("comments")), fmt_age(lt.get("age_hours"))))
        print("   %s" % status_text(item))
        where = []
        if k in ed["new_keys"]:
            where.append("new reel")
        if k in ed["breakouts"]:
            where.append("BREAKOUT")
        if k in ed["since_keys"]:
            where.append("shown before, label arrived since")
        print("   in this edition as: %s" % ", ".join(where))
        cap = (r.get("caption") or "").strip().replace("\n", " ")
        print("   caption: %s" % (cap[:300] or "(none)"))
        text = w.get("_transcript_text", "")
        if text:
            words = text.split()
            print("   transcript (%d words): %s%s" % (len(words), " ".join(words if args.full else words[:90]),
                                                    "" if args.full or len(words) <= 90 else " ..."))
        elif w:
            print("   transcript: none (%s)" % "; ".join(w.get("problems") or ["no speech"]))
        if w.get("contact_sheet"):
            print("   LOOK AT: %s" % (home() / w["contact_sheet"]))
            if w.get("contact_sheet_map"):
                print("   tile map: %s" % (home() / w["contact_sheet_map"]))
            if w.get("hero_frame"):
                print("   hook frame (full size): %s" % (home() / w["hero_frame"]))
        elif r.get("is_video", True) and k in ed["new_keys"]:
            print("   NOT WATCHED: %s" % "; ".join((r.get("watch") or {}).get("problems") or ["run watch.py"]))
        if k in ed["breakouts"]:
            first = " ".join(text.split()[:14]) if text else ""
            template["breakouts"].append({"key": k, "hook_quote": first, "why_it_worked": ""})
        if k in ed["new_keys"] and w.get("status") == "done":
            template["reel_notes"][k] = {"hook": "", "on_screen_text": "", "format": "", "visuals": "", "cta": ""}
    out = runs_dir() / run_id / "editorial_template.json"
    write_json(out, template)
    print("\nTemplate written: %s" % out)
    print("Copy it to %s, fill it in, then run: python3 scripts/render.py --check"
          % (runs_dir() / run_id / "editorial.json"))
    return 0


if __name__ == "__main__":
    run_main(main)
