#!/usr/bin/env python3
"""Accepted rows that cover the same footage twice (same episode, overlapping start/end).

  python scripts/find_dups.py [--work work] [--overlap 0.5] [--apply]

Two rows of one episode (accepted or still to do; rejected rows are ignored) are duplicates when the
shorter one's span is more than --overlap (fraction) inside the other. The scan merges anchors that
overlap, but a hand-added row, a vision extension or a second scan can still land on top of an
existing one (some I&S shorts came up twice).

Without --apply it just lists the pairs with each row's edit work (holds / cuts / vision / preview).
With --apply the loser is rejected with the note "dup of <id>": a to-do row loses to an accepted one,
otherwise the row with less edit work (fewer holds+cuts, then the shorter, then the later id). Pairs
whose categories differ (a `music` excerpt cut out of an advert, say) are only listed, never
rejected. Undo on the review page: accept the row again. Reload the review page afterwards.
"""
import argparse, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from review_server import load_rows, write_reviewed, _sec  # noqa: E402


def work(e):
    return len([x for x in (e.get("holds", "") + ";" + e.get("cuts", "")).split(";") if "-" in x]) \
        + (1 if e.get("vision") else 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default="work")
    ap.add_argument("--overlap", type=float, default=0.5)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    rows, edits = load_rows(a.work)
    acc = []
    for r in rows:
        e = edits.get(r["id"], {})
        if e.get("status", "todo") != "reject":
            acc.append((r["episode"], _sec(e.get("start") or r["start"]), _sec(e.get("end") or r["end"]), r["id"],
                        e.get("category") or r["category"], e.get("status", "todo")))
    pairs = []
    for i in range(len(acc)):
        for j in range(i + 1, len(acc)):
            p, q = acc[i], acc[j]
            if p[0] != q[0]:
                continue
            ov = min(p[2], q[2]) - max(p[1], q[1])
            short = min(p[2] - p[1], q[2] - q[1])
            if short > 0 and ov / short > a.overlap:
                pairs.append((p, q, ov / short))
    print(f"{len(acc)} accepted / to-do rows, {len(pairs)} overlapping pairs\n")
    drop = {}
    for p, q, f in pairs:
        same = p[4].split("+")[0].split("&")[0] == q[4].split("+")[0].split("&")[0]
        # accepted beats to-do; then more edit work, then the longer, then the earlier id
        keep, lose = sorted((p, q), key=lambda x: (x[5] != "accept", -work(edits.get(x[3], {})), -(x[2] - x[1]), x[3]))
        for x in (p, q):
            e = edits.get(x[3], {})
            tag = ("keep " if x is keep else "drop ") if same else "     "
            print(f"  {tag}{x[3]:28s} {x[4]:16s} {x[5]:6s} {x[1]:8.2f}-{x[2]:8.2f}"
                  f"  holds:{e.get('holds', '') or '-'}  cuts:{e.get('cuts', '') or '-'}"
                  f"{'  👁' if e.get('vision') else ''}{'  preview' if e.get('preview') else ''}")
        print(f"    overlap {f:.0%} of the shorter" + ("" if same else " — different categories, both kept") + "\n")
        if same:
            drop[lose[3]] = keep[3]
    if a.apply and drop:
        rp = os.path.join(a.work, "review.json")
        cur = json.load(open(rp, encoding="utf-8"))
        for lose, keep in drop.items():
            e = cur.setdefault(lose, {})
            e["status"] = "reject"
            e["note"] = (("dup of " + keep + "; ") + e.get("note", "")).strip("; ")
        with open(rp, "w", encoding="utf-8") as f:
            json.dump(cur, f, indent=1)
        write_reviewed(a.work)
        print(f"rejected {len(drop)} rows")
    elif drop:
        print("(dry run: add --apply to reject the 'drop' rows)")


if __name__ == "__main__":
    main()
