#!/usr/bin/env python3
"""Publish the player and the clips to GitHub Pages.

  python scripts/deploy_pages.py [--branch gh-pages] [--remote origin] [--dry-run]

Builds the `gh-pages` branch from scratch in a temporary worktree with the same layout the local
server uses (so the player's relative paths work unchanged):

  index.html          -> redirects to player/
  player/index.html   copy of player/index.html
  clips/index.csv     + every mp4 it lists
  .nojekyll           so nothing is preprocessed

then commits and force-pushes it. The clips are copyrighted footage: this makes them public.
GitHub Pages serves a public repo at https://<user>.github.io/<repo>/player/ once Pages is set to
the gh-pages branch (`gh api -X POST repos/<user>/<repo>/pages -f source[branch]=gh-pages -f source[path]=/`).
Repo soft limit 1 GB, 100 MB per file; the clips are ~2 MB each.
"""
import argparse, csv, os, shutil, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REDIRECT = '<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0; url=player/">' \
           '<title>Springfield TV</title><a href="player/">Springfield TV</a>\n'


def git(*args, cwd=ROOT, check=True):
    return subprocess.run(["git", *args], cwd=cwd, check=check, capture_output=True, text=True).stdout.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--branch", default="gh-pages")
    ap.add_argument("--remote", default="origin")
    ap.add_argument("--clips", default=os.path.join(ROOT, "clips"))
    ap.add_argument("--dry-run", action="store_true", help="build the tree, report sizes, push nothing")
    a = ap.parse_args()

    idx = os.path.join(a.clips, "index.csv")
    rows = list(csv.DictReader(open(idx, encoding="utf-8")))
    files = [r["file"] for r in rows if os.path.exists(os.path.join(a.clips, r["file"]))]
    missing = len(rows) - len(files)
    size = sum(os.path.getsize(os.path.join(a.clips, f)) for f in files)
    print(f"{len(files)} clips, {size / 1e6:.0f} MB" + (f" ({missing} listed but missing)" if missing else ""))

    tmp = tempfile.mkdtemp(prefix="stv-pages-")
    wt = os.path.join(tmp, "wt")
    git("worktree", "prune")
    if git("branch", "--list", a.branch):
        git("worktree", "add", wt, a.branch)
        for n in os.listdir(wt):                       # start clean; the tree is rebuilt every time
            if n != ".git":
                (shutil.rmtree if os.path.isdir(os.path.join(wt, n)) else os.remove)(os.path.join(wt, n))
    else:
        git("worktree", "add", "--detach", wt)
        git("checkout", "--orphan", a.branch, cwd=wt)
        git("rm", "-rf", "-q", ".", cwd=wt, check=False)
        for n in os.listdir(wt):
            if n != ".git":
                (shutil.rmtree if os.path.isdir(os.path.join(wt, n)) else os.remove)(os.path.join(wt, n))
    os.makedirs(os.path.join(wt, "player")); os.makedirs(os.path.join(wt, "clips"))
    shutil.copy(os.path.join(ROOT, "player", "index.html"), os.path.join(wt, "player", "index.html"))
    open(os.path.join(wt, "index.html"), "w", encoding="utf-8").write(REDIRECT)
    open(os.path.join(wt, ".nojekyll"), "w").close()
    with open(os.path.join(wt, "clips", "index.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader()
        w.writerows(r for r in rows if r["file"] in files)
    for fn in files:
        shutil.copy(os.path.join(a.clips, fn), os.path.join(wt, "clips", fn))

    git("add", "-A", cwd=wt)
    if git("status", "--porcelain", cwd=wt):
        git("-c", "user.useConfigOnly=false", "commit", "-q", "-m", f"pages: {len(files)} clips", cwd=wt)
        print("committed", git("rev-parse", "--short", "HEAD", cwd=wt))
    else:
        print("no change since last deploy")
    if a.dry_run:
        print("dry run: not pushing; tree left in", wt)
        return
    print(git("push", "-f", a.remote, f"{a.branch}:{a.branch}", cwd=wt) or f"pushed {a.branch}")
    git("worktree", "remove", "--force", wt)
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
