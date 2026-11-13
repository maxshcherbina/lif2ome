"""Leica .lif files to OME-TIFF files.

    series_list(path)            the names of the series of a file
    read_info(path, series)      what one series holds: axes, shape, channel names, pixel size, Z step
    read_series(info)            the pixels of one series, with the axes in OME order
    write_ome(path, data, ...)   one OME-TIFF, zlib compressed (lossless)
    convert(path)                every series of a file; each new file is read back and compared with the pixels that were read
    read_settings(path, series)  the main microscope settings of one series, in readable form
    read_xml(path, series)       the full Leica XML of one series
    read_metadata(ome_path)      your fields, the settings and the Leica XML of a file that lif2ome wrote

reader.py reads, writer.py writes, stitch.py merges the tiles of a tile scan, converter.py does a whole file,
cli.py is the lif2ome command.
"""
from .cli import main
from .converter import convert, out_path
from .reader import (
           ImageInfo,
           Unsupported,
           guess_kind,
           read_info,
           read_part,
           read_series,
           read_settings,
           read_xml,
           series_list,
           series_paths,
           to_cnyx,
)
from .writer import read_metadata, write_ome

__all__ = [
           "ImageInfo",
           "Unsupported",
           "convert",
           "guess_kind",
           "main",
           "out_path",
           "read_info",
           "read_metadata",
           "read_part",
           "read_series",
           "read_settings",
           "read_xml",
           "series_list",
           "series_paths",
           "to_cnyx",
           "write_ome",
]
