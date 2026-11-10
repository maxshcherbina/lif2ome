"""Read a series of a Leica .lif file: what it holds, and its pixels.

The reader uses liffile. bioio-lif gives the colour names as channel names, keeps a negative Z step (a stack taken
top to bottom) and cannot tell a cross-section (XZ scan) from a stack. A .czi file is read through bioio when bioio and
bioio-czi are installed; that path has no test with a real file.
"""
from dataclasses import dataclass, field
from pathlib import Path

import liffile
import numpy as np

KINDS = {"stack": "3D stack", "movie": "2D movie", "image": "2D image"}
OME_ORDER = "TCZYX"


@dataclass
class ImageInfo:
    path: str
    axes: str                 ## the axes of the series in the file, for example ZCYX; a tile scan has no M here
    shape: tuple
    kind: str                 ## stack, movie or image
    channels: list            ## the channel names
    anisotropy: float = 1.0   ## the Z step / the pixel size; 1 when one of them is not known
    series: int = 0
    series_name: str = ""
    dtype: str = "uint8"
    notes: list = field(default_factory=list)
    pixel_size: float = 0.0   ## the X step in µm; 0: not known
    z_step: float = 0.0       ## the Z step in µm; 0: not known or no Z axis
    tiles: int = 0            ## the number of tiles of a tile scan; 0: not a tile scan

    @property
    def n(self):
        """ The number of slices or frames """
        for ax in "ZTIQ":
            if ax in self.axes and self.shape[self.axes.index(ax)] > 1:
                return self.shape[self.axes.index(ax)]
        return 1

    @property
    def name(self):
        stem = Path(self.path).name
        for suffix in (".ome.tif", ".ome.tiff", ".tif", ".tiff", ".lif", ".czi"):
            stem = stem.removesuffix(suffix)
        return f"{stem} - {self.series_name}" if self.series_name else stem

    def describe(self):
        """ One line, for example "3D stack, 15 slices, 3 channels" """
        what = {"stack": "slices", "movie": "frames", "image": "plane"}[self.kind]
        n = "1 plane" if self.kind == "image" else f"{self.n} {what}"
        c = len(self.channels)
        return f"{KINDS[self.kind]}, {n}, {c} channel{'s' if c != 1 else ''}"


def guess_kind(axes, shape):
    size = dict(zip(axes, shape))
    if size.get("Z", 1) > 1:
        return "stack"
    if max(size.get("T", 1), size.get("I", 1), size.get("Q", 1)) > 1:
        return "movie"
    return "image"


def _is_lif(path):
    return str(path).lower().endswith(".lif")


def series_list(path):
    """ The names of the series of a .lif file (or a .czi file, with bioio) """
    if _is_lif(path):
        with liffile.LifFile(path) as lif:
            return [im.name for im in lif.images]
    return [str(s) for s in _bioio_image(path).scenes]


def read_info(path, series=0, tiles=False):
    """ What one series holds. Raises ValueError, with a sentence for the user, for a series this library does not
    convert: a cross-section, a series with other axes than T, Z, C, Y, X, and a tile scan unless tiles is True """
    path = str(path)
    if not _is_lif(path):
        return _bioio_info(path, series)
    with _lif_image(path, series) as im:
        setting = im.xml_element.find('./Data/Image/Attachment[@Name="HardwareSetting"]/ATLConfocalSettingDefinition')
        scan = setting.attrib.get("ScanMode", "") if setting is not None else ""
        if scan and not scan.startswith("xy"):   ## only the scan mode tells a cross-section from a stack
            raise ValueError(f"cross-section (scan mode {scan}), not a stack or a movie")
        axes, shape = "".join(d[0] for d in im.dims), tuple(im.shape)
        n_tiles = 0
        if "M" in im.dims:
            n_tiles = shape[im.dims.index("M")]
            if not tiles:
                raise ValueError(f"tile scan with {n_tiles} tiles, skipped; convert the merged series, or use --tiles "
                                 "for one file per tile")
            shape = tuple(n for d, n in zip(im.dims, shape) if d != "M")
            axes = axes.replace("M", "")
        if len(set(axes)) < len(axes) or not set(axes) <= set(OME_ORDER):
            raise ValueError(f"axes {', '.join(im.dims)}: only T, Z, C, Y and X are read")
        z, x = _lif_step(im.coords, "Z"), _lif_step(im.coords, "X")
        return ImageInfo(path, axes, shape, guess_kind(axes, shape), _lif_channel_names(im),
                         z / x if z and x else 1.0, series, series_name=im.name, dtype=str(im.dtype),
                         pixel_size=x * 1e6, z_step=z * 1e6, tiles=n_tiles)


def read_series(info, tile=None):
    """ (data, axes): all the pixels of one series, the axes in OME order (T, C, Z, Y, X; an axis the series does not
    have is absent). tile: the tile to read of a tile scan, counted from 0 """
    if not _is_lif(info.path):
        img = _bioio_image(info.path)
        img.set_scene(info.series)
        return np.asarray(img.get_image_data(OME_ORDER)), OME_ORDER
    with _lif_image(info.path, info.series) as im:
        if info.tiles:
            data = liffile.LifImageFrames(im, M=int(tile)).asarray()   ## one tile only: a tile scan can be very large
            if data.shape != info.shape:
                raise ValueError(f"tile {tile} has the shape {data.shape}, not {info.shape}")
        else:
            data = im.asarray()
    axes = "".join(ax for ax in OME_ORDER if ax in info.axes)
    return np.ascontiguousarray(np.transpose(data, [info.axes.index(ax) for ax in axes])), axes


def to_cnyx(data, axes):
    """ (C, N, Y, X) from any axes order: N is Z, T or I; single axes of size 1 go away """
    for ax in list(axes):
        if ax not in "CYX" and data.shape[axes.index(ax)] == 1 and len(axes) > 3:
            data = np.take(data, 0, axis=axes.index(ax))
            axes = axes.replace(ax, "")
    if "C" not in axes:
        data, axes = data[np.newaxis], "C" + axes
    if len(axes) == 3:
        data, axes = data[:, np.newaxis], axes[0] + "N" + axes[1:]
    n_axes = [ax for ax in axes if ax not in "CYX"]
    if len(n_axes) != 1:
        raise ValueError(f"This file has axes {axes}: one stack or one movie at a time.")
    order = [axes.index(ax) for ax in ("C", n_axes[0], "Y", "X")]
    return np.transpose(data, order)


def read_part(info, channels, frames=None, crop=None, tile=None):
    """ A part of one stack or one movie, (C, N, Y, X): the chosen channels (indices), an optional frame range
    (first, last), inclusive, counted from 0, and an optional crop box (y0, y1, x0, x1) """
    data = to_cnyx(*read_series(info, tile))[list(channels)]
    if frames is not None:
        data = data[:, frames[0]:frames[1] + 1]
    if crop is not None:
        data = data[:, :, crop[0]:crop[1], crop[2]:crop[3]]
    return np.ascontiguousarray(data)


## .lif: liffile
class _lif_image:
    """ with _lif_image(path, series) as im: one series of a .lif file, the file open while the block runs """
    def __init__(self, path, series):
        self.path, self.series = path, series

    def __enter__(self):
        self.lif = liffile.LifFile(self.path)
        try:
            return self.lif.images[self.series]
        except Exception:   ## a wrong series: __exit__ does not run, so close here
            self.lif.close()
            raise

    def __exit__(self, *exc):
        self.lif.close()


def _lif_step(coords, axis):
    """ The step in metres of one axis: |last - first| / (N - 1); a top-to-bottom stack has a negative span """
    values = coords.get(axis)
    return abs(float(values[-1]) - float(values[0])) / (len(values) - 1) if values is not None and len(values) > 1 else 0


def _lif_channel_names(im):
    """ The dye name when the file has one, else the colour of the channel in LAS X (its LUT, for example Green),
    else Channel n. liffile names a channel with no dye Ch0, Ch1... """
    described = sorted(im.xml_element.findall("./Data/Image/ImageDescription/Channels/ChannelDescription"),
                       key=lambda c: int(c.attrib.get("BytesInc", 0)))   ## liffile's channel order
    names = []
    for i, name in enumerate(str(c) for c in im.coords.get("C", [])):
        if name.startswith("Ch") and name[2:].isdigit() or name in ("Red", "Green", "Blue"):
            name = described[i].attrib.get("LUTName") if i < len(described) else ""
        names.append(name or f"Channel {i + 1}")
    if len(set(names)) < len(names):   ## two channels with one colour: the number tells them apart
        names = [f"{n} {i + 1}" for i, n in enumerate(names)]
    return names or ["Channel 1"]


## .czi: bioio reads each series (a "scene") of the file. No test with a real file
def _bioio_image(path):
    try:
        from bioio import BioImage
    except ImportError:
        raise ValueError(f"{Path(path).name}: lif2ome reads .lif files. For a .czi file, install the extra: "
                         "uv pip install 'lif2ome[czi]'") from None
    return BioImage(path)


def _bioio_info(path, series):
    img = _bioio_image(path)
    img.set_scene(series)
    shape, sizes = tuple(img.dims.shape), img.physical_pixel_sizes
    return ImageInfo(str(path), OME_ORDER, shape, guess_kind(OME_ORDER, shape), [str(c) for c in img.channel_names],
                     (sizes.Z / sizes.X) if sizes.Z and sizes.X else 1.0, series, series_name=str(img.scenes[series]),
                     dtype=str(img.dtype), pixel_size=float(sizes.X or 0), z_step=float(sizes.Z or 0))
