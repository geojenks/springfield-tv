#!/usr/bin/env python3
"""Fill the blank `episode` column in a catalog CSV using the TVmaze API.

Matches on (season, simpsons_episode title). Rows with season 'shorts' are skipped.
Usage:  python scripts/fill_episode_numbers.py catalog/itchy_scratchy.csv
"""
import csv, json, re, sys, urllib.request, unicodedata

API = "https://api.tvmaze.com/singlesearch/shows?q=the+simpsons&embed=episodes"

def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())

def main(path):
    with urllib.request.urlopen(API) as r:
        eps = json.load(r)["_embedded"]["episodes"]
    lookup = {(e["season"], norm(e["name"])): e["number"] for e in eps}

    rows = list(csv.DictReader(open(path, newline="", encoding="utf-8")))
    missing = []
    for row in rows:
        if row["episode"] or not row["season"].isdigit():
            continue
        key = (int(row["season"]), norm(row["simpsons_episode"]))
        if key in lookup:
            row["episode"] = str(lookup[key])
        else:
            # fuzzy: title startswith
            cands = [n for (s, t), n in lookup.items()
                     if s == key[0] and (t.startswith(key[1]) or key[1].startswith(t))]
            if len(cands) == 1:
                row["episode"] = str(cands[0])
            else:
                missing.append(row["id"] + " " + row["simpsons_episode"])

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader(); w.writerows(rows)
    print(f"filled; unmatched: {len(missing)}")
    for m in missing: print("  ", m)

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "catalog/itchy_scratchy.csv")
