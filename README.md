# lif2ome

`lif2ome` converts Leica `.lif` files to OME-TIFF files.

## What it does

- It writes one OME-TIFF file for each series of a `.lif` file.
- It keeps all channels and all pixels. The compression is zlib, which is lossless.
- It writes the pixel size, the Z step and the channel names into each file.
- It keeps the microscope settings: each file holds the full Leica XML of its series and a short readable list.
- It writes fields of your own into each file, for example the genotype.
- It reads each new file back and compares it with the pixels of the `.lif` file.
- It leaves a file alone when the file is there already. Thus a stopped run continues from the next series.

The files are standard OME-TIFF. `tifffile` and `bioio` open them; both are tested. Fiji, napari and QuPath read this
format, but no test has opened a `lif2ome` file in them yet.

## Install

You need [uv](https://docs.astral.sh/uv/).

```bash
uv tool install ~/dev/lif2ome
```

In a project of your own, add it as a library:

```bash
uv add ~/dev/lif2ome
```

## The command

```bash
lif2ome "/path/to/My experiment.lif"
```

You can give more than one file. In a terminal, one line shows the step in work on the series:

```
  [2/12] Series002: 0.48 GB, writing (step 2 of 3) ...
```

When the series is done, its result takes the place of that line. At the end, a summary shows the output folder, the
number of files in it, their size, and each series that was not converted:

```
My experiment.lif
  [1/12] Series001: ok, CZYX (3, 15, 1024, 1024) uint8, channels ['DAPI', 'Green', 'Red'], 0.2841 µm/px, z step 1.0000 µm, 0.14 GB, pixels match, about 40 s left
  [2/12] Series002: already there
  [3/12] TileScan 1: SKIPPED: tile scan with 16 tiles, skipped; convert the merged series, or use --tiles for one file per tile

Done in 48 s: 10 converted, 1 already there, 1 skipped, 0 failed
/path/to/My experiment
  now holds 11 OME-TIFF files, 1.52 GB
Not converted:
  My experiment.lif: [3/12] TileScan 1: SKIPPED: tile scan with 16 tiles, skipped; convert the merged series, or use --tiles for one file per tile
```

Options:

| Option | What it does |
|---|---|
| `--only "Series003"` | Converts the series with this name only. |
| `--tiles` | Writes one file for each tile of a tile scan. |
| `--out FOLDER` | Writes the files in this folder. |
| `--meta genotype=Ecad` | Writes a field of your own into each new file. Give it again for more fields. |
| `--inspect` | Converts nothing. Prints the metadata of `.ome.tif` files, or the settings of each series of `.lif` files. |

## The output names

The files go in a folder that has the name of the `.lif` file, beside the `.lif` file:

```
My experiment.lif
My experiment/
    My experiment - Series001.ome.tif
    My experiment - Series002.ome.tif
    My experiment - TileScan 1 - tile 01.ome.tif     (with --tiles only)
```

- These characters in a series name become `_`: `< > : " / \ | ? *`.
- When two series have one name, the second file gets ` (2)` after the name.
- A file that fails the pixel check gets the name `... .ome.tif.BAD`.

## What is in a file

| Series | Axes in the file |
|---|---|
| A stack, or a single image | `CZYX` |
| A movie | `CTYX` |
| A movie of stacks | `TCZYX` |

A channel name is the dye name when the `.lif` file has one. If not, it is the colour of the channel in LAS X (for
example `Green`). If the file has no colour, it is `Channel 1`, `Channel 2`.

## The metadata in a file

Each file holds three things in its OME header, so they stay with the image:

1. **Your fields.** You give them with `--meta KEY=VALUE`, or with `metadata={...}` in Python.
2. **The microscope settings, as a short readable list.** The objective, the zoom, the pinhole, the scan speed, the
   averages, and the lasers and detectors of each scan sequence.
3. **The full Leica XML of the series.** It has every setting that the `.lif` file holds for the series.

Fiji, QuPath and OMERO show the first two as lists of key and value. To see them in the terminal:

```bash
lif2ome --inspect "My experiment/My experiment - Series001.ome.tif"
```

```
My experiment - Series001.ome.tif
  Your fields
    genotype  Ecad
  Microscope settings
    Source file            My experiment.lif
    Series                 Series001
    Acquired               2026-10-26 20:07:35
    Microscope             TCS SP8 DMI8-CS
    Objective              HC PL APO CS2 40x/1.30 OIL
    Zoom                   0.75
    Pinhole (Airy units)   1
    Seq. 1: Laser 488 nm (%)    1
    Seq. 1: Detector HyD 2      gain 100, offset -0.00667, 493 to 547 nm, Leica/EGFP
    Seq. 2: Laser 552 nm (%)    3
    Seq. 2: Detector HyD 4      560.4 to 619.9 nm
  Leica XML: 33,214 characters
```

`lif2ome --inspect "My experiment.lif"` prints the same list for each series of a `.lif` file, with no conversion.

- The short list is a help for a person. When you need a setting that is not in it, read the Leica XML.
- A detector shows no gain when the `.lif` file gives no gain for it in that sequence.
- A file that is there already is left alone, so it gets no new metadata. Delete the file and run the command again.

## In a container

The `Dockerfile` makes an image that runs the command. Mount the folder that holds the `.lif` files at `/data`.

Build the image one time, in this folder:

```bash
docker build -t lif2ome .
```

Run it:

```bash
docker run --rm -v "/path/to/folder":/data lif2ome "My experiment.lif"
```

On a Mac with Apple's `container` tool, the commands are `container build -t lif2ome .` and:

```bash
container run --rm -m 8g -v "/path/to/folder":/data lif2ome "My experiment.lif"
```

- `-m 8g` is necessary with `container`: its default is 1 GB of memory.
- Add `-t` after `run` to see the step in work on a series. Without `-t`, a line comes only when a series is done.
- The command needs about 2.5 times the size of the largest series. A 4.5 GB series used 11 GB. For such a file, give
  `-m 16g`.
- A container that has too little memory stops with no message. Run the command again with more memory; the files that
  are there stay.
- An image built on an Apple silicon Mac is for ARM chips. On a PC with an Intel or AMD chip, build the image on that PC.

What is tested: the `container` commands, on an Apple silicon Mac. The `docker` commands have no test yet. Apptainer has no test yet.

## In Python

```python
import lif2ome

lif2ome.series_list("file.lif")                 # the names of the series
info = lif2ome.read_info("file.lif", 0)         # axes, shape, channels, pixel_size and z_step in µm
data, axes = lif2ome.read_series(info)          # the pixels, axes in the order T, C, Z, Y, X
lif2ome.write_ome("out.ome.tif", data, axes, info.channels, info.pixel_size, info.z_step)
lif2ome.convert("file.lif", metadata={"genotype": "Ecad"})   # the whole file, as the command does

meta = lif2ome.read_metadata("out.ome.tif")    # {"fields": {...}, "settings": {...}, "leica_xml": "..."}
lif2ome.read_settings("file.lif", 0)           # the short readable list of one series
lif2ome.read_xml("file.lif", 0)                # the full Leica XML of one series
```

## Limits

- A tile scan is skipped unless you give `--tiles`. LAS X usually saves the merged image as one more series.
- A cross-section (an XZ scan) is skipped.
- The command needs memory of about 2.5 times the size of the largest series, for the pixel check.
- The time between frames of a movie is not written.
- `--tiles` writes the tiles as they are. It does not merge them.
- A `.czi` file works only with the extra `lif2ome[czi]` (bioio). This has no test with a real file.

## Tests

```bash
uv run pytest
```

The tests need no microscope file. To test a real file also:

```bash
LIF2OME_TEST_LIF="/path/to/file.lif" uv run pytest -s
```
