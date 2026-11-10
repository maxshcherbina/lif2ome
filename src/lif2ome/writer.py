"""Write one OME-TIFF file, and read the metadata of a file back.

The metadata that OME has no field for goes in annotations in the OME header, so it stays with the image:
the fields of the user (key and value), the microscope settings in readable form (key and value), and the full Leica
XML of the series (text). Fiji, QuPath and OMERO show the key and value lists.
"""
from pathlib import Path
from xml.etree import ElementTree

import tifffile

NAMESPACE = "lif2ome/"   ## fields, settings, leica-xml


def write_ome(path, data, axes, channel_names=None, pixel_size=0.0, z_step=0.0, metadata=None, settings=None,
              leica_xml=None):
    """ One OME-TIFF, zlib compressed (lossless). axes names the axes of data, for example CZYX. pixel_size and z_step
    are in µm; 0 leaves the size out of the file. metadata: the fields of the user, a dict. settings: the microscope
    settings in readable form, a dict. leica_xml: the Leica XML of the series, a text. read_metadata gives them back """
    path = Path(path)
    meta = {"axes": axes}
    if channel_names:
        meta["Channel"] = {"Name": list(channel_names)}
    if pixel_size:
        meta.update(PhysicalSizeX=pixel_size, PhysicalSizeY=pixel_size)
    if z_step and "Z" in axes:
        meta["PhysicalSizeZ"] = z_step
    maps = [{"Namespace": NAMESPACE + name, "Value": {str(k): str(v) for k, v in pairs.items()}}
            for name, pairs in (("fields", metadata), ("settings", settings)) if pairs]
    if maps:
        meta["MapAnnotation"] = maps
    if leica_xml:
        meta["CommentAnnotation"] = {"Namespace": NAMESPACE + "leica-xml", "Value": leica_xml}
    part = path.with_name(path.name + ".part")   ## a cut write leaves no file that looks whole
    try:
        tifffile.imwrite(part, data, ome=True, compression="zlib", photometric="minisblack", metadata=meta)
        part.replace(path)
    finally:
        part.unlink(missing_ok=True)


def read_metadata(path):
    """ {"fields": {...}, "settings": {...}, "leica_xml": "..."} of an OME-TIFF that lif2ome wrote; each is empty
    when the file does not have it """
    out = {"fields": {}, "settings": {}, "leica_xml": ""}
    with tifffile.TiffFile(path) as tif:
        xml = tif.ome_metadata
    if not xml:
        return out
    for el in ElementTree.fromstring(xml).iter():
        name = el.attrib.get("Namespace", "")
        if not name.startswith(NAMESPACE):
            continue
        value = next((child for child in el if child.tag.endswith("Value")), None)
        if value is None:
            continue
        if name == NAMESPACE + "leica-xml":
            out["leica_xml"] = value.text or ""
        elif name.removeprefix(NAMESPACE) in out:
            out[name.removeprefix(NAMESPACE)] = {m.attrib["K"]: m.text or "" for m in value}
    return out
