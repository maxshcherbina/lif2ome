"""Leica .lif files to OME-TIFF files.

    series_list(path)            the names of the series of a file
    read_info(path, series)      what one series holds: axes, shape, channel names, pixel size, Z step
    read_series(info)            the pixels of one series, with the axes in OME order
    write_ome(path, data, ...)   one OME-TIFF, zlib compressed (lossless)
    convert(path)                every series of a file, each one checked against the source pixels

reader.py reads, writer.py writes, converter.py does a whole file, cli.py is the lif2ome command.
"""
from .cli import main
from .converter import convert, out_path
from .reader import (
           ImageInfo,
           guess_kind,
           read_info,
           read_part,
           read_series,
           series_list,
           to_cnyx,
)
from .writer import write_ome

__all__ = [
           "ImageInfo",
           "convert",
           "guess_kind",
           "main",
           "out_path",
           "read_info",
           "read_part",
           "read_series",
           "series_list",
           "to_cnyx",
           "write_ome",
]
