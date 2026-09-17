#!/usr/bin/env python3
"""Find the songs from the ♪ subtitle lines, for a MUSIC channel.

  python scripts/music_scan.py [--work work] [--seasons 1-9] [--apply] [--min-lines 3] [--min-span 8]

Consecutive ♪ lines (gap <= --gap s) make a run. Runs that are just the show's title card or the
Itchy & Scratchy theme are dropped (the I&S shorts are already rows; their theme is not "music").
Then each run is matched against the review rows (work/segments.csv + hand-added rows, any status):

  overlaps an accepted row   -> that row gets the `music` tag (it is on a TV, so it also stays on
                                the normal channels); rows that were rejected/unreviewed are left alone
  overlaps nothing accepted  -> a new review.json row  SxxEyy-music-NN  (new=true, method music,
                                category music, status todo, tags [music, not_tv]) covering exactly
                                the sung lines (--pad adds seconds either side, default 0); trim it on
                                the review page and accept it. not_tv keeps it off MAIN and the
                                category channels in the player; drop the tag on the /tags/ page if
                                the song turns out to be on a screen.
  already a music row          -> still to do: its start/end/lyrics are rewritten from the current
                                settings (so a re-run with a new --pad retimes them); accepted or
                                rejected rows are left alone.

Without --apply nothing is written. Reload the review / tags page after running it.
"""
import argparse, csv, json, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from review_server import load_rows, write_reviewed, _sec, default_tags  # noqa: E402

SKIP = re.compile(r"itchy|scratchy|they fight and fight|^♪ the simpsons ♪$", re.I)


def hms(t):
    t = max(0.0, t)
    return f"{int(t // 3600):02d}:{int(t // 60 % 60):02d}:{t % 60:05.2f}"


def runs_for(subs, gap):
    """♪ lines grouped into runs: [(t0, t1, [lines])]."""
    out = []
    for s in subs:
        txt = s["Content"].replace("\n", " ").strip()
        if "♪" not in txt:
            continue
        a, b = s["StartTimestamp"] / 1000, s["EndTimestamp"] / 1000
        if out and a - out[-1][1] <= gap:
            out[-1][1] = max(out[-1][1], b); out[-1][2].append(txt)
        else:
            out.append([a, b, [txt]])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default="work")
    ap.add_argument("--seasons", default="", help="e.g. 1-9 or 3,5 (default: every episode with subs)")
    ap.add_argument("--gap", type=float, default=6.0, help="max silence between ♪ lines inside one run")
    ap.add_argument("--min-lines", type=int, default=3)
    ap.add_argument("--min-span", type=float, default=8.0, help="seconds")
    ap.add_argument("--pad", type=float, default=0.0, help="seconds added either side of a new row")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    seasons = set()
    for part in a.seasons.split(","):
        if "-" in part:
            x, y = part.split("-"); seasons.update(range(int(x), int(y) + 1))
        elif part.strip():
            seasons.add(int(part))

    rows, edits = load_rows(a.work)
    by_ep = {}
    for r in rows:
        by_ep.setdefault(r["episode"], []).append(r)
    rp = os.path.join(a.work, "review.json")

    tagged, new, retimed, kept, changes = [], [], [], 0, {}
    for fn in sorted(os.listdir(os.path.join(a.work, "subs"))):
        m = re.match(r"(S(\d\d)E\d\d)\.json$", fn)
        if not m or (seasons and int(m.group(2)) not in seasons):
            continue
        ep = m.group(1)
        subs = json.load(open(os.path.join(a.work, "subs", fn), encoding="utf-8")).get("Subtitles") or []
        n_ep = 0
        for t0, t1, lines in runs_for(subs, a.gap):
            body = [l for l in lines if not SKIP.search(l)]
            if len(body) < a.min_lines or t1 - t0 < a.min_span:
                continue
            kept += 1
            hit = None
            for r in by_ep.get(ep, []):
                e = edits.get(r["id"], {})
                if e.get("status") != "accept":
                    continue
                s, en = _sec(e.get("start") or r["start"]), _sec(e.get("end") or r["end"])
                if min(en, t1) - max(s, t0) > 2:              # overlap of more than 2 s
                    hit = r; break
            first = re.sub(r"^[-♪ ]+|[ ♪]+$", "", body[0])
            if hit:
                he = edits.get(hit["id"], {})
                tg = set(he["tags"] if "tags" in he else default_tags(he.get("category") or hit["category"]))
                if "music" not in tg:
                    tagged.append((hit["id"], hms(t0), hms(t1), first))
                    changes[hit["id"]] = {"tags": sorted(tg | {"music"})}
                continue
            existing = [k for k in edits if k.startswith(ep + "-music-")]
            while f"{ep}-music-{n_ep:02d}" in existing:
                n_ep += 1
            rid = f"{ep}-music-{n_ep:02d}"; n_ep += 1
            lyrics = " / ".join(re.sub(r"[♪ ]+$|^[-♪ ]+", "", l) for l in body)
            # the same run may already be a row from an earlier run of this script (with another --pad)
            dup = next((k for k in existing if abs(_sec(edits[k]["start"]) - t0) < 5), None)
            if dup:
                if edits[dup].get("status", "todo") == "todo" and \
                        (edits[dup].get("start"), edits[dup].get("end")) != (hms(t0 - a.pad), hms(t1 + a.pad)):
                    retimed.append((dup, hms(t0 - a.pad), hms(t1 + a.pad), first))
                    changes[dup] = dict(start=hms(t0 - a.pad), end=hms(t1 + a.pad), note=first, lyrics=lyrics)
                continue
            new.append((rid, hms(t0 - a.pad), hms(t1 + a.pad), first))
            changes[rid] = dict(new=True, episode=ep, category="music", method="music", status="todo",
                                  start=hms(t0 - a.pad), end=hms(t1 + a.pad), tags=["music", "not_tv"],
                                  note=first, lyrics=lyrics)

    print(f"{kept} song runs")
    print(f"\n{len(tagged)} inside accepted rows -> tag music:")
    for x in tagged:
        print("  %-28s %s-%s  %s" % x)
    print(f"\n{len(new)} new rows (music + not_tv, status todo):")
    for x in new:
        print("  %-22s %s-%s  %s" % x)
    print(f"\n{len(retimed)} existing to-do music rows retimed:")
    for x in retimed:
        print("  %-22s %s-%s  %s" % x)
    if a.apply:                                     # re-read and merge per row: the review server may be writing too
        cur = json.load(open(rp, encoding="utf-8")) if os.path.exists(rp) else {}
        for rid, d in changes.items():
            cur.setdefault(rid, {}).update(d)
        with open(rp, "w", encoding="utf-8") as f:
            json.dump(cur, f, indent=1)
        write_reviewed(a.work)
        print("\nwritten to", rp)
    else:
        print("\n(dry run: nothing written; add --apply)")


if __name__ == "__main__":
    main()
