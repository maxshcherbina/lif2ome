# lif2ome

Convert Leica `.lif` files to OME-TIFF, one file per series. Every file is read back after it is written and compared
with the pixels that were read from the `.lif`.

## Features

- Writes one OME-TIFF per series, with every channel. Compression is zlib, so no pixel value changes.
- Stores the pixel size, the Z step, the time between frames and the channel names in each file.
- Keeps the microscope settings: each file carries the full Leica XML of its series, plus a short summary you can read.
- Stores your own fields in each file, such as the genotype.
- Reads every new file back and checks that it holds the same pixels that were read from the `.lif`.
- Merges a tile scan that LAS X left unmerged into one image.
- Skips files that already exist, so an interrupted run picks up at the next series.

The output is standard OME-TIFF, the format that Fiji, napari, QuPath and bioio read.

## Installation

You need [uv](https://docs.astral.sh/uv/).

```bash
uv tool install git+https://github.com/maxshcherbina/lif2ome
```

To use it as a library in your own project:

```bash
uv add git+https://github.com/maxshcherbina/lif2ome
```

## Usage

```bash
lif2ome "/path/to/My experiment.lif"
```

You can pass several files, or a folder. A folder stands for every `.lif` in it and in its subfolders; names that
start with `.` or `@` are left out. An empty folder is not an error.

```bash
lif2ome /path/to/folder
```

While a series is being converted, a status line shows which step it is on:

```
  working   Series002  0.48 GB, writing (step 2 of 3) ...
```

The result replaces that line when the series finishes. `ok` means the file was written, read back,
and found to hold the pixels that were read. Each file ends with a summary line, and the run ends with the totals, the output folders, and the
series that were not converted:

```
[1/2] My experiment.lif   3 series, 1.52 GB
  ok        Series001   0.49 GB -> 0.14 GB   12 s, about 30 s left
  there     Series002
  skipped   TileScan 1  tile scan with 16 tiles; the file has the merged series 'TileScan 1_Merged'
  file done: 1 converted, 1 already there, 1 skipped in 12 s.   Total: 3 of 7 series, about 30 s left

[2/2] Second experiment.lif   4 series, 1.94 GB
  ok        Series001   0.49 GB -> 0.14 GB   8 s, about 24 s left
  ...

Done in 48 s: 5 converted, 1 already there, 1 skipped, 0 failed
/path/to/My experiment
  now holds 2 OME-TIFF files, 0.28 GB
Not converted:
  My experiment.lif: TileScan 1: skipped, tile scan with 16 tiles; the file has the merged series 'TileScan 1_Merged'
```

Options:

| Option | What it does |
|---|---|
| `--only "Series003"` | Convert only the series with this name. |
| `--tiles` | Write one file per tile of a tile scan, and do not merge it. |
| `--out FOLDER` | Write the files to this folder. |
| `--meta genotype=Ecad` | Store your own field in each new file. Repeat the option to add more. |
| `--verbose` | Add the axes, the shape, the channels and the pixel sizes under each converted series. |
| `--inspect` | Convert nothing. Print the metadata of `.ome.tif` files, or the settings of each series in a `.lif`. |

## Output files

Files go in a folder named after the `.lif`, next to it:

```
My experiment.lif
My experiment/
    My experiment - Series001.ome.tif
    My experiment - Series002.ome.tif
    My experiment - TileScan 1 - tile 01.ome.tif     (with --tiles only)
```

- Characters that are not allowed in file names (`< > : " / \ | ? *`) become `_`.
- If two series share a name, the second file gets ` (2)` appended.
- A file that fails the pixel check is renamed to end in `.ome.tif.BAD`.

### Axes and channel names

| Series | Axes in the file |
|---|---|
| A stack, or a single image | `CZYX` |
| A movie | `CTYX` |
| A movie of stacks | `TCZYX` |
| A cross-section (XZ scan) | `CZYX`, where Y of the file is Z of the microscope |

A channel is named after its dye if the `.lif` records one. Otherwise it takes the channel colour from LAS X, such as
`Green`. With neither, it is `Channel 1`, `Channel 2`, and so on.

## Tile scans

A tile scan that was merged in LAS X is one large image, and it converts like any other series.

A tile scan that was left unmerged holds one image per tile. `lif2ome` merges it into one file:

1. It reads the stage position of every tile from the `.lif`. The grid and the overlap follow from those positions, so
   a scan taken with 10% overlap and one taken with 20% both work.
2. It matches the picture in each overlap between neighbouring tiles, and moves every tile to the place that fits all
   its overlaps best. A stage does not return to exactly the same place on each row, so the positions alone leave
   visible steps at the seams.
3. It places the tiles on one canvas. Where two tiles overlap, the pixels of one tile are kept as they are. No pixel
   value is averaged or changed.

The progress line and the settings summary in the file say what was done:

```
  ok        TileScan 2   3.89 GB -> 0.87 GB   2 min 03 s   merged 14 x 16 tiles, overlap 10% by 10%
```

```
Tile scan    14 x 16 tiles, overlap 10% by 10%; merged by lif2ome from the stage positions, each tile moved to
             fit its overlaps: 418 of 418 overlaps matched, largest move 70 px
```

- If few overlaps match, the line says `CHECK THE MERGE`. The tiles then sit where the stage put them.
- If the file already holds the merged image, a series named after the tile scan with "merged" in its name, the
  unmerged scan is skipped.
- A tile that lies in no row and no column of the grid is left out of the merge. The settings summary says so, and
  `--tiles` writes it.
- `--tiles` writes one file per tile instead.

## Metadata

Three things are stored in the OME header of each file, so they travel with the image:

1. **Your fields.** Set them with `--meta KEY=VALUE`, or with `metadata={...}` in Python.
2. **A summary of the microscope settings.** Objective, zoom, pinhole, scan speed, averaging, and the lasers and
   detectors of each scan sequence.
3. **The full Leica XML of the series.** Everything the `.lif` records about that series.

The first two are OME map annotations, which Fiji, QuPath and OMERO display as key-value tables. To print them in a
terminal:

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

`lif2ome --inspect "My experiment.lif"` prints the same summary for every series in a `.lif`, without converting anything.

- The summary covers the settings people usually ask about. For anything else, read the Leica XML.
- A detector is listed without a gain when the `.lif` records none for that sequence.
- Existing files are skipped, so they keep whatever metadata they had. To add metadata to one, delete it and run the
  command again.

## Docker

The `Dockerfile` builds an image that runs the command. Mount the folder with your `.lif` files at `/data`.

Build the image once, from this folder:

```bash
docker build -t lif2ome .
```

Run it:

```bash
docker run --rm -v "/path/to/folder":/data lif2ome "My experiment.lif"
```

The container only sees the mounted folder, as `/data`. To convert every `.lif` in it, pass `.`:

```bash
docker run --rm -v "/path/to/folder":/data lif2ome .
```

With Apple's `container` tool on a Mac, build with `container build -t lif2ome .` and run:

```bash
container run --rm -m 8g -v "/path/to/folder":/data lif2ome "My experiment.lif"
```

- `container` gives a container 1 GB of memory by default, so `-m 8g` is required.
- Conversion needs about 2.5 times the size of the largest series in memory. A 4.5 GB series peaked at 11 GB; use
  `-m 16g` for files like that.
- A container that runs out of memory stops without an error message. Run it again with more memory. Finished files
  are kept.
- Add `-t` after `run` to see the status line. Without it, output appears only when a series finishes.
- An image built on an Apple silicon Mac only runs on ARM. Build on the machine that will run it, or pass
  `--arch amd64` to `container build` for Intel and AMD machines.

## Python API

```python
import lif2ome

lif2ome.series_list("file.lif")                 # the names of the series
info = lif2ome.read_info("file.lif", 0)         # axes, shape, channels, pixel_size and z_step in µm
data, axes = lif2ome.read_series(info)          # the pixels, axes in the order T, C, Z, Y, X
lif2ome.write_ome("out.ome.tif", data, axes, info.channels, info.pixel_size, info.z_step)
lif2ome.convert("file.lif", metadata={"genotype": "Ecad"})   # a whole file, like the command

meta = lif2ome.read_metadata("out.ome.tif")    # {"fields": {...}, "settings": {...}, "leica_xml": "..."}
lif2ome.read_settings("file.lif", 0)           # the settings summary of one series
lif2ome.read_xml("file.lif", 0)                # the full Leica XML of one series
```

## Limitations

- A merged tile scan has no blend at the seams, and the tiles keep their own brightness. A tile that is brighter in
  the middle than at its edge leaves a faint grid, as it does in a LAS X merge without smoothing.
- A tile scan with flipped or swapped scan axes is not merged. Use `--tiles` for it.
- A cross-section (XZ scan) is stored with its depth axis as Y, because a plane of an OME-TIFF is always Y by X.
  The Y pixel size in the file is then the depth step.
- A whole series is held in memory, plus a second copy for the pixel check.
- `.czi` support is experimental. It needs the `lif2ome[czi]` extra, which uses bioio.

## Development

```bash
uv run pytest
```

The tests run without a microscope file. To include a real one:

```bash
LIF2OME_TEST_LIF="/path/to/file.lif" uv run pytest -s
```
