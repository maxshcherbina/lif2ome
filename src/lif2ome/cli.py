"""The lif2ome command."""
import argparse
from pathlib import Path

from .converter import convert


def main(argv=None):
    parser = argparse.ArgumentParser(prog="lif2ome", description="Convert every series of Leica .lif files to "
                                     "OME-TIFF files, in a folder named after each .lif file, beside it.")
    parser.add_argument("files", nargs="+", type=Path, help="one or more .lif files")
    parser.add_argument("--only", metavar="SERIES", help="convert the series with this name only")
    parser.add_argument("--tiles", action="store_true", help="write one file per tile of a tile scan")
    parser.add_argument("--out", metavar="FOLDER", type=Path, help="write the files in this folder")
    args = parser.parse_args(argv)
    total = {}
    for path in args.files:
        print(path.name, flush=True)
        try:
            counts = convert(path, args.only, args.tiles, args.out, lambda line: print(line, flush=True))
        except Exception as e:   ## one bad file does not stop the others
            print(f"  FAILED: {type(e).__name__}: {e}", flush=True)
            counts = {"failed": 1}
        for key, n in counts.items():
            total[key] = total.get(key, 0) + n
    get = total.get
    print(f"Done: {get('ok', 0)} converted, {get('there', 0)} already there, {get('skipped', 0)} skipped, "
          f"{get('failed', 0)} failed", flush=True)
    return 1 if get("failed") else 0
