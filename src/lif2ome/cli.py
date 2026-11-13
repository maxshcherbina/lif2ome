"""The lif2ome command."""
import argparse
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import tifffile

from . import reader
from .converter import _gb, _projection_kinds, _time_left, _took, convert, job_bytes, plan
from .writer import read_metadata


def _inspect(path, label):
    """ Prints the metadata of an OME-TIFF that lif2ome wrote, or the settings of each series of a .lif file """
    print(label)
    if Path(path).suffix.lower() not in (".tif", ".tiff") and reader._is_lif(path):
        blocks = [(f"Series {name}", reader.read_settings(path, s)) for s, name in enumerate(reader.series_paths(path))]
        xml = None
    else:
        meta = read_metadata(path)
        print("  Channels")
        words = {rgb: name for name, rgb in reader.COLORS.items()}
        width = max((len(name) for name in meta["channels"]), default=0)
        for name, color in zip(meta["channels"], meta["colors"]):
            text = words.get(color, ", ".join(map(str, color))) if color is not None else ""
            print(f"    {name:<{width}}  {text}".rstrip())
        _inspect_times(path, meta["plane_times"])
        blocks, xml = [("Your fields", meta["fields"]), ("Microscope settings", meta["settings"])], meta["leica_xml"]
    for title, pairs in blocks:
        print(f"  {title}" + ("" if pairs else ": none"))
        width = max((len(k) for k in pairs), default=0)
        for key, value in pairs.items():
            print(f"    {key:<{width}}  {value}")
    if xml is not None:
        print(f"  Leica XML: {len(xml):,} characters" if xml else "  Leica XML: none")


def _inspect_times(path, times):
    if not times:
        return
    with tifffile.TiffFile(path) as tif:
        series = tif.series[0]
        if "T" not in series.axes or series.shape[series.axes.index("T")] <= 1:
            return
        values = np.asarray(times).reshape(series.shape[:-2])
        t = series.axes.index("T")
        starts = np.moveaxis(values, t, 0).reshape(series.shape[t], -1).min(axis=1)
    steps = np.diff(starts)
    print(f"  Times: {len(starts)} time points, {max(times) - min(times):.6g} s from first to last plane, "
          f"steps {steps.min():.6g} to {steps.max():.6g} s")


def _expand(paths):
    """ [(path, label)]: a file as it is, labelled with its name; a folder as every .lif and .xlef file in it and in its
    subfolders, sorted, each labelled with its path from the folder (two subfolders can hold files with one name).
    A name that starts with "." or "@" is not searched: macOS leaves ._name.lif files on network shares, and a NAS
    keeps deleted files in @Recycle """
    found = []
    for path in paths:
        if not path.is_dir():
            found.append((path, path.name))
            continue
        lifs = sorted(f for f in path.rglob("*") if f.suffix.lower() in (".lif", ".xlef") and f.is_file()
                      and not any(part.startswith((".", "@")) for part in f.relative_to(path).parts))
        if not lifs:
            print(f"{path}: no .lif or .xlef files in this folder", flush=True)
        found += [(f, f.relative_to(path).as_posix()) for f in lifs]
    unique = {}
    for path, label in found:
        unique.setdefault(path.resolve(), (path, label))
    return list(unique.values())


def _status_line(text, width):
    return (text + " ...")[:max(0, width - 1)]


def _projections(text):
    try:
        return _projection_kinds(text.split(","))
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def main(argv=None):
    parser = argparse.ArgumentParser(prog="lif2ome", description="Convert every series of Leica files to "
                                     "OME-TIFF files, in a folder named after each input file, beside it.")
    parser.add_argument("files", nargs="+", type=Path, help="Leica files (.lif, .xlef, .xlcf, .xllf, .lof, .xlif), "
                        ".czi files, or folders: a folder stands for "
                        "every .lif and .xlef file in it and in its subfolders")
    parser.add_argument("--only", metavar="SERIES", help="convert the series with this name only")
    parser.add_argument("--tiles", action="store_true", help="write one file per tile of a tile scan; without this, an "
                        "unmerged tile scan is merged into one file")
    parser.add_argument("--out", metavar="FOLDER", type=Path, help="write the files in this folder")
    parser.add_argument("--projections", type=_projections, default=(), metavar="max,sum,avg",
                        help="write Z projections: max, sum, avg (mean is accepted as avg)")
    parser.add_argument("--meta", metavar="KEY=VALUE", action="append", default=[],
                        help="a field of your own that each new file gets, for example --meta genotype=Ecad; "
                        "give it more than one time for more fields")
    parser.add_argument("--low-memory", action="store_true", help="always stream series writing and checking")
    parser.add_argument("--verbose", action="store_true", help="add the axes, the shape, the channels and the pixel "
                        "sizes to the line of each converted series")
    parser.add_argument("--inspect", action="store_true", help="convert nothing: print the metadata of .ome.tif files "
                        "that lif2ome wrote, or the microscope settings of each series of Leica files")
    args = parser.parse_args(argv)
    if any("=" not in pair for pair in args.meta):
        parser.error("--meta needs KEY=VALUE, for example --meta genotype=Ecad")
    fields = dict(pair.split("=", 1) for pair in args.meta)
    files = _expand(args.files)
    if args.inspect:
        for path, label in files:
            try:
                _inspect(path, label)
            except Exception as e:   ## one bad file does not stop the others
                print(f"  FAILED: {type(e).__name__}: {e}")
        return 0
    ## in a terminal, one line shows the step in work on the series and the result line then takes its place
    live = sys.stdout.isatty()
    clear = "\r\x1b[K" if live else ""
    status = (lambda text: print(clear + _status_line(text, shutil.get_terminal_size().columns), end="", flush=True)) if live else (lambda text: None)
    total, problems, folders, start = {}, [], [], time.time()

    def report(line):
        print(clear + line, flush=True)

    def result(word, name, reason):
        nonlocal converted_bytes
        if word == "ok":
            converted_bytes += job_bytes(next(j for j in jobs if j[0] == name))
        if word in ("skipped", "FAILED"):
            problems.append(f"  {label}: " + (f"{name}: " if name else "") + f"{word}, {reason}")

    if not files:   ## only empty folders: nothing to do is not a failure
        return 0
    ## the plan of every file first: the totals and the time left need all of them
    plans = []
    for path, label in files:
        ## with --out, a file from a subfolder keeps its subfolder, so two files with one name do not collide
        out = args.out / Path(label).parent if args.out else None
        try:
            plans.append((path, label, out, plan(path, args.only, args.tiles, out), None))
        except Exception as e:   ## one bad file does not stop the others
            plans.append((path, label, out, [], f"{type(e).__name__}: {e}"))
    missing = lambda jobs: len(jobs) == 1 and (jobs[0][4] or "").startswith("FAILED: no series has the name ")
    matched = args.only is not None and any(jobs and not missing(jobs) for *_, jobs, _ in plans)
    outputs = {}
    for path, label, out, jobs, error in plans:
        if matched and missing(jobs):
            jobs[0] = (args.only, None, None, None, "SKIPPED: no series has that name")
        for i, (name, info, tile, target, problem) in enumerate(jobs):
            if target is None:
                continue
            key = str(target.resolve()).casefold()
            if key in outputs:
                jobs[i] = (name, None, tile, target, f"FAILED: output already planned by {outputs[key]}")
            else:
                outputs[key] = path
    todo = lambda jobs: [j for j in jobs if j[1] is not None and not j[3].exists()]
    all_series = sum(len(jobs) for *_, jobs, _ in plans)
    bytes_left, bytes_done, series_done = sum(job_bytes(j) for *_, jobs, _ in plans for j in todo(jobs)), 0, 0
    for k, (path, label, out, jobs, error) in enumerate(plans, 1):
        size = sum(job_bytes(j) for j in jobs)
        print(f"[{k}/{len(plans)}] {label}" + (f"   {len(jobs)} series, {_gb(size)}" if jobs else ""), flush=True)
        began, mine = time.time(), sum(job_bytes(j) for j in todo(jobs))
        converted_bytes = 0
        if error:
            result("FAILED", "", error)
            report(f"  FAILED    {error}")
            counts = {"failed": 1}
        else:
            counts = convert(path, args.only, args.tiles, out, report, status, fields, args.verbose, jobs,
                             result=result, projections=args.projections, low_memory=args.low_memory)
        for key, n in counts.items():
            total[key] = total.get(key, 0) + n
        bytes_left, bytes_done, series_done = bytes_left - mine, bytes_done + converted_bytes, series_done + len(jobs)
        parts = [f"{n} {word}" for word, n in (("converted", counts.get("ok", 0)), ("already there", counts.get("there", 0)),
                                               ("skipped", counts.get("skipped", 0)), ("failed", counts.get("failed", 0))) if n]
        end = f"  file done: {', '.join(parts) or 'nothing to do'} in {_took(time.time() - began)}."
        if len(plans) > 1:
            end += f"   Total: {series_done} of {all_series} series"
            if bytes_left > 0 and bytes_done:
                end += ", " + _time_left(bytes_left * (time.time() - start) / bytes_done)
        print(end + "\n", flush=True)
        folder = out or path.with_suffix("")
        if folder not in folders and folder.is_dir():
            folders.append(folder)
    get = total.get
    print(f"Done in {_took(time.time() - start)}: {get('ok', 0)} converted, {get('there', 0)} already there, "
          f"{get('skipped', 0)} skipped, {get('failed', 0)} failed", flush=True)
    for folder in folders:
        files = [f for f in folder.glob("*.ome.tif") if not f.name.startswith("._")]
        print(f"{folder}\n  now holds {len(files)} OME-TIFF files, {_gb(sum(f.stat().st_size for f in files))}")
    if problems:
        print("Not converted:\n" + "\n".join(problems), flush=True)
    return 1 if get("failed") else 0
