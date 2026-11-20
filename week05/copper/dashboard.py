"""Export a standalone dashboard from Week 05 result files."""

import argparse
import csv
import json
from .results import read_rows
from datetime import date, datetime
from pathlib import Path

from .strategy import ENTRY_OPEN, FORCE_CLOSE, KST, STEP

ROOT = Path(__file__).resolve().parents[1]


def read_csv(path):
    rows = read_rows(path)
    if rows:
        return rows
    if not path.is_file():
        path = path.parent / "details" / path.name
        if not path.is_file():
            return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def read_run(folder):
    bars = read_csv(folder / "bars.csv")
    return dict(bars=[{k: str(row[k]) if k == "start" else row[k] for k in ("start", "close", "regime")} for row in bars],
                trades=read_csv(folder / "trades.csv"), holds=read_csv(folder / "holds.csv"),
                positions=read_csv(folder / "open_positions.csv"))


def render(root, now):
    threshold_file = root / "thresholds.json"
    thresholds = json.loads(threshold_file.read_text(encoding="utf-8"))["thresholds"] if threshold_file.is_file() else {}
    separate_replay = (root / "replay").is_dir()
    replay = read_run(root / "replay" if separate_replay else root)
    available = {}
    for bar in replay["bars"]:
        start = datetime.fromisoformat(bar["start"])
        bars = available.setdefault(start.date(), [])
        if ENTRY_OPEN <= start.time() < FORCE_CLOSE:
            bars.append(start)
    sessions = []
    requested = {date(2026, 11, day) for day in (16, 17, 18)} if separate_replay else set()
    for day in sorted(requested | available.keys()):
        key = day.isoformat()
        cutoff = datetime.combine(day, FORCE_CLOSE, KST)
        pending = day not in available and now < cutoff
        expected = (cutoff - datetime.combine(day, ENTRY_OPEN, KST)) // STEP
        sessions.append(dict(day=key, state="ready" if day in available else "pending" if pending else "missing",
                             received=len(available.get(day, ())), expected=expected))
    paper = []
    for folder in sorted((root / "paper").glob("*/"), reverse=True):
        # Earlier runs stored actual paper fills in a nested directory.
        result = folder / "paper" if read_csv(folder / "paper" / "summary.csv") else folder
        if read_csv(result / "summary.csv"):
            paper.append(dict(name=folder.name, **read_run(result)))
    baseline = next((row for row in read_csv(root / "raw" / "coverage-baseline.csv")
                     if row["symbol"] == thresholds.get("symbol")), None)
    data = dict(replay=replay, sessions=sessions, paper=paper, thresholds=thresholds, baseline=baseline)
    payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    return (ROOT / "copper" / "dashboard.html").read_text(encoding="utf-8").replace("__WEEK05_DATA__", payload)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=ROOT / "results")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or args.results / "dashboard.html"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(args.results, datetime.now(KST)), encoding="utf-8")
    print(output.resolve())


if __name__ == "__main__":
    main()
