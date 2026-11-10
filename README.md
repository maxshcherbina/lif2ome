# lif2ome

`lif2ome` converts Leica `.lif` files to OME-TIFF files.

## What it does

- It writes one OME-TIFF file for each series of a `.lif` file.
- It keeps all channels and all pixels. The compression is zlib, which is lossless.
- It writes the pixel size, the Z step and the channel names into each file.
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

You can give more than one file. The command prints one line for each series:

```
My experiment.lif
  [1/12] Series001: ok, CZYX (3, 15, 1024, 1024) uint8, channels ['DAPI', 'Green', 'Red'], 0.2841 µm/px, z step 1.0000 µm, 0.14 GB, pixels match, about 40 s left
  [2/12] Series002: already there
  [3/12] TileScan 1: SKIPPED: tile scan with 16 tiles, skipped; convert the merged series, or use --tiles for one file per tile
Done: 10 converted, 1 already there, 1 skipped, 0 failed
```

Options:

| Option | What it does |
|---|---|
| `--only "Series003"` | Converts the series with this name only. |
| `--tiles` | Writes one file for each tile of a tile scan. |
| `--out FOLDER` | Writes the files in this folder. |

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
lif2ome.convert("file.lif")                     # the whole file, as the command does
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
