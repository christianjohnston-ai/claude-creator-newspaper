#!/usr/bin/env python3
"""Print the newspaper: one self-contained offline HTML file per edition.

Claude writes the words (headline, lead story, why each breakout worked, themes, notes on
each reel) into editorial.json after reading the transcripts and contact sheets. This
script checks that file against the saved data and refuses to print when:
  - a number in the text does not match the saved readings, labels, transcripts or captions
  - a quoted hook is not word for word in that reel's transcript or caption
  - a breakout, or a watched reel, has no entry
  - a theme does not name reels from two or more creators
  - any text has a long dash (U+2014 or U+2013)
Then it writes editions/<date>.html and records what was shown in data/history.json,
so nothing repeats in a later edition unless something new happened to it.

Usage:
  python3 scripts/render.py --check       check editorial.json, write nothing
  python3 scripts/render.py               check, then print the edition
  python3 scripts/render.py --editorial PATH
"""
from __future__ import annotations

import argparse
import base64
import datetime as _dt
import html
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (LONG_DASHES, UserError, data_dir, editions_dir, fmt_age, fmt_int, home, iso,  # noqa: E402
                    load_config, now_utc, parse_time, read_json, resolve_run, run_main, runs_dir,
                    safe_name, strip_long_dashes, write_json)

LABELED = ("Breakout", "Above usual", "Usual", "Below")
NAMES = {"instagram": "Instagram", "tiktok": "TikTok"}


# ------------------------------------------------------------ the edition

def load_history() -> Dict[str, Any]:
    return read_json(data_dir() / "history.json", {"editions": [], "shown": {}})


def watch_info(reel: Dict[str, Any]) -> Dict[str, Any]:
    w = reel.get("watch") or {}
    if not w.get("dir"):
        return {}
    info = read_json(home() / w["dir"] / "watch.json", {}) or {}
    t = {}
    if info.get("transcript", {}).get("path"):
        t = read_json(home() / info["transcript"]["path"], {}) or {}
    info["_transcript_text"] = t.get("text", "")
    info["_transcript_segments"] = t.get("segments", [])
    return info


def select_edition(cfg: Dict[str, Any]) -> Dict[str, Any]:
    reels = read_json(data_dir() / "reels.json", {})
    readings = read_json(data_dir() / "readings.json", [])
    labels = read_json(data_dir() / "labels.json", {})
    hist = load_history()
    shown = hist.get("shown", {})
    latest: Dict[str, Dict[str, Any]] = {}
    all_reads: Dict[str, List[Dict[str, Any]]] = {}
    for rd in sorted(readings, key=lambda x: x.get("read_at") or ""):
        latest[rd["key"]] = rd
        all_reads.setdefault(rd["key"], []).append(rd)

    new_keys = sorted((k for k, r in reels.items() if r.get("role") == "new" and not shown.get(k, {}).get("new_in")),
                      key=lambda k: (reels[k]["creator"], reels[k].get("posted_at") or ""))
    label_news = [k for k, lab in labels.items() if k in reels and not shown.get(k, {}).get("label_in")]
    breakouts = sorted((k for k in label_news if labels[k]["label"] == "Breakout"),
                       key=lambda k: -(labels[k].get("ratio") or 0))
    since = sorted((k for k in label_news if shown.get(k, {}).get("new_in")),
                   key=lambda k: -(labels[k].get("ratio") or 0))
    keys = sorted(set(new_keys) | set(breakouts) | set(since))
    info = {k: watch_info(reels[k]) for k in keys}
    return {"reels": reels, "latest": latest, "reads": all_reads, "labels": labels, "history": hist,
            "new_keys": new_keys, "breakouts": breakouts, "since_keys": since, "keys": keys, "watch": info}


# ----------------------------------------------------- number checking

NUM_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(\s?[kKmM](?![a-zA-Z])|%|x(?![a-zA-Z]))?")


def numbers_in(text: str) -> List[Tuple[str, float, str, int]]:
    """(token, value, suffix, decimals) for each number written in text."""
    out = []
    for m in NUM_RE.finditer(text or ""):
        tok, suf = m.group(1), (m.group(2) or "").strip().lower()
        dec = len(tok.split(".")[1]) if "." in tok else 0
        out.append((m.group(0), float(tok.replace(",", "")), suf, dec))
    return out


class Allowed:
    """Values a piece of text may cite. Integers must match exactly (or as 1.2K / 3M);
    measured decimals (ratios, ages, medians, lengths) may be rounded."""

    def __init__(self):
        self.exact: Set[float] = set()
        self.rounded: Set[float] = set()

    def add_exact(self, v):
        if v is not None:
            self.exact.add(float(v))

    def add_rounded(self, v):
        if v is not None:
            self.rounded.add(float(v))

    def merge(self, other: "Allowed") -> "Allowed":
        a = Allowed()
        a.exact = self.exact | other.exact
        a.rounded = self.rounded | other.rounded
        return a

    def ok(self, value: float, suffix: str, dec: int) -> bool:
        scale = {"k": 1e3, "m": 1e6}.get(suffix, 1.0)
        pool = list(self.exact) + list(self.rounded)
        if scale > 1:
            return any(round(a / scale, dec) == value for a in pool)
        if suffix == "%":
            return any(round(a * 100, dec) == value for a in self.rounded) or value in self.exact
        if value in self.exact:
            return True
        return any(round(a, dec) == value for a in self.rounded)


def text_numbers(text: str, allowed: Allowed) -> None:
    for _tok, val, suf, dec in numbers_in(text):
        allowed.add_exact(val)
        if suf == "%":
            allowed.add_rounded(val / 100.0)


def reel_allowed(k: str, ed: Dict[str, Any]) -> Allowed:
    a = Allowed()
    r = ed["reels"][k]
    for rd in ed["reads"].get(k, []):
        for f in ("views", "likes", "comments"):
            a.add_exact(rd.get(f))
        if rd.get("age_hours") is not None:
            a.add_rounded(rd["age_hours"])
            a.add_rounded(rd["age_hours"] / 24.0)
    lab = ed["labels"].get(k)
    if lab:
        for f in ("views", "likes", "comments", "compared_with"):
            a.add_exact(lab.get(f))
        for f in ("ratio", "median"):
            a.add_rounded(lab.get(f))
        if lab.get("age_hours") is not None:
            a.add_rounded(lab["age_hours"])
            a.add_rounded(lab["age_hours"] / 24.0)
    a.add_rounded(r.get("duration_s"))
    a.add_rounded((ed["watch"].get(k) or {}).get("duration_s"))
    text_numbers(r.get("caption", ""), a)
    text_numbers((ed["watch"].get(k) or {}).get("_transcript_text", ""), a)
    posted = parse_time(r.get("posted_at"))
    if posted:
        loc = posted.astimezone()
        for v in (loc.day, loc.month, loc.year, posted.day, posted.year):
            a.add_exact(v)
    return a


def global_allowed(cfg: Dict[str, Any], ed: Dict[str, Any], edition_no: int, today: _dt.date) -> Allowed:
    a = Allowed()
    m = cfg["measure"]
    for v in (m["breakout"], m["above"], m["below"], m["min_history"], m["judge_at_hours"], m["early_after_days"],
              m["early_multiple"], cfg["baseline_reels"], 2, 3):
        a.add_exact(v)
    creators = {(ed["reels"][k]["platform"], ed["reels"][k]["creator"]) for k in ed["keys"]}
    for v in (len(ed["new_keys"]), len(ed["breakouts"]), len(ed["since_keys"]), len(creators),
              len(cfg["creators"]), edition_no, today.day, today.month, today.year):
        a.add_exact(v)
    return a


# ----------------------------------------------------------- checking

def norm_words(text: str) -> str:
    t = strip_long_dashes(text or "").lower().replace("\u2019", "'").replace("\u2018", "'")
    t = re.sub(r"[^a-z0-9' ]+", " ", t)
    return " ".join(t.split())


def check_editorial(cfg: Dict[str, Any], ed: Dict[str, Any], e: Dict[str, Any], edition_no: int,
                    today: _dt.date) -> List[str]:
    errs: List[str] = []
    if not isinstance(e, dict):
        return ["editorial.json must be a JSON object"]

    def walk(value: Any, where: str):
        if isinstance(value, str):
            for d in LONG_DASHES:
                if d in value:
                    i = value.index(d)
                    errs.append("%s has a long dash near %r; use a comma, a period or a hyphen"
                                % (where, value[max(0, i - 20):i + 20]))
        elif isinstance(value, dict):
            for k2, v2 in value.items():
                walk(v2, "%s.%s" % (where, k2))
        elif isinstance(value, list):
            for i, v2 in enumerate(value):
                walk(v2, "%s[%d]" % (where, i))
    walk(e, "editorial")

    g = global_allowed(cfg, ed, edition_no, today)
    every = g
    for k in ed["keys"]:
        every = every.merge(reel_allowed(k, ed))

    def nums(text: str, allowed: Allowed, where: str):
        for tok, val, suf, dec in numbers_in(text or ""):
            if not allowed.ok(val, suf, dec):
                errs.append("%s: the number %r does not match any saved number for the reels it is about "
                            "(readings, labels, transcript, caption). Fix the number or point it at the right reel."
                            % (where, tok.strip()))

    def known(k: str, where: str) -> bool:
        if k not in ed["keys"]:
            errs.append("%s: reel %r is not in this edition" % (where, k))
            return False
        return True

    if not str(e.get("headline", "")).strip():
        errs.append("headline is empty")
    if not str(e.get("lead_story", "")).strip():
        errs.append("lead_story is empty")
    nums(e.get("headline", ""), every, "headline")
    nums(e.get("lead_story", ""), every, "lead_story")
    for k in e.get("lead_reel_keys", []) or []:
        known(k, "lead_reel_keys")

    got = {}
    for i, b in enumerate(e.get("breakouts", []) or []):
        where = "breakouts[%d]" % i
        k = b.get("key", "")
        if not known(k, where):
            continue
        if k not in ed["breakouts"]:
            errs.append("%s: %s is not labeled Breakout in this edition" % (where, k))
            continue
        got[k] = b
        if not str(b.get("why_it_worked", "")).strip():
            errs.append("%s: why_it_worked is empty" % where)
        sentences = [s for s in re.split(r"(?<=[.!?])\s+", str(b.get("why_it_worked", "")).strip()) if s]
        if len(sentences) > 3:
            errs.append("%s: why_it_worked has %d sentences; keep it to 1 to 3" % (where, len(sentences)))
        nums(b.get("why_it_worked", ""), g.merge(reel_allowed(k, ed)), where + ".why_it_worked")
        quote = norm_words(b.get("hook_quote", ""))
        src = norm_words((ed["watch"].get(k) or {}).get("_transcript_text", "")) + " | " + \
            norm_words(ed["reels"][k].get("caption", ""))
        if not quote:
            errs.append("%s: hook_quote is empty (quote the first spoken words, word for word)" % where)
        elif quote not in src:
            errs.append("%s: hook_quote %r is not word for word in that reel's transcript or caption"
                        % (where, b.get("hook_quote")))
    for k in ed["breakouts"]:
        if k not in got:
            errs.append("breakouts: %s is a Breakout in this edition but has no entry" % k)

    for i, t in enumerate(e.get("themes", []) or []):
        where = "themes[%d]" % i
        ks = [k for k in (t.get("reel_keys") or []) if known(k, where)]
        creators = {(ed["reels"][k]["platform"], ed["reels"][k]["creator"]) for k in ks}
        if len(creators) < 2:
            errs.append("%s (%r): a theme needs reels from two or more creators" % (where, t.get("name")))
        a = g
        for k in ks:
            a = a.merge(reel_allowed(k, ed))
        for f in ("name", "summary"):
            nums(t.get(f, ""), a, "%s.%s" % (where, f))
        for j, ang in enumerate(t.get("angles", []) or []):
            if ang.get("key") not in ks:
                errs.append("%s.angles[%d]: key %r is not in this theme's reel_keys" % (where, j, ang.get("key")))
            nums(ang.get("angle", ""), a, "%s.angles[%d]" % (where, j))
        if t.get("returning"):
            before = {norm_words(x) for ed_ in ed["history"].get("editions", []) for x in ed_.get("themes", [])}
            if norm_words(t.get("name", "")) not in before:
                errs.append("%s: marked returning, but no earlier edition had a theme named %r"
                            % (where, t.get("name")))

    notes = e.get("reel_notes", {}) or {}
    for k, n in notes.items():
        if not known(k, "reel_notes"):
            continue
        a = g.merge(reel_allowed(k, ed))
        for f in ("hook", "format", "visuals", "cta"):
            nums(n.get(f, ""), a, "reel_notes[%s].%s" % (k, f))
    for k in ed["new_keys"]:
        w = ed["watch"].get(k) or {}
        if w.get("status") == "done":
            n = notes.get(k) or {}
            if not str(n.get("hook", "")).strip() or not str(n.get("format", "")).strip():
                errs.append("reel_notes[%s]: this reel was watched; write at least its hook and format" % k)

    for i, s in enumerate(e.get("since_last", []) or []):
        where = "since_last[%d]" % i
        ks = [k for k in (s.get("reel_keys") or []) if known(k, where)]
        a = g
        for k in ks:
            a = a.merge(reel_allowed(k, ed))
        nums(s.get("text", ""), a if ks else every, where)
    return errs


# ----------------------------------------------------------------- HTML

CSS = """
:root{--paper:#f7f3ea;--ink:#1d1b17;--muted:#6b645a;--rule:#cfc6b6;--card:#fffdf8;--accent:#9b2c1c;
--good:#1f6b3a;--warn:#8a5a00;--chip:#efe8da;--serif:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,"Times New Roman",serif;
--sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
@media (prefers-color-scheme:dark){:root{--paper:#17150f;--ink:#ece6da;--muted:#a69d8f;--rule:#3b362c;
--card:#201d16;--accent:#e0775f;--good:#6fc28f;--warn:#e0b25f;--chip:#2b271e}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.55 var(--serif)}
.wrap{max-width:1080px;margin:0 auto;padding:20px 16px 64px}
.mast{text-align:center;border-bottom:3px double var(--ink);padding-bottom:10px}
.mast h1{font-size:clamp(34px,7vw,64px);margin:0;letter-spacing:.5px;font-weight:700}
.dateline{display:flex;flex-wrap:wrap;justify-content:space-between;gap:6px;font:13px/1.4 var(--sans);
color:var(--muted);border-bottom:1px solid var(--ink);padding:6px 0;margin-bottom:22px;text-transform:uppercase;letter-spacing:.6px}
h2.kicker{font:700 12px/1 var(--sans);letter-spacing:1.6px;text-transform:uppercase;color:var(--accent);
margin:36px 0 12px;padding-top:12px;border-top:1px solid var(--rule)}
.headline{font-size:clamp(28px,5vw,46px);line-height:1.12;margin:4px 0 14px;font-weight:700}
.lead{columns:2 300px;column-gap:28px;font-size:18px}.lead p{margin:0 0 12px}
.lead p:first-child::first-letter{float:left;font-size:58px;line-height:.9;padding:4px 8px 0 0;font-weight:700}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}.chip{font:12px/1.2 var(--sans);background:var(--chip);
padding:5px 9px;border-radius:999px;color:var(--ink);text-decoration:none}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px}
.card{background:var(--card);border:1px solid var(--rule);border-radius:10px;padding:14px;min-width:0}
.card h3{margin:0 0 4px;font-size:19px;line-height:1.25}
.meta{font:13px/1.45 var(--sans);color:var(--muted)}
.nums{display:flex;flex-wrap:wrap;gap:12px;font:600 14px/1.3 var(--sans);margin:8px 0}
.nums span b{display:block;font:11px/1 var(--sans);color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-bottom:3px}
.pill{display:inline-block;font:700 12px/1.2 var(--sans);padding:5px 9px;border-radius:6px;border:1px solid currentColor}
.p-Breakout{color:var(--accent)}.p-Above{color:var(--good)}.p-Usual{color:var(--muted)}.p-Below{color:var(--muted)}
.p-young{color:var(--warn)}.p-none{color:var(--muted)}
.frame{width:100%;aspect-ratio:9/16;object-fit:cover;border-radius:6px;background:#000;display:block}
.strip{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-bottom:10px}
.reel{display:grid;grid-template-columns:110px 1fr;gap:12px;align-items:start}
.quote{font-size:21px;line-height:1.35;font-style:italic;margin:10px 0}
.why{font-size:16px}
.notes{font:14px/1.5 var(--sans);margin:8px 0;padding:0;list-style:none}.notes li{margin:2px 0}
.notes b{color:var(--muted);font-weight:600}
details{margin-top:8px;font:14px/1.55 var(--sans)}summary{cursor:pointer;color:var(--accent);font-weight:600;min-height:28px}
details .t{white-space:pre-wrap;background:var(--chip);padding:10px;border-radius:6px;max-height:360px;overflow:auto}
a{color:var(--accent)}a.open{font:600 14px/1 var(--sans)}
.creator{margin:26px 0 10px;font-size:22px;border-bottom:1px solid var(--rule);padding-bottom:6px}
.creator small{font:13px var(--sans);color:var(--muted);margin-left:6px}
.desk{font:14px/1.55 var(--sans);color:var(--muted)}.desk li{margin:3px 0}
.empty{font:15px/1.5 var(--sans);color:var(--muted);font-style:italic}
@media (max-width:520px){.reel{grid-template-columns:88px 1fr}.lead{font-size:17px}}
"""


def esc(text: Any) -> str:
    return html.escape(strip_long_dashes(str(text if text is not None else "")), quote=True)


def img_tag(path: Optional[str], alt: str, cls: str = "frame") -> str:
    if not path:
        return '<div class="%s" role="img" aria-label="no frame"></div>' % cls
    p = home() / path
    if not p.exists():
        return '<div class="%s" role="img" aria-label="frame missing"></div>' % cls
    data = base64.b64encode(p.read_bytes()).decode("ascii")
    return '<img class="%s" alt="%s" loading="lazy" src="data:image/jpeg;base64,%s">' % (cls, esc(alt), data)


def pill(ed: Dict[str, Any], k: str) -> str:
    lab = ed["labels"].get(k)
    if lab and lab["label"] in LABELED:
        return ('<span class="pill p-%s">%s &middot; %.2fx</span>'
                % (lab["label"].split()[0], esc(lab["label"]), lab["ratio"]))
    if lab:
        return '<span class="pill p-none">not enough history yet</span>'
    lt = ed["latest"].get(k, {})
    return '<span class="pill p-young">too young: %s views in %s</span>' % (fmt_int(lt.get("views")),
                                                                             fmt_age(lt.get("age_hours")))


def basis(ed: Dict[str, Any], k: str, need: int = 10) -> str:
    lab = ed["labels"].get(k)
    if lab and lab["label"] in LABELED:
        return ("Judged at %s: %s views vs. the median %s of %d other reels."
                % (fmt_age(lab["age_hours"]), fmt_int(lab["views"]), fmt_int(int(round(lab["median"]))),
                   lab["compared_with"]))
    if lab:
        return ("Judged at %s: only %d other reels to compare with (needs %d)."
                % (fmt_age(lab["age_hours"]), lab["compared_with"], need))
    return "Gets its label at its 3-day reading."


def numbers_row(ed: Dict[str, Any], k: str) -> str:
    lab = ed["labels"].get(k)
    src = lab if lab else ed["latest"].get(k, {})
    when = "at %s" % fmt_age(src.get("age_hours"))
    return ('<div class="nums"><span><b>Views %s</b>%s</span><span><b>Likes</b>%s</span>'
            '<span><b>Comments</b>%s</span></div>' % (esc(when), fmt_int(src.get("views")),
                                                        fmt_int(src.get("likes")), fmt_int(src.get("comments"))))


def posted_line(ed: Dict[str, Any], k: str) -> str:
    r = ed["reels"][k]
    t = parse_time(r.get("posted_at"))
    when = t.astimezone().strftime("%b %d, %Y %I:%M %p").replace(" 0", " ") if t else "date unknown"
    dur = r.get("duration_s") or (ed["watch"].get(k) or {}).get("duration_s")
    return "@%s &middot; %s &middot; posted %s%s" % (esc(r["creator"]), NAMES[r["platform"]], esc(when),
                                                    (" &middot; %ds long" % int(round(dur))) if dur else "")


def transcript_box(ed: Dict[str, Any], k: str) -> str:
    w = ed["watch"].get(k) or {}
    text = w.get("_transcript_text", "")
    if not w:
        return '<p class="meta">Not watched yet.</p>'
    if not text:
        return '<p class="meta">No transcript (no speech found, or no speech-to-text tool).</p>'
    return ("<details><summary>Transcript (%d words)</summary><div class=\"t\">%s</div></details>"
            % (len(text.split()), esc(text)))


def caption_box(ed: Dict[str, Any], k: str) -> str:
    cap = (ed["reels"][k].get("caption") or "").strip()
    if not cap:
        return ""
    return "<details><summary>Caption</summary><div class=\"t\">%s</div></details>" % esc(cap)


def sheet_link(ed: Dict[str, Any], k: str, out_dir: Path) -> str:
    w = ed["watch"].get(k) or {}
    if not w.get("contact_sheet"):
        return ""
    p = home() / w["contact_sheet"]
    try:
        rel_path = Path(_relpath(p, out_dir))
    except ValueError:
        return ""
    return ' &middot; <a href="%s">contact sheet</a>' % esc(rel_path.as_posix())


def _relpath(target: Path, start: Path) -> str:
    import os
    return os.path.relpath(str(target), str(start))


def notes_list(n: Dict[str, Any]) -> str:
    rows = []
    for f, label in (("hook", "Hook"), ("on_screen_text", "On screen"), ("format", "Format"),
                     ("visuals", "Visuals"), ("cta", "Call to action")):
        if str(n.get(f, "")).strip():
            rows.append("<li><b>%s:</b> %s</li>" % (label, esc(n[f])))
    return '<ul class="notes">%s</ul>' % "".join(rows) if rows else ""


def anchor(k: str) -> str:
    return "r-" + safe_name(k)


def build_html(cfg: Dict[str, Any], ed: Dict[str, Any], e: Dict[str, Any], edition_no: int,
               out_path: Path, problems: List[str], cost: float) -> str:
    today = _dt.datetime.now().astimezone()
    creators_in = sorted({(ed["reels"][k]["platform"], ed["reels"][k]["creator"]) for k in ed["new_keys"]})
    notes = e.get("reel_notes", {}) or {}
    P: List[str] = []
    add = P.append
    add("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>%s, %s</title><style>%s</style></head><body><div class=\"wrap\">"
        % (esc(cfg["paper_name"]), esc(today.strftime("%B %d, %Y")), CSS))
    add('<header class="mast"><h1>%s</h1></header>' % esc(cfg["paper_name"]))
    niche = cfg.get("niche") or ""
    add('<div class="dateline"><span>Edition %d</span><span>%s</span><span>%d new reel%s from %d creator%s</span>%s</div>'
        % (edition_no, esc(today.strftime("%A, %B %d, %Y")), len(ed["new_keys"]),
           "" if len(ed["new_keys"]) == 1 else "s", len(creators_in), "" if len(creators_in) == 1 else "s",
           ("<span>%s</span>" % esc(niche)) if niche else ""))

    # 1. Headline and lead story
    add('<h2 class="kicker">The lead</h2><div class="headline">%s</div>' % esc(e.get("headline", "")))
    paras = [p.strip() for p in re.split(r"\n\s*\n", str(e.get("lead_story", ""))) if p.strip()]
    add('<div class="lead">%s</div>' % "".join("<p>%s</p>" % esc(p) for p in paras))
    lk = e.get("lead_reel_keys") or []
    if lk:
        add('<div class="chips">%s</div>' % "".join(
            '<a class="chip" href="#%s">@%s: %s</a>' % (anchor(k), esc(ed["reels"][k]["creator"]),
                                                       esc((ed["reels"][k].get("caption") or k)[:48]))
            for k in lk if k in ed["reels"]))

    # 2. Breaking out
    add('<h2 class="kicker">Breaking out</h2>')
    by_key = {b["key"]: b for b in (e.get("breakouts") or [])}
    if not ed["breakouts"]:
        add('<p class="empty">No reel reached Breakout (3x its creator\'s usual views at 3 days) since the last paper.</p>')
    else:
        add('<div class="grid">')
        for k in ed["breakouts"]:
            b = by_key.get(k, {})
            w = ed["watch"].get(k) or {}
            frames = w.get("frames") or []
            pick = []
            if frames:
                hook = [f for f in frames if f["why"] == "hook"] or frames
                pick.append(min(hook, key=lambda f: abs(f["t"] - 1.0)))
                scene = [f for f in frames if f["why"] == "scene change"]
                mid = frames[len(frames) // 2]
                pick.append(scene[0] if scene else mid)
                pick.append(frames[-1] if frames[-1] not in pick else mid)
            strip = "".join(img_tag(f["path"], "frame at %.1f s" % f["t"]) for f in pick[:3])
            add('<article class="card" id="b-%s">%s<div class="meta">%s</div>%s%s'
                '<p class="quote">&ldquo;%s&rdquo;</p><p class="why">%s</p>'
                '<p class="meta">%s</p><a class="open" href="%s" target="_blank" rel="noopener">Open the original</a>%s</article>'
                % (safe_name(k), ('<div class="strip">%s</div>' % strip) if strip else "", posted_line(ed, k),
                   numbers_row(ed, k), pill(ed, k), esc(b.get("hook_quote", "")), esc(b.get("why_it_worked", "")),
                   esc(basis(ed, k)), esc(ed["reels"][k]["url"]), transcript_box(ed, k)))
        add("</div>")

    # 3. Themes
    add('<h2 class="kicker">Themes</h2>')
    themes = e.get("themes") or []
    if not themes:
        add('<p class="empty">No idea showed up from two or more creators this time.</p>')
    for t in themes:
        add('<article class="card" style="margin-bottom:14px"><h3>%s%s</h3><p>%s</p><ul class="notes">%s</ul></article>'
            % (esc(t.get("name", "")), " <span class=\"pill p-young\">back again</span>" if t.get("returning") else "",
               esc(t.get("summary", "")),
               "".join('<li><b><a href="#%s">@%s</a>:</b> %s</li>' % (anchor(a["key"]), esc(ed["reels"][a["key"]]["creator"]),
                                                                    esc(a.get("angle", "")))
                       for a in (t.get("angles") or []) if a.get("key") in ed["reels"])))

    # 4. Every new reel, creator by creator
    add('<h2 class="kicker">Every new reel</h2>')
    if not ed["new_keys"]:
        add('<p class="empty">No new reels since the last paper.</p>')
    for platform, handle in creators_in:
        ks = [k for k in ed["new_keys"] if (ed["reels"][k]["platform"], ed["reels"][k]["creator"]) == (platform, handle)]
        add('<h3 class="creator">@%s<small>%s &middot; %d new</small></h3><div class="grid">'
            % (esc(handle), NAMES[platform], len(ks)))
        for k in ks:
            r = ed["reels"][k]
            w = ed["watch"].get(k) or {}
            cap = (r.get("caption") or "").strip().splitlines()
            title = cap[0][:90] if cap else "Untitled reel"
            add('<article class="card" id="%s"><div class="reel"><div>%s</div><div><h3>%s</h3><div class="meta">%s</div>'
                '%s%s<p class="meta">%s</p></div></div>%s%s'
                '<a class="open" href="%s" target="_blank" rel="noopener">Open the original</a>'
                '<span class="meta">%s</span>%s%s</article>'
                % (anchor(k), img_tag(w.get("hero_frame"), "first second of the reel"), esc(title), posted_line(ed, k),
                   numbers_row(ed, k), pill(ed, k), esc(basis(ed, k)), notes_list(notes.get(k) or {}),
                   ("<p class=\"meta\">Watch problems: %s</p>" % esc("; ".join(w.get("problems") or [])))
                   if w.get("problems") else "", esc(r["url"]), sheet_link(ed, k, out_path.parent),
                   transcript_box(ed, k), caption_box(ed, k)))
        add("</div>")

    # 5. Since the last paper
    add('<h2 class="kicker">Since the last paper</h2>')
    items = []
    for k in ed["since_keys"]:
        lab = ed["labels"][k]
        if lab["label"] in LABELED:
            items.append('<li><a href="%s" target="_blank" rel="noopener">@%s</a>: &ldquo;%s&rdquo; reached <b>%s</b> at %s, %s views, %.2fx its usual %s.</li>'
                         % (esc(ed["reels"][k]["url"]), esc(ed["reels"][k]["creator"]), esc((ed["reels"][k].get("caption") or "")[:70]),
                            esc(lab["label"]), fmt_age(lab["age_hours"]), fmt_int(lab["views"]), lab["ratio"],
                            fmt_int(int(round(lab["median"])))))
    for s in e.get("since_last") or []:
        items.append("<li>%s</li>" % esc(s.get("text", "")))
    add('<ul class="desk" style="color:var(--ink)">%s</ul>' % "".join(items) if items
        else '<p class="empty">Nothing new to report on earlier stories.</p>')

    # Desk notes
    add('<h2 class="kicker">From the desk</h2><ul class="desk">')
    add("<li>How labels work: a reel is judged once, at its first reading at 3 days or older, against the median "
        "views of the same creator's other judged reels. Breakout is 3x or more, Above usual 1.5x to 3x, Usual "
        "0.5x to 1.5x, Below under 0.5x. With fewer than 10 reels to compare, it says not enough history yet.</li>")
    if cost:
        add("<li>Apify cost of the runs in this edition: $%.4f.</li>" % cost)
    for p in problems:
        add("<li>%s</li>" % esc(p))
    add("</ul></div></body></html>")
    return "\n".join(P)


# -------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run")
    ap.add_argument("--editorial")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    run_id = resolve_run(args.run)
    ed = select_edition(cfg)
    hist = ed["history"]
    edition_no = len(hist.get("editions", [])) + 1
    today = _dt.datetime.now().astimezone().date()
    epath = Path(args.editorial).expanduser() if args.editorial else runs_dir() / run_id / "editorial.json"
    e = read_json(epath, None)
    if e is None:
        raise UserError("No editorial at %s. Run scripts/brief.py, read the reels, then write it." % epath)
    errs = check_editorial(cfg, ed, e, edition_no, today)
    if errs:
        print("NOT PRINTED. editorial.json has %d problem(s):" % len(errs), file=sys.stderr)
        for x in errs:
            print("  - " + x, file=sys.stderr)
        return 5
    if args.check:
        print("editorial.json passes every check (%d new reels, %d breakouts, %d label updates)."
              % (len(ed["new_keys"]), len(ed["breakouts"]), len(ed["since_keys"])))
        return 0

    pend = read_json(data_dir() / "state.json", {}).get("pending_runs", []) or [run_id]
    problems, cost = [], 0.0
    for rid in pend:
        f = read_json(runs_dir() / rid / "fetch.json", {}) or {}
        problems.extend(f.get("problems", []))
        cost += float(f.get("charged_usd") or 0.0)
    for k in ed["new_keys"]:
        w = ed["reels"][k].get("watch") or {}
        if w.get("status") != "done" and ed["reels"][k].get("is_video", True):
            problems.append("@%s %s was not watched: %s" % (ed["reels"][k]["creator"], ed["reels"][k]["url"],
                                                             "; ".join(w.get("problems") or ["watch.py has not run on it"])))

    editions_dir().mkdir(parents=True, exist_ok=True)
    name, i = today.isoformat(), 2
    out = editions_dir() / (name + ".html")
    while out.exists():
        out = editions_dir() / ("%s_%d.html" % (name, i))
        i += 1
    page = build_html(cfg, ed, e, edition_no, out, problems, cost)
    at = next((j for j, ch in enumerate(page) if ch in LONG_DASHES), None)
    if at is not None:
        raise UserError("the page would contain a long dash near %r; not printed" % page[max(0, at - 40):at + 40])
    out.write_text(page, encoding="utf-8")

    shown = hist.setdefault("shown", {})
    edition_id = out.stem
    for k in ed["new_keys"]:
        shown.setdefault(k, {})["new_in"] = edition_id
    for k in ed["keys"]:
        if k in ed["labels"]:
            shown.setdefault(k, {})["label_in"] = edition_id
    hist.setdefault("editions", []).append({
        "edition": edition_no, "id": edition_id, "file": str(out.relative_to(home())), "printed_at": iso(now_utc()),
        "runs": pend, "headline": e.get("headline"), "new_keys": ed["new_keys"], "breakout_keys": ed["breakouts"],
        "label_update_keys": ed["since_keys"], "themes": [t.get("name", "") for t in (e.get("themes") or [])]})
    write_json(data_dir() / "history.json", hist)
    st = read_json(data_dir() / "state.json", {}) or {}
    st["pending_runs"] = []
    st["last_edition"] = edition_id
    write_json(data_dir() / "state.json", st)
    print("Printed edition %d: %s" % (edition_no, out))
    print("  %d new reels, %d breakouts, %d label updates, %d themes, %d KB"
          % (len(ed["new_keys"]), len(ed["breakouts"]), len(ed["since_keys"]), len(e.get("themes") or []),
             len(page.encode("utf-8")) // 1024))
    return 0


if __name__ == "__main__":
    run_main(main)
