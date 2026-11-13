"""Write one OME-TIFF file, and read the metadata of a file back.

The metadata that OME has no field for goes in annotations in the OME header, so it stays with the image:
the fields of the user (key and value), the microscope settings in readable form (key and value), and the full Leica
XML of the series (text). Fiji, QuPath and OMERO show the key and value lists.
"""
from pathlib import Path
from xml.etree import ElementTree

import numpy as np
import tifffile

NAMESPACE = "lif2ome/"   ## fields, settings, leica-xml


def write_ome(path, data, axes, channel_names=None, pixel_size=0.0, z_step=0.0, metadata=None, settings=None,
              leica_xml=None, pixel_size_y=0.0, time_step=0.0, *, channel_colors=None, plane_times=None, streaming=False):
    """ One OME-TIFF, zlib compressed (lossless). axes names the axes of data, for example CZYX. pixel_size and z_step
    are in µm; 0 leaves the size out of the file. pixel_size_y: the Y step in µm when it is not the X step. time_step:
    the time between frames in seconds. metadata: the fields of the user, a dict. settings: the microscope
    settings in readable form, a dict. leica_xml: the Leica XML of the series, a text. read_metadata gives them back.
    channel_colors: one RGB tuple per channel, or None for an unknown colour.
    plane_times: seconds from the first stamp, in leading-axis order.
    streaming: iterate planes with bounded compression buffering instead of passing an array """
    path = Path(path)
    meta = {"axes": axes}
    if channel_names:
        meta["Channel"] = {"Name": list(channel_names)}
    if channel_colors:
        channels = [{"Name": name} for name in channel_names] if channel_names else [{} for _ in channel_colors]
        for channel, color in zip(channels, channel_colors):
            if color is not None:
                channel["Color"] = int.from_bytes(bytes((*color, 255)), "big", signed=True)
        meta["Channel"] = channels
    if pixel_size:
        meta.update(PhysicalSizeX=pixel_size, PhysicalSizeY=pixel_size_y or pixel_size)
    if z_step and "Z" in axes:
        meta["PhysicalSizeZ"] = z_step
    if time_step and "T" in axes:
        meta.update(TimeIncrement=time_step, TimeIncrementUnit="s")
    if plane_times is not None:
        times = np.asarray(plane_times).ravel().tolist()
        if len(times) != int(np.prod(data.shape[:-2])):
            raise ValueError("plane times must have one value per plane")
        meta["Plane"] = {"DeltaT": times, "DeltaTUnit": ["s"] * len(times)}
    maps = [{"Namespace": NAMESPACE + name, "Value": {str(k): str(v) for k, v in pairs.items()}}
            for name, pairs in (("fields", metadata), ("settings", settings)) if pairs]
    if maps:
        meta["MapAnnotation"] = maps
    if leica_xml:
        meta["CommentAnnotation"] = {"Namespace": NAMESPACE + "leica-xml", "Value": leica_xml}
    part = path.with_name(path.name + ".part")   ## a cut write leaves no file that looks whole
    try:
        ## Compressed iterators do not trigger tifffile's automatic BigTIFF selection.
        # tifffile compresses iterator strips on its own ordered thread pool.
        # Bound its batch size instead of forcing serial compression.
        options = {"buffersize": 8 * 1024**2} if streaming else {}
        tifffile.imwrite(part, iter_planes(data) if streaming else data,
                         shape=data.shape, dtype=data.dtype, ome=True,
                         compression="zlib", photometric="minisblack", metadata=meta, **options,
                         bigtiff=data.nbytes > 2**32 - 2**25)
        part.replace(path)
    finally:
        part.unlink(missing_ok=True)


def iter_planes(data):
    """ Yield planes without flattening a transposed series into a copy. """
    for index in np.ndindex(data.shape[:-2]):
        yield data[index]


def read_metadata(path):
    """ The fields, settings, Leica XML, channel names (channels) and RGB colours (colors) of an OME-TIFF.
    Pixel sizes are in µm. time_step and plane_times are in seconds. Missing sizes are 0.
    plane_times lists DeltaT in page order, or is empty when absent.
    Missing metadata is empty. A channel with no colour has None """
    out = {"fields": {}, "settings": {}, "leica_xml": "", "channels": [], "colors": [],
           "pixel_size": 0.0, "pixel_size_y": 0.0, "z_step": 0.0, "time_step": 0.0, "plane_times": []}
    with tifffile.TiffFile(path) as tif:
        xml = tif.ome_metadata
    if not xml:
        return out
    for el in ElementTree.fromstring(xml).iter():
        if el.tag.rsplit("}", 1)[-1] == "Pixels":
            for key, attr in (("pixel_size", "PhysicalSizeX"), ("pixel_size_y", "PhysicalSizeY"),
                              ("z_step", "PhysicalSizeZ"), ("time_step", "TimeIncrement")):
                out[key] = float(el.attrib.get(attr, 0))
        if el.tag.rsplit("}", 1)[-1] == "Channel":
            out["channels"].append(el.attrib.get("Name", ""))
            color = el.attrib.get("Color")
            out["colors"].append(tuple((int(color) >> shift) & 255 for shift in (24, 16, 8))
                                 if color is not None else None)
        if el.tag.rsplit("}", 1)[-1] == "Plane" and "DeltaT" in el.attrib:
            out["plane_times"].append(float(el.attrib["DeltaT"]))
        name = el.attrib.get("Namespace", "")
        if not name.startswith(NAMESPACE):
            continue
        value = next((child for child in el if child.tag.endswith("Value")), None)
        if value is None:
            continue
        if name == NAMESPACE + "leica-xml":
            out["leica_xml"] = value.text or ""
        elif name.removeprefix(NAMESPACE) in ("fields", "settings"):
            out[name.removeprefix(NAMESPACE)] = {m.attrib["K"]: m.text or "" for m in value}
    return out
