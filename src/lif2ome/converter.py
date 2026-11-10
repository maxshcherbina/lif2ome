"""Convert every series of a file. Each new file is read back and compared with the pixels that were read."""
import re
import time
from pathlib import Path

import numpy as np
import tifffile

from . import reader, stitch
from .writer import write_ome


def _ome_layout(data, axes, kind):
    """ The array and axes that go in the file: CZYX for a stack and a 2D image (as the older files have), CTYX for
    a movie, TCZYX for a movie of stacks """
    size = dict(zip(axes, data.shape))
    if size.get("T", 1) > 1 and size.get("Z", 1) > 1:
        want = "TCZYX"
    else:
        want = "CTYX" if kind == "movie" else "CZYX"
    for ax in [a for a in axes if a not in want]:   ## a T or Z axis of size 1
        data, axes = np.take(data, 0, axis=axes.index(ax)), axes.replace(ax, "")
    for ax in [a for a in want if a not in axes]:
        data, axes = data[np.newaxis], ax + axes
    return np.ascontiguousarray(np.transpose(data, [axes.index(ax) for ax in want])), want


def _same_pixels(path, data, axes):
    """ True when the file holds the pixels of data. tifffile can drop an axis of size 1 when it reads """
    with tifffile.TiffFile(path) as tif:
        back, back_axes = tif.series[0].asarray(), tif.series[0].axes
    if back.dtype != data.dtype or not set(back_axes) <= set(axes):
        return False
    for ax in [a for a in axes if a not in back_axes]:
        back, back_axes = back[np.newaxis], ax + back_axes
    back = np.transpose(back, [back_axes.index(ax) for ax in axes])
    return np.array_equal(back, data)


def out_path(path, name, out_dir=None):
    """ <folder named after the file, beside it>/<file name> - <series>.ome.tif """
    path = Path(path)
    clean = re.sub(r'[<>:"/\\|?*]', "_", name)   ## characters that a file name cannot have
    return Path(out_dir or path.with_suffix("")) / f"{path.stem} - {clean}.ome.tif"


def _time_left(seconds):
    if seconds < 90:
        return f"about {max(round(seconds), 1)} s left"
    hours, minutes = divmod(round(seconds / 60), 60)
    return f"about {hours} h {minutes:02d} min left" if hours else f"about {minutes} min left"


def _gb(n):
    return f"{n / 1e9:.2f} GB" if n >= 1e8 else f"{n / 1e6:.1f} MB"


def _took(seconds):
    minutes, s = divmod(round(seconds), 60)
    return f"{minutes} min {s:02d} s" if minutes else f"{s} s"


def _convert_one(info, tile, out, status=lambda text: None, metadata=None):
    """ Writes and checks one file. Returns (done, the words for the progress line, the details for --verbose).
    status gets the step in work """
    start = time.time()
    merged = ""
    if isinstance(tile, dict):   ## a tile scan to merge
        status(f"reading and merging {len(tile['tiles'])} tiles (step 1 of 3)")
        axes = "".join(ax for ax in reader.OME_ORDER if ax in info.axes)
        data, found = stitch.merge(tile["positions"], lambda k: reader.read_series(info, tile["tiles"][k])[0])
        merged = (f"{found['columns']} x {found['rows']} tiles, overlap {found['overlap_x']:.0%} by {found['overlap_y']:.0%}; "
                  f"merged by lif2ome from the stage positions, each tile moved to fit its overlaps: "
                  f"{found['matched']} of {found['pairs']} overlaps matched, largest move {found['moved']:.0f} px")
        if tile.get("left_out"):
            merged += f"; {tile['left_out']} tile off the grid left out, --tiles writes it"
        if found["matched"] < found["pairs"] / 2:
            merged += "; CHECK THE MERGE: few overlaps matched"
    else:
        status("reading (step 1 of 3)")
        data, axes = reader.read_series(info, tile)
    data, axes = _ome_layout(data, axes, info.kind)
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.name + ".part")
    status("writing (step 2 of 3)")
    settings = reader.read_settings(info.path, info.series)
    if info.notes:
        settings["Note on the axes"] = "; ".join(info.notes)
    if merged:
        settings["Tile scan"] = merged
    write_ome(part, data, axes, info.channels, info.pixel_size, info.z_step, metadata, settings,
              reader.read_xml(info.path, info.series), info.pixel_size_y, info.time_step)
    ## ponytail: the check holds the series twice in memory; compare plane by plane if a series outgrows the memory
    status("checking the pixels (step 3 of 3)")
    if not _same_pixels(part, data, axes):
        part.replace(out.with_name(out.name + ".BAD"))
        return False, "the pixel check failed, renamed to .BAD", ""
    part.replace(out)
    with tifffile.TiffFile(out) as tif:
        big = ", BigTIFF" if tif.is_bigtiff else ""
    details = f"{axes} {data.shape} {data.dtype}, channels {info.channels}, {info.pixel_size:.4f} µm/px"
    details += f", z step {info.z_step:.4f} µm" if info.z_step and "Z" in axes else ""
    details += f", {info.time_step:.4g} s between frames" if info.time_step and "T" in axes else ""
    details += "".join(f", {note}" for note in info.notes) + big
    words = f"{_gb(data.nbytes)} -> {_gb(out.stat().st_size)}   {_took(time.time() - start)}"
    if merged:
        words += f"   merged {merged.split(';')[0]}"
        if tile.get("left_out"):
            words += f"; {tile['left_out']} tile off the grid left out, --tiles writes it"
        if "CHECK" in merged:
            words += "; CHECK THE MERGE: few overlaps matched"
        details = merged.split("; ", 1)[1] + "\n" + details
    return True, words, details


def job_bytes(job):
    """ The size of the pixels of one job of plan(), in bytes; 0 for a series that is not converted """
    info, tile = job[1], job[2]
    count = len(tile["tiles"]) if isinstance(tile, dict) else 1   ## a merge reads every tile of the scan
    return int(np.prod(info.shape)) * np.dtype(info.dtype).itemsize * count if info is not None else 0


def plan(path, only=None, tiles=False, out_dir=None):
    """ The jobs of one file, one per output file: [(label, info, tile, out, error)]. A series that is not converted
    has an error text ("SKIPPED: ..." or "FAILED: ...") and no info. tile is None for a plain series, the number of
    the tile for one tile of a tile scan, and {"tiles": [...], "positions": [...], "left_out": n} for a tile scan to
    merge.

    A tile scan is merged into one file, unless tiles is True (one file per tile) or the file already holds the merged
    image: a series whose name begins with the name of the tile scan and has "merg" in it, as LAS X names it """
    path = Path(path)
    jobs, selected, used = [], [], set()
    names = reader.series_list(path)

    def unique(label):
        base, n = label, 1
        while str(out_path(path, label, out_dir)).casefold() in used:
            n += 1
            label = f"{base} ({n})"
        used.add(str(out_path(path, label, out_dir)).casefold())
        return label

    for s, name in enumerate(names):
        label = unique(name)
        try:
            info = reader.read_info(path, s, True)
            numbers, merge = (range(info.tiles) if info.tiles else [None]), None
            if info.tiles and not tiles:
                copy = next((n for n in names if n != name and n.startswith(name) and "merg" in n.lower()), None)
                if copy:
                    raise reader.Unsupported(f"tile scan with {info.tiles} tiles; the file has the merged series {copy!r}")
                try:
                    positions = reader.read_tile_positions(path, s)
                except reader.Unsupported as e:
                    raise reader.Unsupported(f"tile scan with {info.tiles} tiles, not merged: {e}; use --tiles for one "
                                             "file per tile") from None
                off = stitch.grid(positions, info.shape[-2:])["off_grid"]   ## a tile in no row and no column
                keep = [k for k in range(info.tiles) if k not in off]
                ## a tile in no row and no column is not part of the picture: it is left out, and --tiles writes it
                merge, numbers = {"tiles": keep, "positions": [positions[k] for k in keep], "left_out": len(off)}, []
        except reader.Unsupported as e:
            series_jobs = [(label, None, None, None, f"SKIPPED: {e}")]
        except Exception as e:
            series_jobs = [(label, None, None, None, f"FAILED: {type(e).__name__}: {e}")]
        else:
            width = max(len(str(info.tiles)), 2)
            series_jobs = [(label, info, merge, out_path(path, label, out_dir), None)] if merge else []
            for tile in numbers:
                full = label if tile is None else unique(f"{label} - tile {tile + 1:0{width}d}")
                series_jobs.append((full, info, tile, out_path(path, full, out_dir), None))
        jobs.extend(series_jobs)
        selected.extend(j for j in series_jobs if only in (name, label, j[0]))
    if only is None:
        return jobs
    return selected or [(only, None, None, None,
                         f"FAILED: no series has the name {only!r}. Series names: {', '.join(names)}")]


def convert(path, only=None, tiles=False, out_dir=None, report=print, status=lambda text: None, metadata=None,
            verbose=False, jobs=None, result=None):
    """ Every series of a file as an OME-TIFF (only: that series name only). Each file is read back and compared with
    the pixels that were read from the source. A file that exists is left alone, so a stopped run goes on from the next series. Returns the
    counts {"ok", "there", "skipped", "failed"}.

    report gets one line when a series is done: a word (ok, there, skipped, FAILED), the series name, and the sizes
    and the time or the reason. "ok" means written and checked. verbose adds the axes, the shape, the channels and
    the pixel sizes to an ok line. status gets a line each time the work on a series goes to its next step.
    metadata: a dict of your own fields, for example {"genotype": "Ecad"}; each new file gets them. Each new file
    also gets the microscope settings and the full Leica XML of its series; writer.read_metadata gives all three back.
    jobs: the result of plan() for this file, when the caller has it already.
    result: an optional callback receiving (word, label, reason) for each finished series """
    jobs = plan(path, only, tiles, out_dir) if jobs is None else jobs
    counts = {"ok": 0, "there": 0, "skipped": 0, "failed": 0}
    width = min(max((len(job[0]) for job in jobs), default=0), 40)
    line = lambda word, label, rest: f"  {word:<8}  {label:<{width}}  {rest}".rstrip()
    left = sum(job_bytes(j) for j in jobs if j[1] is not None and not j[3].exists())   ## bytes still to convert
    done, start = 0, time.time()
    for job in jobs:
        label, info, tile, out, error = job
        if error:
            key = "skipped" if error.startswith("SKIPPED: ") else "failed"
            word, rest = ("skipped", error.removeprefix("SKIPPED: ")) if key == "skipped" else ("FAILED", error.removeprefix("FAILED: "))
        elif out.exists():
            key, word, rest = "there", "there", ""
        else:
            try:
                ok, rest, details = _convert_one(info, tile, out, lambda step: status(
                    line("working", label, f"{_gb(job_bytes(job))}, {step}")), metadata)
            except Exception as e:   ## one bad series does not stop the others
                ok, rest, details = False, f"{type(e).__name__}: {e}", ""
            key, word = ("ok", "ok") if ok else ("failed", "FAILED")
            left -= job_bytes(job)
            if ok:
                done += job_bytes(job)
            if ok and left > 0:
                rest += ", " + _time_left(left * (time.time() - start) / done)
            if verbose and details:
                rest += "\n" + " " * (width + 14) + details
        counts[key] += 1
        report(line(word, label, rest))
        if result is not None:
            result(word, label, rest)
    return counts
