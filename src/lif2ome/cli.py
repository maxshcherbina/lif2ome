"""The lif2ome command."""
import argparse
import sys
import time
from pathlib import Path

from .converter import convert


def _size(n):
    return f"{n / 1e9:.2f} GB" if n >= 1e8 else f"{n / 1e6:.1f} MB"


def _took(seconds):
    minutes, s = divmod(round(seconds), 60)
    return f"{minutes} min {s:02d} s" if minutes else f"{s} s"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="lif2ome", description="Convert every series of Leica .lif files to "
                                     "OME-TIFF files, in a folder named after each .lif file, beside it.")
    parser.add_argument("files", nargs="+", type=Path, help="one or more .lif files")
    parser.add_argument("--only", metavar="SERIES", help="convert the series with this name only")
    parser.add_argument("--tiles", action="store_true", help="write one file per tile of a tile scan")
    parser.add_argument("--out", metavar="FOLDER", type=Path, help="write the files in this folder")
    args = parser.parse_args(argv)
    ## in a terminal, one line shows the step in work on the series and the result line then takes its place
    live = sys.stdout.isatty()
    clear = "\r\x1b[K" if live else ""
    status = (lambda text: print(clear + text + " ...", end="", flush=True)) if live else (lambda text: None)
    total, problems, folders, start = {}, [], [], time.time()

    def report(line):
        print(clear + line, flush=True)
        if "SKIPPED: " in line or "FAILED" in line:
            problems.append(f"  {path.name}: {line.strip()}")

    for path in args.files:
        print(path.name, flush=True)
        try:
            counts = convert(path, args.only, args.tiles, args.out, report, status)
        except Exception as e:   ## one bad file does not stop the others
            report(f"  FAILED: {type(e).__name__}: {e}")
            counts = {"failed": 1}
        for key, n in counts.items():
            total[key] = total.get(key, 0) + n
        folder = args.out or path.with_suffix("")
        if folder not in folders and folder.is_dir():
            folders.append(folder)
    get = total.get
    print(f"\nDone in {_took(time.time() - start)}: {get('ok', 0)} converted, {get('there', 0)} already there, "
          f"{get('skipped', 0)} skipped, {get('failed', 0)} failed", flush=True)
    for folder in folders:
        files = [f for f in folder.glob("*.ome.tif") if not f.name.startswith("._")]
        print(f"{folder}\n  now holds {len(files)} OME-TIFF files, {_size(sum(f.stat().st_size for f in files))}")
    if problems:
        print("Not converted:\n" + "\n".join(problems), flush=True)
    return 1 if get("failed") else 0
