"""Convert every series of a file, and check each new file against the source pixels."""
import re
import time
from pathlib import Path

import numpy as np
import tifffile

from . import reader
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


def _convert_one(info, tile, out, status=lambda text: None):
    """ Writes and checks one file. Returns the words for the progress line. status gets the step in work """
    status("reading (step 1 of 3)")
    data, axes = reader.read_series(info, tile)
    data, axes = _ome_layout(data, axes, info.kind)
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.name + ".part")
    status("writing (step 2 of 3)")
    write_ome(part, data, axes, info.channels, info.pixel_size, info.z_step)
    ## ponytail: the check holds the series twice in memory; compare plane by plane if a series outgrows the memory
    status("checking the pixels (step 3 of 3)")
    if not _same_pixels(part, data, axes):
        part.replace(out.with_name(out.name + ".BAD"))
        return "FAILED the pixel check, renamed to .BAD"
    part.replace(out)
    with tifffile.TiffFile(out) as tif:
        big = ", BigTIFF" if tif.is_bigtiff else ""
    size = out.stat().st_size
    size = f"{size / 1e9:.2f} GB" if size >= 1e8 else f"{size / 1e6:.1f} MB"
    z = f", z step {info.z_step:.4f} µm" if info.z_step and "Z" in axes else ""
    return (f"ok, {axes} {data.shape} {data.dtype}, channels {info.channels}, {info.pixel_size:.4f} µm/px{z}, "
            f"{size}{big}, pixels match")


def convert(path, only=None, tiles=False, out_dir=None, report=print, status=lambda text: None):
    """ Every series of a file as an OME-TIFF (only: that series name only). Each file is read back and compared with
    the source pixels. A file that exists is left alone, so a stopped run goes on from the next series. Returns the
    counts {"ok", "there", "skipped", "failed"}; report gets one line per series when the series is done; status gets
    a line each time the work on a series goes to its next step (read, write, check) """
    path = Path(path)
    jobs, counts, seen = [], {"ok": 0, "there": 0, "skipped": 0, "failed": 0}, {}
    names = reader.series_list(path)
    if only is not None and only not in names:
        report(f"  FAILED: no series has the name {only!r}. Series names: {', '.join(names)}")
        counts["failed"] = 1
        return counts
    for s, name in enumerate(names):
        seen[name] = seen.get(name, 0) + 1
        label = name if seen[name] == 1 else f"{name} ({seen[name]})"   ## two series with one name: none is lost
        if only is not None and name != only:
            continue
        try:
            info = reader.read_info(path, s, tiles)
        except ValueError as e:
            jobs.append((label, None, None, None, f"SKIPPED: {e}"))
            continue
        except Exception as e:
            jobs.append((label, None, None, None, f"FAILED: {type(e).__name__}: {e}"))
            continue
        width = max(len(str(info.tiles)), 2)
        for tile in range(info.tiles) if info.tiles else [None]:
            full = label if tile is None else f"{label} - tile {tile + 1:0{width}d}"
            jobs.append((full, info, tile, out_path(path, full, out_dir), None))
    size = lambda job: int(np.prod(job[1].shape)) * np.dtype(job[1].dtype).itemsize
    left = sum(size(j) for j in jobs if j[1] is not None and not j[3].exists())   ## bytes still to convert
    done, start = 0, time.time()
    for i, (label, info, tile, out, error) in enumerate(jobs, 1):
        if error:
            words, key = error, "skipped" if error.startswith("SKIPPED:") else "failed"
        elif out.exists():
            words, key = "already there", "there"
        else:
            try:
                gb = f"{size(jobs[i - 1]) / 1e9:.2f} GB"
                words = _convert_one(info, tile, out, lambda step: status(f"  [{i}/{len(jobs)}] {label}: {gb}, {step}"))
            except Exception as e:   ## one bad series does not stop the others
                words = f"FAILED: {type(e).__name__}: {e}"
            key = "ok" if words.startswith("ok") else "failed"
            done, left = done + size(jobs[i - 1]), left - size(jobs[i - 1])
            if left > 0:
                words += ", " + _time_left(left * (time.time() - start) / done)
        counts[key] += 1
        report(f"  [{i}/{len(jobs)}] {label}: {words}")
    return counts
