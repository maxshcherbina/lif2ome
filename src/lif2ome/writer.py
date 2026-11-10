"""Write one OME-TIFF file."""
from pathlib import Path

import tifffile


def write_ome(path, data, axes, channel_names=None, pixel_size=0.0, z_step=0.0):
    """ One OME-TIFF, zlib compressed (lossless). axes names the axes of data, for example CZYX. pixel_size and z_step
    are in µm; 0 leaves the size out of the file """
    path = Path(path)
    meta = {"axes": axes}
    if channel_names:
        meta["Channel"] = {"Name": list(channel_names)}
    if pixel_size:
        meta.update(PhysicalSizeX=pixel_size, PhysicalSizeY=pixel_size)
    if z_step and "Z" in axes:
        meta["PhysicalSizeZ"] = z_step
    part = path.with_name(path.name + ".part")   ## a cut write leaves no file that looks whole
    try:
        tifffile.imwrite(part, data, ome=True, compression="zlib", photometric="minisblack", metadata=meta)
        part.replace(path)
    finally:
        part.unlink(missing_ok=True)
