#!/usr/bin/env python3
"""Suggest tags for the accepted rows from the subtitle lines heard inside them.

  python scripts/auto_tags.py [--work work] [--min 2] [--tags sports,news] [--ids ...] [--apply]

Every accepted row's span is matched against work/subs/SxxEyy.json; each tag in RULES has a regex,
and a row whose lines hit it at least --min times (default 2; --min 1 for the rarer ones) is listed
with the hit count and the first matching line. Tags the row already carries are skipped.
--apply adds the listed tags to review.json (the existing tags stay; a row with no tag list yet
starts from its category tokens, as the /tags/ page does) and regenerates segments_reviewed.csv.
Reload the review / tags page after --apply; extract_clips.py rewrites clips/index.csv (no re-cut).
"""
import argparse, json, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from review_server import load_rows, write_reviewed, _sec, default_tags  # noqa: E402

RULES = {
    "sports": r"touchdown|home ?run|\binnings?\b|quarterback|football|baseball|basketball|hockey|golf|bowling|"
              r"boxing|boxer|wrestl|tennis|olympic|stadium|playoff|half ?time|\bbatters?\b|referee|umpire|"
              r"championship|world series|super bowl|knockout|heavyweight|"
              r"pitcher|slam dunk|marathon|racetrack|isotopes|pin ?pals|springfield atoms",
    "news": r"this just in|breaking news|channel (?:six|6)|kent brockman|eye on springfield|smartline|"
            r"in other news|reporting live|news at|good evening,? i'm|our top story|special report|"
            r"editorial|action news|and now .{0,20}weather",
    "mcbain": r"mcbain|mendoza",
    "troy_mcclure": r"troy mcclure|you may remember me|you might remember me",
    "krusty": r"krusty|sideshow|hey,? hey,? kids",
    "itchy_scratchy": r"itchy|scratchy",
    "advert": r"call now|operators are standing by|order now|not sold in stores|side effects|act now|"
              r"while supplies last|batteries not included|only \$?\d|money back|\bguaranteed\b|"
              r"available (?:now|at)|for a limited time|ask your doctor|new from",
    "bumblebee": r"bumblebee|¡ay|ay,? caramba|no me gusta|señor",
}                                                   # `music` is music_scan.py's job (it skips the I&S theme)


def lines_in(subs, s, e):
    for x in subs:
        a, b = x["StartTimestamp"] / 1000, x["EndTimestamp"] / 1000
        if min(b, e) - max(a, s) > 0.3:
            yield x["Content"].replace("\n", " ").strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default="work")
    ap.add_argument("--min", type=int, default=2, help="matching lines needed to suggest a tag")
    ap.add_argument("--tags", default="", help="only these tags (comma-separated; default all)")
    ap.add_argument("--ids", nargs="*", default=[])
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    want = [t for t in a.tags.split(",") if t] or list(RULES)
    rx = {t: re.compile(RULES[t], re.I) for t in want}

    rows, edits = load_rows(a.work)
    subs_cache = {}
    changes, listed = {}, 0
    for r in rows:
        e = edits.get(r["id"], {})
        if e.get("status") != "accept" or (a.ids and r["id"] not in a.ids):
            continue
        ep = r["episode"]
        if ep not in subs_cache:
            p = os.path.join(a.work, "subs", ep + ".json")
            subs_cache[ep] = json.load(open(p, encoding="utf-8")).get("Subtitles") or [] if os.path.exists(p) else []
        s, en = _sec(e.get("start") or r["start"]), _sec(e.get("end") or r["end"])
        lines = list(lines_in(subs_cache[ep], s, en))
        have = set(e["tags"] if "tags" in e else default_tags(e.get("category") or r["category"]))
        add = {}
        for t, x in rx.items():
            if t in have:
                continue
            hits = [l for l in lines if x.search(l)]
            if len(hits) >= a.min:
                add[t] = hits
        if add:
            listed += 1
            print(f"{r['id']:<28} [{'+'.join(sorted(have))}]")
            for t, hits in add.items():
                print(f"    + {t:<14} x{len(hits)}  {hits[0][:70]}")
            changes[r["id"]] = sorted(have | set(add))
    print(f"\n{listed} rows get new tags")
    if a.apply:
        rp = os.path.join(a.work, "review.json")
        cur = json.load(open(rp, encoding="utf-8"))          # re-read: the review server may be writing too
        for rid, tags in changes.items():
            cur.setdefault(rid, {})["tags"] = tags
        with open(rp, "w", encoding="utf-8") as f:
            json.dump(cur, f, indent=1)
        write_reviewed(a.work)
        print("written to", rp)
    else:
        print("dry run; --apply to write")


if __name__ == "__main__":
    main()
