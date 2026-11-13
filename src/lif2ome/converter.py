"""Convert every series of a file. Each new file is read back and compared with the pixels that were read."""
from contextlib import nullcontext
import os
import re
import time
from pathlib import Path

import numpy as np
import tifffile

from . import reader, stitch
from .writer import iter_planes, read_metadata, write_ome


def _projection_kinds(words):
    kinds = tuple(dict.fromkeys("avg" if word == "mean" else word for word in words))
    if any(kind not in ("max", "sum", "avg") for kind in kinds):
        raise ValueError("projections must be max, sum, avg (mean is accepted as avg)")
    return kinds


def _project_planes(tif, kind):
    """ Reduce Z in page order, keeping all other axes. Hold one output and one plane. """
    series = tif.series[0]
    z = series.axes.index("Z")
    shape = series.shape[:z] + series.shape[z + 1:]
    data = np.zeros(shape, dtype=series.dtype if kind == "max" else np.float32)
    for k, page in enumerate(tif.pages):
        position = np.unravel_index(k, series.shape[:-2])
        target = data[position[:z] + position[z + 1:]]
        plane = page.asarray()
        if kind == "max":
            if position[z] == 0:
                target[:] = plane
            else:
                np.maximum(target, plane, out=target)
        else:
            np.add(target, plane, out=target)
    if kind == "avg":
        data /= series.shape[z]
    return data, series.axes.replace("Z", "")


def _project_one(source, kind):
    """ Write and check one projection. None means there is no Z stack. """
    out = source.parent / "projections" / f"{kind.upper()}_{source.name}"
    with tifffile.TiffFile(source) as tif:
        series = tif.series[0]
        if "Z" not in series.axes or series.shape[series.axes.index("Z")] <= 1:
            return None
        if out.exists():
            return "there"
        planes = series.shape[series.axes.index("Z")]
        data, axes = _project_planes(tif, kind)
    meta = read_metadata(source)
    word = {"max": "maximum", "sum": "sum", "avg": "average"}[kind]
    settings = {**meta["settings"], "Projection": f"{word} of {planes} planes along Z"}
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.name + ".part")
    try:
        write_ome(part, data, axes, meta["channels"], pixel_size=meta["pixel_size"],
                  pixel_size_y=meta["pixel_size_y"], time_step=meta["time_step"], metadata=meta["fields"],
                  settings=settings, leica_xml=meta["leica_xml"], channel_colors=meta["colors"])
        if not _same_pixels(part, data, axes):
            part.replace(out.with_name(out.name + ".BAD"))
            raise ValueError("the pixel check failed, renamed to .BAD")
        part.replace(out)
    finally:
        part.unlink(missing_ok=True)
    return "written"


def _project_outputs(out, kinds, label, result):
    written, there, errors = [], [], []
    for kind in kinds:
        try:
            state = _project_one(out, kind)
        except Exception as e:   ## one bad projection does not stop the others
            reason = f"{kind.upper()} FAILED: {type(e).__name__}: {e}"
            errors.append(reason)
            if result is not None:
                result("FAILED", label, "proj " + reason)
        else:
            if state is not None:
                (written if state == "written" else there).append(kind.upper())
    parts = [f"{', '.join(names)} {word}" for names, word in ((written, "written"), (there, "there")) if names]
    return "; ".join(parts + errors), len(errors)


def _ome_layout(data, axes, kind):
    """ The array and axes that go in the file: CZYX for a stack and a 2D image (as the older files have), CTYX for
    a movie, TCZYX for a movie of stacks """
    size = dict(zip(axes, data.shape))
    if size.get("T", 1) > 1 and size.get("Z", 1) > 1:
        want = "TCZYX"
    else:
        want = "CTYX" if kind == "movie" else "CZYX"
    for ax in [a for a in axes if a not in want]:   ## a T or Z axis of size 1
        data, axes = np.squeeze(data, axis=axes.index(ax)), axes.replace(ax, "")
    for ax in [a for a in want if a not in axes]:
        data, axes = data[np.newaxis], ax + axes
    return np.transpose(data, [axes.index(ax) for ax in want]), want


def _same_pixels(path, data, axes):
    """ Compare every pixel using one decoded YX plane at a time.

    write_ome writes minisblack pages in C order from views ending in YX.
    Singleton axes dropped from tifffile's series axes do not affect this order.
    """
    planes = iter_planes(data)
    with tifffile.TiffFile(path) as tif:
        if len(tif.pages) != int(np.prod(data.shape[:-2])):
            return False
        for page, plane in zip(tif.pages, planes):
            if page.dtype != data.dtype or page.shape != plane.shape:
                return False
            if not np.array_equal(page.asarray(), plane):
                return False
    return True


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


def _memory_budget():
    """40% of the smaller of physical RAM and any finite cgroup v1/v2 limit."""
    try:
        physical = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        return 0   ## unknown capacity: choose streaming conservatively
    limits = [max(0, physical)]
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            limit = int(Path(path).read_text().strip())
        except (OSError, ValueError):   ## includes cgroup v2's "max"
            continue
        if 0 < limit < 2**60:   ## v1 often uses a near-int64-max sentinel
            limits.append(limit)
    return min(limits) * 2 // 5


def _use_streaming(info, low_memory=False, *, budget=None):
    """Stream when forced or series bytes exceed 40% of min(physical RAM,
    finite container limit). Count twice the bytes if read_series' OME reorder
    or the final TIFF layout needs a contiguous copy; singleton moves are views.
    budget overrides the byte budget for tests. tifffile copies non-contiguous
    array input, so a non-contiguous transpose cannot be written as-is here.
    """
    if low_memory:
        return True
    size = dict(zip(info.axes, info.shape))
    source = "".join(ax for ax in info.axes if size[ax] > 1)
    read = "".join(ax for ax in reader.OME_ORDER if ax in source)
    want = "TCZYX" if size.get("T", 1) > 1 and size.get("Z", 1) > 1 else (
        "CTYX" if info.kind == "movie" else "CZYX")
    written = "".join(ax for ax in want if ax in source)
    need = int(np.prod(info.shape)) * np.dtype(info.dtype).itemsize
    need *= 2 if source != read or read != written else 1
    return need > (_memory_budget() if budget is None else budget)


def _convert_one(info, tile, out, status=lambda text: None, metadata=None, low_memory=False):
    if not isinstance(tile, dict):
        status("reading (step 1 of 3)")
    streaming = _use_streaming(info, low_memory)
    part = out.with_name(out.name + ".part")
    try:
        source = (nullcontext(None) if isinstance(tile, dict) else
                  reader.mapped_series(info, tile) if streaming else nullcontext(reader.read_series(info, tile)))
        with source as pixels:
            return _convert_open(info, tile, out, status, metadata, pixels, streaming)
    finally:
        part.unlink(missing_ok=True)


def _convert_open(info, tile, out, status, metadata, pixels, streaming):
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
        data, axes = pixels
    times = None if isinstance(tile, dict) else reader.read_plane_times(info, tile)
    if times is not None:
        times, _ = _ome_layout(times[..., np.newaxis, np.newaxis], axes, info.kind)
        times = times[..., 0, 0]
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
              reader.read_xml(info.path, info.series), info.pixel_size_y, info.time_step, channel_colors=info.colors,
              plane_times=times, streaming=streaming)
    ## ponytail: read every pixel back one YX plane at a time, without a second full-series array
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
    if streaming:
        details += ", streamed, low memory"
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

    A tile scan is merged into one file, unless tiles is True (one file per tile) or its positions cannot be read.
    Any LAS X merge is converted separately as a plain series under its own name. """
    path = Path(path)
    jobs, selected, used = [], [], set()
    names = reader.series_list(path)
    paths = reader.series_paths(path)

    def unique(label):
        base, n = label, 1
        while str(out_path(path, label, out_dir)).casefold() in used:
            n += 1
            label = f"{base} ({n})"
        used.add(str(out_path(path, label, out_dir)).casefold())
        return label

    for s, (name, series_path) in enumerate(zip(names, paths)):
        label = unique(series_path)
        try:
            info = reader.read_info(path, s, True)
            numbers, merge = (range(info.tiles) if info.tiles else [None]), None
            if info.tiles and not tiles:
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
        selected.extend(j for j in series_jobs if only in (name, series_path, series_path.replace("/", "_"), label, j[0]))
    if only is None:
        return jobs
    return selected or [(only, None, None, None,
                         f"FAILED: no series has the name {only!r}. Series names: {', '.join(paths)}")]


def convert(path, only=None, tiles=False, out_dir=None, report=print, status=lambda text: None, metadata=None,
            verbose=False, jobs=None, result=None, *, projections=(), low_memory=False):
    """ Every series of a file as an OME-TIFF (only: that series name only). Each file is read back and compared with
    the pixels that were read from the source. A file that exists is left alone, so a stopped run goes on from the next series. Returns the
    counts {"ok", "there", "skipped", "failed"}.

    report gets one line when a series is done: a word (ok, there, skipped, FAILED), the series name, and the sizes
    and the time or the reason. "ok" means written and checked. verbose adds the axes, the shape, the channels and
    the pixel sizes to an ok line. status gets a line each time the work on a series goes to its next step.
    metadata: a dict of your own fields, for example {"genotype": "Ecad"}; each new file gets them. Each new file
    also gets the microscope settings and the full Leica XML of its series; writer.read_metadata gives all three back.
    jobs: the result of plan() for this file, when the caller has it already.
    result: an optional callback receiving (word, label, reason) for each finished series or projection.
    low_memory: always stream writing and checking; Leica series are memory-mapped.
    projections: Z projections to write from each output file: max, sum, avg (or mean). """
    projections = _projection_kinds(projections)
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
                    line("working", label, f"{_gb(job_bytes(job))}, {step}")), metadata, low_memory)
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
        if projections and key in ("ok", "there"):
            summary, failed = _project_outputs(out, projections, label, result)
            counts["failed"] += failed
            if summary:
                report(line("proj", label, summary))
                if result is not None:
                    result("proj", label, summary)
    return counts
