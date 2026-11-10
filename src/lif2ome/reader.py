"""Read a series of a Leica .lif file: what it holds, and its pixels.

The reader uses liffile. bioio-lif gives the colour names as channel names, keeps a negative Z step (a stack taken
top to bottom) and gives a cross-section (XZ scan) the axes of a stack. A .czi file is read through bioio when bioio and
bioio-czi are installed; that path has no test with a real file.
"""
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

import liffile
import numpy as np

KINDS = {"stack": "3D stack", "movie": "2D movie", "image": "2D image"}
OME_ORDER = "TCZYX"


class Unsupported(ValueError):
    """ A series that this reader does not convert. """


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
    pixel_size_y: float = 0.0   ## the Y step in µm; 0: the same as X
    time_step: float = 0.0    ## the time between frames in seconds; 0: not known or no T axis
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
    """ What one series holds. Raises Unsupported, with a sentence for the user, for a series this library does not
    convert: a series with other axes than T, Z, C, Y, X, and a tile scan unless tiles is True.
    A cross-section (XZ scan) has planes of Z by X. A plane of an OME-TIFF is Y by X, so the info names the axes as
    the file has them: Y of the file is Z of the microscope, and Z of the file is Y of the microscope """
    path = str(path)
    if not _is_lif(path):
        return _bioio_info(path, series)
    with _lif_image(path, series) as im:
        axes, shape = "".join(d[0] for d in im.dims), tuple(im.shape)
        n_tiles = 0
        if "M" in im.dims:
            n_tiles = shape[im.dims.index("M")]
            if not tiles:
                raise Unsupported(f"tile scan with {n_tiles} tiles; convert the merged series, or use --tiles "
                                 "for one file per tile")
            shape = tuple(n for d, n in zip(im.dims, shape) if d != "M")
            axes = axes.replace("M", "")
        z, y, x, t = (_lif_step(im.coords, ax) for ax in "ZYXT")
        axes, cross = cross_section_axes(axes)
        notes = []
        if cross:
            z, y = y, z
            notes.append("cross-section: Y of the file is Z of the microscope")
        if len(set(axes)) < len(axes) or not set(axes) <= set(OME_ORDER) or not axes.endswith("YX"):
            raise Unsupported(f"axes {', '.join(im.dims)}: only T, Z, C, Y and X are read, with planes of Y by X or Z by X")
        return ImageInfo(path, axes, shape, guess_kind(axes, shape), _lif_channel_names(im),
                         z / x if z and x else 1.0, series, series_name=im.name, dtype=str(im.dtype), notes=notes,
                         pixel_size=x * 1e6, pixel_size_y=y * 1e6, z_step=z * 1e6, time_step=t, tiles=n_tiles)


def cross_section_axes(axes):
    """ (axes, True) with the letters Z and Y exchanged when the planes are Z by X (a cross-section); else
    (axes, False). ZX gives YX; YZX, a pile of cross-sections along Y, gives ZYX """
    if axes.endswith("ZX"):
        return axes.translate(str.maketrans("ZY", "YZ")), True
    return axes, False


def read_tile_positions(path, series=0):
    """ The (x, y) of each tile of a tile scan, in pixels, as the stage gave them: tile k of the series is entry k.
    Raises Unsupported when the file does not give a position for each tile """
    with _lif_image(str(path), series) as im:
        scan = im.xml_element.find('./Data/Image/Attachment[@Name="TileScanInfo"]')
        tiles = scan.findall("Tile") if scan is not None else []
        if "M" not in im.dims or len(tiles) != im.shape[im.dims.index("M")]:
            raise Unsupported("the file gives no stage position for each tile")
        ## ponytail: no file with a flipped or swapped scan was at hand, so such a scan is not merged; read the flags
        ## here when one turns up
        if any(scan.attrib.get(flag, "0") != "0" for flag in ("FlipX", "FlipY", "SwapXY")):
            raise Unsupported("the tiles are flipped or swapped against the stage axes")
        x, y = _lif_step(im.coords, "X"), _lif_step(im.coords, "Y")
        if not x or not y:
            raise Unsupported("the file gives no pixel size")
        return [(float(t.attrib["PosX"]) / x, float(t.attrib["PosY"]) / y) for t in tiles]


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


def read_xml(path, series=0):
    """ The Leica XML of one series, as text: every setting the .lif file holds for it. "" for a file that is not .lif """
    if not _is_lif(path):
        return ""
    with _lif_image(str(path), series) as im:
        return ElementTree.tostring(im.xml_element, encoding="unicode")


def read_settings(path, series=0):
    """ The main microscope settings of one series in readable form, a dict of label and text, for example
    {"Objective": "HC PL APO CS2 40x/1.30 OIL", "Zoom": "0.75", "Laser 488 nm (%)": "1"}. The Leica XML (read_xml) has
    all settings; this is the short list a person reads """
    out = {"Source file": Path(path).name}
    if not _is_lif(path):
        return out
    with _lif_image(str(path), series) as im:
        out["Series"] = im.name
        ## ponytail: the XML differs between microscopes and LAS X versions, so a part that is absent or odd is left
        ## out of the short list; the full XML is in the file in every case
        try:
            _lif_settings(im, out)
        except Exception as e:
            out["Note"] = f"the short list is not complete ({type(e).__name__}: {e}); see the Leica XML"
    return out


def _num(text, digits=3, scale=1.0):
    """ A number of the XML as short text: 0.999614337196571 gives 1, 6.52917E-05 with scale 1e6 gives 65.3 """
    return f"{float(text) * scale:.{digits}g}"


def _lif_settings(im, out):
    stamps = im.timestamps
    if stamps is not None and len(stamps):
        out["Acquired"] = str(stamps[0]).split(".")[0].replace("T", " ")
    step = _lif_step(im.coords, "T")
    if step:
        out["Time between frames (s)"] = _num(step, 4)
    hw = im.xml_element.find('./Data/Image/Attachment[@Name="HardwareSetting"]')
    if hw is None:
        return
    conf = hw.find("ATLConfocalSettingDefinition")
    c = conf.attrib if conf is not None else {}

    def put(label, text):
        if text not in (None, ""):
            out[label] = " ".join(str(text).split())

    put("Microscope", f"{hw.attrib.get('SystemTypeName', '')} {c.get('MicroscopeModel', '')}")
    put("Software", hw.attrib.get("Software"))
    put("Objective", c.get("ObjectiveName"))
    for label, key in (("Magnification", "Magnification"), ("Numerical aperture", "NumericalAperture"),
                       ("Immersion", "Immersion"), ("Refraction index", "RefractionIndex"), ("Zoom", "Zoom"),
                       ("Scan mode", "ScanMode"), ("Scan speed (Hz)", "ScanSpeed"),
                       ("Scan direction", "ScanDirectionXName"), ("Line average", "LineAverage"),
                       ("Frame average", "FrameAverage"), ("Line accumulation", "Line_Accumulation"),
                       ("Frame accumulation", "FrameAccumulation")):
        put(label, c.get(key))
    if c.get("PinholeAiry"):
        put("Pinhole (Airy units)", _num(c["PinholeAiry"]))
    if c.get("Pinhole"):
        put("Pinhole (µm)", _num(c["Pinhole"], scale=1e6))
    if c.get("PixelDwellTime"):
        put("Pixel dwell time (µs)", _num(c["PixelDwellTime"], scale=1e6))
    if conf is None:
        return
    ## a sequential scan has one block per sequence, which holds only what differs from the master block of the scan:
    ## a detector gain that the sequence block does not give comes from the master block when the detector is active there
    master = hw.find("LDM_Block_Sequential/LDM_Block_Sequential_Master/ATLConfocalSettingDefinition")
    master = conf if master is None else master
    blocks = hw.findall("LDM_Block_Sequential/LDM_Block_Sequential_List/ATLConfocalSettingDefinition")
    active = {d.attrib.get("Name"): d.attrib for d in master.iter("Detector") if d.attrib.get("IsActive") == "1"}
    master_bands = {b.attrib.get("Channel"): b.attrib for b in master.iter("MultiBand")}
    for n, block in enumerate(blocks or [master], 1):
        first = f"{block.attrib.get('UserSettingName') or f'Seq. {n}'}: " if blocks else ""
        lasers = {}
        for line in block.iter("LaserLineSetting"):   ## the lines in use: an intensity above 0
            if float(line.attrib.get("IntensityDev", 0)) > 0:
                lasers.setdefault(line.attrib.get("LaserLine", "?"), []).append(_num(line.attrib["IntensityDev"]))
        for line, values in lasers.items():
            put(f"{first}Laser {line} nm (%)", " / ".join(dict.fromkeys(values)))
        bands = {b.attrib.get("Channel"): b.attrib for b in block.iter("MultiBand")} or master_bands
        for det in block.iter("Detector"):
            d = det.attrib
            if d.get("IsActive") != "1":
                continue
            d = {**active.get(d.get("Name"), {}), **d}
            words = [f"{word} {_num(d[key])}" for word, key in (("gain", "Gain"), ("offset", "Offset")) if d.get(key)]
            band = bands.get(d.get("Channel"), {})
            if band.get("LeftWorld") and band.get("RightWorld"):
                words.append(f"{_num(band['LeftWorld'], 4)} to {_num(band['RightWorld'], 4)} nm")
            if band.get("DyeName"):
                words.append(band["DyeName"])
            put(f"{first}Detector {d.get('Name', d.get('Channel', '?'))}", ", ".join(words) or "active")


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
    """ The step of one axis, in metres (in seconds for T): |last - first| / (N - 1); a top-to-bottom stack has a
    negative span """
    values = coords.get(axis)
    return abs(float(values[-1]) - float(values[0])) / (len(values) - 1) if values is not None and len(values) > 1 else 0


def _lif_channel_names(im):
    """ The dye name when the file has one, else the colour of the channel in LAS X (its LUT, for example Green),
    else Channel n. liffile names a channel with no dye Ch0, Ch1... """
    described = sorted(im.xml_element.findall("./Data/Image/ImageDescription/Channels/ChannelDescription"),
                       key=lambda c: int(c.attrib.get("BytesInc", 0)))   ## liffile's channel order
    names = []
    channels = im.coords.get("C")
    if channels is None:
        channels = getattr(im, "_channel_names", None)
    if channels is None:
        channels = [c.findtext("ChannelProperty[Key='DyeName']/Value", "").removeprefix("Leica/")
                    or c.attrib.get("LUTName", "") for c in described]
    for i, name in enumerate(str(c) for c in channels):
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
                     dtype=str(img.dtype), pixel_size=float(sizes.X or 0), pixel_size_y=float(sizes.Y or 0),
                     z_step=float(sizes.Z or 0))
