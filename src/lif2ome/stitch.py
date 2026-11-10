"""Merge the tiles of a tile scan into one image.

The stage position of each tile comes from the file. A stage does not return to the same place on each row, so the
positions are then corrected: each pair of tiles that overlap is matched by its picture, and the positions that fit all
the matches best are used. A pair with no clear match keeps the distance that the stage gave.
"""
import numpy as np

SEARCH = 40      ## how far, in pixels, a match may be from the distance that the stage gave
MIN_SCORE = 0.3  ## the correlation of the overlap of two tiles from which a match counts (1 is a perfect match)


def grid(positions, shape):
    """ What the positions say about the scan: {"columns", "rows", "overlap_x", "overlap_y" (a fraction of the tile),
    "off_grid" (indices of tiles that are in no row and no column with another tile)} """
    positions = np.asarray(positions, float)
    out = {}
    lines = {}
    for name, axis, size in (("columns", 0, shape[1]), ("rows", 1, shape[0])):
        values = np.sort(positions[:, axis])
        starts = values[np.insert(np.diff(values) > size * 0.02, 0, True)]   ## positions closer than 2 % are one line
        lines[axis] = [np.flatnonzero(np.abs(positions[:, axis] - s) <= size * 0.02) for s in starts]
    alone = lambda axis: {int(m[0]) for m in lines[axis] if len(m) == 1}
    off = sorted(alone(0) & alone(1)) if len(positions) > 2 else []
    for name, axis, size in (("columns", 0, shape[1]), ("rows", 1, shape[0])):
        kept = [m for m in lines[axis] if not set(m.tolist()) <= set(off)]
        centres = np.sort([positions[m, axis].mean() for m in kept])
        out[name] = len(kept)
        step = np.median(np.diff(centres)) if len(centres) > 1 else size
        out["overlap_" + "xy"[axis]] = float(1 - step / size)
    out["off_grid"] = off
    return out


def _offset(a, b, nominal):
    """ (score, dy, dx): where tile b lies from tile a, found from the pictures, near the nominal (dy, dx) """
    h, w = a.shape
    fa, fb = np.fft.rfft2(a - a.mean()), np.fft.rfft2(b - b.mean())
    cross = fa * np.conj(fb)
    surface = np.fft.irfft2(cross / (np.abs(cross) + 1e-6), s=a.shape)
    ny, nx = (int(round(v)) for v in nominal)
    ys, xs = np.arange(ny - SEARCH, ny + SEARCH + 1), np.arange(nx - SEARCH, nx + SEARCH + 1)
    window = surface[np.ix_(ys % h, xs % w)]
    i, j = np.unravel_index(np.argmax(window), window.shape)
    dy, dx = int(ys[i]), int(xs[j])
    if abs(dy) >= h or abs(dx) >= w:
        return 0.0, ny, nx
    u = a[max(dy, 0):h + min(dy, 0), max(dx, 0):w + min(dx, 0)]
    v = b[max(-dy, 0):h + min(-dy, 0), max(-dx, 0):w + min(-dx, 0)]
    if u.size < 64:
        return 0.0, ny, nx
    u, v = u - u.mean(), v - v.mean()
    return float((u * v).sum() / (np.sqrt((u * u).sum() * (v * v).sum()) + 1e-9)), dy, dx


def place(positions, pictures):
    """ (positions, matched, pairs): the corrected (x, y) of each tile in pixels, as integers from 0; how many pairs
    of tiles that overlap gave a clear match, and how many pairs there are. positions: the (x, y) the stage gave, in
    pixels. pictures: one 2D picture per tile, to match the overlaps """
    positions = np.asarray(positions, float)
    n, (h, w) = len(positions), pictures[0].shape
    rows, rhs_x, rhs_y, weights = [], [], [], []
    matched = pairs = 0
    for i in range(n):
        for j in range(i + 1, n):
            dx, dy = positions[j] - positions[i]
            if abs(dx) >= w * 0.98 or abs(dy) >= h * 0.98 or min(w - abs(dx), h - abs(dy)) < 8:
                continue   ## the two tiles do not share a strip
            if (w - abs(dx)) * (h - abs(dy)) < 0.02 * w * h:
                continue   ## they touch at a corner only
            pairs += 1
            score, my, mx = _offset(pictures[i], pictures[j], (dy, dx))
            if score < MIN_SCORE:
                continue
            matched += 1
            row = np.zeros(n)
            row[i], row[j] = -1, 1
            rows.append(row); rhs_x.append(mx); rhs_y.append(my); weights.append(score)
    ## each tile is also held, weakly, at the place the stage gave: this fixes the whole picture in place and keeps a
    ## tile with no match where the stage put it
    rows += list(np.eye(n))
    weights += [0.01] * n
    a = np.array(rows) * np.array(weights)[:, None]
    solve = lambda rhs: np.linalg.lstsq(a, np.array(rhs) * np.array(weights), rcond=None)[0]
    x = solve(rhs_x + list(positions[:, 0]))
    y = solve(rhs_y + list(positions[:, 1]))
    fitted = np.round(np.stack([x - x.min(), y - y.min()], axis=1)).astype(int)
    return fitted, matched, pairs


def merge(positions, read_tile):
    """ (data, report): one image from the tiles. positions: the (x, y) of each tile in pixels, as the stage gave
    them. read_tile(k): the pixels of tile k, with Y and X as the last two axes. Where two tiles overlap, the pixels
    of the later tile are kept as they are: no pixel value is changed or averaged.
    report: {"columns", "rows", "overlap_x", "overlap_y", "matched", "pairs", "moved" (the largest correction of a
    tile, in pixels)} """
    positions = np.asarray(positions, float)
    first = read_tile(0)
    h, w = first.shape[-2:]
    picture = lambda tile: tile.reshape(-1, h, w).mean(axis=0, dtype=np.float32)
    pictures = [picture(first)] + [picture(read_tile(k)) for k in range(1, len(positions))]
    fitted, matched, pairs = place(positions, pictures)
    del pictures
    data = np.zeros(first.shape[:-2] + (fitted[:, 1].max() + h, fitted[:, 0].max() + w), first.dtype)
    for k, (x, y) in enumerate(fitted):
        data[..., y:y + h, x:x + w] = first if k == 0 else read_tile(k)
    nominal = positions - positions.min(axis=0)
    moved = np.abs((fitted - fitted.mean(axis=0)) - (nominal - nominal.mean(axis=0))).max()
    return data, {**{k: v for k, v in grid(positions, (h, w)).items() if k != "off_grid"},
                  "matched": matched, "pairs": pairs, "moved": float(moved)}
