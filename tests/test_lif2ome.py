"""No test needs a microscope file: convert() runs on a made-up file through a patched reader.
A real file: LIF2OME_TEST_LIF=/path/to/file.lif uv run pytest"""
import os
import tracemalloc
from pathlib import Path
from xml.etree import ElementTree

import numpy as np
import pytest
import tifffile

import lif2ome
from lif2ome import converter, reader, stitch


def stack(shape=(2, 3, 8, 8), dtype=np.uint16):
    return np.arange(np.prod(shape), dtype=dtype).reshape(shape)


def test_status_line_fits_terminal_width():
    from lif2ome.cli import _status_line

    text = "  working   C             3.88 GB, reading and merging 224 tiles (step 1 of 3)"
    assert _status_line(text, 80) == (text + " ...")[:79]
    assert len(_status_line(text, 80)) == 79
    assert _status_line("  working   C", 80) == "  working   C ..."


def test_write_ome_keeps_pixels_names_and_sizes(tmp_path):
    data, f = stack(), tmp_path / "x.ome.tif"
    lif2ome.write_ome(f, data, "CZYX", ["DAPI", "Green"], pixel_size=0.5, z_step=2.0)
    with tifffile.TiffFile(f) as tif:
        assert tif.series[0].axes == "CZYX"
        assert np.array_equal(tif.series[0].asarray(), data)
        assert tif.pages[0].compression.name == "ADOBE_DEFLATE"   ## zlib
        xml = tif.ome_metadata
    assert 'Name="DAPI"' in xml and 'Name="Green"' in xml
    assert 'PhysicalSizeX="0.5"' in xml and 'PhysicalSizeY="0.5"' in xml and 'PhysicalSizeZ="2.0"' in xml
    assert not list(tmp_path.glob("*.part"))


def test_pixel_check_finds_one_wrong_pixel(tmp_path):
    data, f = stack(), tmp_path / "x.ome.tif"
    lif2ome.write_ome(f, data, "CZYX")
    assert converter._same_pixels(f, data, "CZYX")
    wrong = data.copy()
    wrong[1, 2, 3, 4] += 1
    assert not converter._same_pixels(f, wrong, "CZYX")
    assert not converter._same_pixels(f, data.astype(np.uint32), "CZYX")


@pytest.mark.parametrize("dtype", [np.uint8, np.uint16])
@pytest.mark.parametrize("bigtiff", [False, True])
@pytest.mark.parametrize("axes, shape", [
    ("YX", (8, 9)),
    ("ZYX", (3, 8, 9)),
    ("CZYX", (2, 3, 8, 9)),
    ("CTYX", (2, 3, 8, 9)),
    ("TCZYX", (2, 3, 4, 8, 9)),
    ("TCZYX", (1, 2, 1, 8, 9)),
])
def test_pixel_check_page_order(tmp_path, dtype, bigtiff, axes, shape):
    data, f = stack(shape, dtype), tmp_path / "pages.ome.tif"
    # Match write_ome's options, forcing BigTIFF without a multi-GB fixture.
    tifffile.imwrite(f, data, ome=True, compression="zlib", photometric="minisblack",
                     metadata={"axes": axes}, bigtiff=bigtiff)
    planes = data.reshape(-1, *shape[-2:])
    with tifffile.TiffFile(f) as tif:
        assert tif.is_bigtiff == bigtiff
        assert len(tif.pages) == len(planes)
        for page, plane in zip(tif.pages, planes):
            assert page.dtype == data.dtype
            np.testing.assert_array_equal(page.asarray(), plane)
    assert converter._same_pixels(f, data, axes)


def test_pixel_check_finds_wrong_last_pixel(tmp_path):
    data, f = stack(), tmp_path / "x.ome.tif"
    lif2ome.write_ome(f, data, "CZYX")
    data[-1, -1, -1, -1] += 1
    assert not converter._same_pixels(f, data, "CZYX")


@pytest.mark.parametrize("planes", [2, 4])
def test_pixel_check_rejects_plane_count(tmp_path, planes):
    data, f = np.zeros((3, 8, 9), dtype=np.uint16), tmp_path / "x.ome.tif"
    lif2ome.write_ome(f, data, "ZYX")
    assert not converter._same_pixels(f, np.zeros((planes, 8, 9), dtype=data.dtype), "ZYX")


def test_pixel_check_rejects_dtype(tmp_path):
    data, f = stack(dtype=np.uint8), tmp_path / "x.ome.tif"
    lif2ome.write_ome(f, data, "CZYX")
    assert not converter._same_pixels(f, data.astype(np.uint16), "CZYX")


def test_pixel_check_rejects_plane_shape(tmp_path):
    data, f = stack((3, 8, 9)), tmp_path / "x.ome.tif"
    lif2ome.write_ome(f, data, "ZYX")
    assert not converter._same_pixels(f, data.reshape(3, 9, 8), "ZYX")


def test_pixel_check_memory_is_less_than_a_series(tmp_path):
    data = np.zeros((32, 1024, 1024), dtype=np.uint16)  # 64 MiB, 2 MiB per plane
    f = tmp_path / "large.ome.tif"
    lif2ome.write_ome(f, data, "ZYX")
    tracemalloc.start()
    try:
        assert converter._same_pixels(f, data, "ZYX")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 16 * 1024**2, f"pixel check allocated {peak / 1024**2:.1f} MiB"


@pytest.mark.parametrize("axes, shape, kind, want, want_shape", [
    ("CZYX", (2, 3, 8, 8), "stack", "CZYX", (2, 3, 8, 8)),
    ("ZYX", (3, 8, 8), "stack", "CZYX", (1, 3, 8, 8)),
    ("CYX", (2, 8, 8), "image", "CZYX", (2, 1, 8, 8)),
    ("TCYX", (4, 2, 8, 8), "movie", "CTYX", (2, 4, 8, 8)),
    ("TCZYX", (4, 2, 1, 8, 8), "movie", "CTYX", (2, 4, 8, 8)),
    ("TCZYX", (4, 2, 3, 8, 8), "stack", "TCZYX", (4, 2, 3, 8, 8)),
])
def test_layout_and_round_trip(tmp_path, axes, shape, kind, want, want_shape):
    data, out_axes = converter._ome_layout(stack(shape), axes, kind)
    assert (out_axes, data.shape) == (want, want_shape)
    f = tmp_path / "x.ome.tif"
    lif2ome.write_ome(f, data, out_axes)
    assert converter._same_pixels(f, data, out_axes)


@pytest.mark.parametrize("axes, shape, kind", [
    ("TCZYX", (1, 2, 3, 8, 9), "stack"),
    ("TCZYX", (4, 1, 1, 8, 9), "movie"),
])
def test_layout_removes_singleton_axes_without_copy(axes, shape, kind):
    data = stack(shape)
    laid_out, _ = converter._ome_layout(data, axes, kind)
    assert laid_out.flags.c_contiguous
    assert np.shares_memory(laid_out, data)
    np.testing.assert_array_equal(laid_out.ravel(), data.ravel())


def test_out_path():
    assert lif2ome.out_path("/a/b.lif", "c/d:e") == Path("/a/b/b - c_d_e.ome.tif")
    assert lif2ome.out_path("/a/b.lif", "s", "/o") == Path("/o/b - s.ome.tif")


def test_to_cnyx():
    assert lif2ome.to_cnyx(stack((3, 2, 8, 8)), "ZCYX").shape == (2, 3, 8, 8)
    assert lif2ome.to_cnyx(stack((8, 8)), "YX").shape == (1, 1, 8, 8)
    with pytest.raises(ValueError):
        lif2ome.to_cnyx(stack((2, 3, 4, 8, 8)), "TCZYX")


@pytest.fixture
def fake(monkeypatch, tmp_path):
    """ A made-up file "f.lif": two series named A, a series with axes that are not read, a tile scan of 3 tiles, a movie of stacks """
    series = {0: ("A", "ZCYX", (3, 2, 8, 8)), 1: ("A", "CYX", (2, 8, 8)), 2: ("XZ", None, None),
              3: ("Tiles", "CZYX", (2, 3, 8, 8)), 4: ("Both", "TZCYX", (2, 3, 2, 8, 8))}
    reads = []

    def read_info(path, s, tiles=False):
        name, axes, shape = series[s]
        if axes is None:
            raise reader.Unsupported("axes L, X: only T, Z, C, Y and X are read")
        if name == "Tiles" and not tiles:
            raise reader.Unsupported("tile scan with 3 tiles, skipped; convert the merged series")
        return lif2ome.ImageInfo(str(path), axes, shape, lif2ome.guess_kind(axes, shape), ["a", "b"], series=s,
                                 series_name=name, dtype="uint16", pixel_size=0.1, z_step=0.5,
                                 tiles=3 if name == "Tiles" else 0, colors=[(0, 0, 255), (0, 255, 0)])

    def read_series(info, tile=None):
        reads.append((info.series, tile))
        data = stack(info.shape) + info.series + (tile or 0) * 7
        axes = "".join(ax for ax in "TCZYX" if ax in info.axes)
        return np.transpose(data, [info.axes.index(ax) for ax in axes]), axes

    monkeypatch.setattr(reader, "series_list", lambda path: [v[0] for v in series.values()])
    monkeypatch.setattr(reader, "series_paths", lambda path: ["20x Images/XZ" if n == "XZ" else n
                                                            for n in reader.series_list(path)])
    monkeypatch.setattr(reader, "read_info", read_info)
    monkeypatch.setattr(reader, "read_series", read_series)
    def no_positions(path, s):
        raise reader.Unsupported("the file gives no stage position for each tile")

    monkeypatch.setattr(reader, "read_tile_positions", no_positions)
    monkeypatch.setattr(reader, "read_settings", lambda path, s: {"Series": series[s][0], "Zoom": "0.75"})
    monkeypatch.setattr(reader, "read_xml", lambda path, s: f'<Element Name="{series[s][0]}"><A b="1 &lt; 2">é\n</A></Element>')
    return tmp_path / "f.lif", reads


def test_convert(fake):
    lif, reads = fake
    lines = []
    counts = lif2ome.convert(lif, report=lines.append)
    assert counts == {"ok": 3, "there": 0, "skipped": 2, "failed": 0}
    names = sorted(p.name for p in (lif.parent / "f").iterdir())
    assert names == ["f - A (2).ome.tif", "f - A.ome.tif", "f - Both.ome.tif"]
    assert lines[0].split()[:2] == ["ok", "A"] and " -> " in lines[0] and "CZYX" not in lines[0]
    assert "left" in lines[0] and "left" not in lines[-1]
    assert lines[2].split()[:2] == ["skipped", "20x"] and "axes L, X" in lines[2]
    assert lines[3].split()[:2] == ["skipped", "Tiles"] and "tile scan with 3 tiles" in lines[3]
    assert lines[1].index("A (2)") == lines[0].index("A")   ## the names start in one column
    with tifffile.TiffFile(lif.parent / "f" / "f - Both.ome.tif") as tif:
        assert tif.series[0].axes == "TCZYX" and tif.series[0].shape == (2, 2, 3, 8, 8)

    ## a second run leaves the files alone and reads no pixels
    reads.clear()
    assert lif2ome.convert(lif, report=lines.append)["there"] == 3
    assert reads == []


def test_convert_tiles_and_only(fake):
    lif, reads = fake
    lines = []
    assert lif2ome.convert(lif, only="Tiles", tiles=True, report=lines.append)["ok"] == 3
    names = sorted(p.name for p in (lif.parent / "f").iterdir())
    assert names == [f"f - Tiles - tile 0{i}.ome.tif" for i in (1, 2, 3)]
    assert reads == [(3, 0), (3, 1), (3, 2)]
    first, second = (tifffile.imread(lif.parent / "f" / n) for n in names[:2])
    assert not np.array_equal(first, second)


@pytest.mark.parametrize("raises", [False, True])
def test_failed_check_leaves_no_final_file(fake, monkeypatch, raises):
    lif, _ = fake
    out = lif2ome.out_path(lif, "Both")
    part = out.with_name(out.name + ".part")
    check = converter._same_pixels

    def fail(path, data, axes):
        assert path == part and part.exists() and not out.exists()
        if raises:
            raise KeyError("read-back")
        return False

    monkeypatch.setattr(converter, "_same_pixels", fail)
    lines = []
    assert lif2ome.convert(lif, only="Both", report=lines.append)["failed"] == 1
    assert not out.exists()
    if raises:
        assert lines[0].split()[0] == "FAILED" and "KeyError: 'read-back'" in lines[0]
    else:
        assert out.with_name(out.name + ".BAD").exists()
        assert not part.exists()

    ## a stopped run can leave a partial file behind
    part.write_bytes(b"unfinished")
    monkeypatch.setattr(converter, "_same_pixels", check)
    assert lif2ome.convert(lif, only="Both", report=lines.append)["ok"] == 1
    assert out.exists() and not part.exists()


@pytest.mark.parametrize("function", ["read_info", "read_series"])
def test_bad_series_does_not_stop_conversion(fake, monkeypatch, function):
    lif, _ = fake
    original = getattr(reader, function)

    def fail(*args):
        series = args[1] if function == "read_info" else args[0].series
        if series == 0:
            raise KeyError("MemBlock_397")
        return original(*args)

    monkeypatch.setattr(reader, function, fail)
    lines = []
    assert lif2ome.convert(lif, report=lines.append) == {"ok": 2, "there": 0, "skipped": 2, "failed": 1}
    assert lines[0].split()[:2] == ["FAILED", "A"] and "KeyError: 'MemBlock_397'" in lines[0]
    assert lif2ome.out_path(lif, "Both").exists()


def test_bad_file_does_not_stop_main(fake, monkeypatch, capsys):
    lif, _ = fake
    original = reader.series_list
    bad = lif.with_name("bad.lif")

    def series_list(path):
        if path == bad:
            raise KeyError("MemBlock_397")
        return original(path)

    monkeypatch.setattr(reader, "series_list", series_list)
    assert lif2ome.main([str(bad), str(lif)]) == 1
    text = capsys.readouterr().out
    assert "[1/2] bad.lif\n  FAILED    KeyError: 'MemBlock_397'" in text
    assert ": 3 converted, 0 already there, 2 skipped, 1 failed" in text


def test_only_missing_name_fails(fake, capsys):
    lif, reads = fake
    lines = []
    assert lif2ome.convert(lif, only="missing", report=lines.append) == {
        "ok": 0, "there": 0, "skipped": 0, "failed": 1}
    assert len(lines) == 1
    assert "no series has the name 'missing'" in lines[0]
    assert "A, A, 20x Images/XZ, Tiles, Both" in lines[0]
    assert reads == []
    assert lif2ome.main([str(lif), "--only", "missing"]) == 1
    assert "1 failed" in capsys.readouterr().out


def test_main_reports_a_missing_file(tmp_path, capsys):
    assert lif2ome.main([str(tmp_path / "none.lif")]) == 1
    assert "FAILED" in capsys.readouterr().out


@pytest.mark.skipif("LIF2OME_TEST_LIF" not in os.environ, reason="set LIF2OME_TEST_LIF to a real .lif file")
def test_real_file(tmp_path):
    lif = Path(os.environ["LIF2OME_TEST_LIF"])
    lines = []
    counts = lif2ome.convert(lif, out_dir=tmp_path, report=lines.append)
    print("\n".join(lines))
    assert counts["failed"] == 0 and counts["ok"] > 0


def test_status_and_summary(fake, capsys):
    lif, _ = fake
    steps = []
    lif2ome.convert(lif, only="Both", report=lambda line: None, status=steps.append)
    assert [s.split(", ")[-1] for s in steps] == ["reading (step 1 of 3)", "writing (step 2 of 3)",
                                                  "checking the pixels (step 3 of 3)"]
    assert steps[0].split()[:2] == ["working", "Both"]
    assert lif2ome.main([str(lif)]) == 0
    out = capsys.readouterr().out
    assert "now holds 3 OME-TIFF files" in out and str(lif.parent / "f") in out
    assert "Not converted:" in out and "f.lif: Tiles: skipped, tile scan with 3 tiles" in out
    assert "\x1b" not in out   ## no terminal codes when the output is not a terminal


def test_metadata_round_trip(fake, capsys):
    lif, _ = fake
    lif2ome.convert(lif, only="Both", report=lambda line: None, metadata={"genotype": "Ecad <Lariat>", "trial": 1})
    f = lif.parent / "f" / "f - Both.ome.tif"
    meta = lif2ome.read_metadata(f)
    assert meta["colors"] == [(0, 0, 255), (0, 255, 0)]
    assert meta["fields"] == {"genotype": "Ecad <Lariat>", "trial": "1"}
    assert meta["settings"] == {"Series": "Both", "Zoom": "0.75"}
    assert meta["leica_xml"] == '<Element Name="Both"><A b="1 &lt; 2">é\n</A></Element>'

    assert lif2ome.main(["--inspect", str(f)]) == 0
    out = capsys.readouterr().out
    assert "genotype  Ecad <Lariat>" in out and "Zoom    0.75" in out and "Leica XML: 53 characters" in out


def test_file_without_metadata(tmp_path):
    f = tmp_path / "x.ome.tif"
    lif2ome.write_ome(f, stack(), "CZYX")
    assert lif2ome.read_metadata(f) == {"fields": {}, "settings": {}, "leica_xml": "",
                                      "channels": ["", ""], "colors": [None, None],
                                      "pixel_size": 0.0, "pixel_size_y": 0.0, "z_step": 0.0, "time_step": 0.0}


def test_meta_option(fake, capsys):
    lif, _ = fake
    assert lif2ome.main([str(lif), "--only", "Both", "--meta", "genotype=Ecad", "--meta", "note=a=b"]) == 0
    assert lif2ome.read_metadata(lif.parent / "f" / "f - Both.ome.tif")["fields"] == {"genotype": "Ecad", "note": "a=b"}
    with pytest.raises(SystemExit):
        lif2ome.main([str(lif), "--meta", "genotype"])


def test_cross_section_axes():
    assert reader.cross_section_axes("ZX") == ("YX", True)
    assert reader.cross_section_axes("CZX") == ("CYX", True)
    assert reader.cross_section_axes("YZX") == ("ZYX", True)   ## a pile of cross-sections along Y
    assert reader.cross_section_axes("TCYZX") == ("TCZYX", True)
    assert reader.cross_section_axes("CZYX") == ("CZYX", False)


def test_time_step_and_y_size_are_written(tmp_path):
    f = tmp_path / "movie.ome.tif"
    lif2ome.write_ome(f, stack((2, 4, 8, 8)), "CTYX", pixel_size=0.5, pixel_size_y=1.5, time_step=30.5)
    xml = tifffile.TiffFile(f).ome_metadata
    assert 'TimeIncrement="30.5"' in xml and 'TimeIncrementUnit="s"' in xml
    assert 'PhysicalSizeX="0.5"' in xml and 'PhysicalSizeY="1.5"' in xml
    g = tmp_path / "stack.ome.tif"
    lif2ome.write_ome(g, stack(), "CZYX", pixel_size=0.5, time_step=30.5)
    assert "TimeIncrement" not in tifffile.TiffFile(g).ome_metadata   ## no T axis: no time


def test_folder(fake, tmp_path, capsys):
    root = tmp_path / "share"
    for name in ("b.lif", "sub/a.LIF", "sub/._a.lif", "@Recycle/old.lif", "sub/notes.txt"):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).touch()
    assert lif2ome.main([str(root), "--only", "Both"]) == 0
    out = capsys.readouterr().out
    made = sorted(f.relative_to(root).as_posix() for f in root.rglob("*.ome.tif"))
    assert made == ["b/b - Both.ome.tif", "sub/a/a - Both.ome.tif"]
    assert out.index("[2/2] sub/a.LIF   1 series") > out.index("[1/2] b.lif   1 series") and "old.lif" not in out and "._a" not in out

    assert lif2ome.main([str(root)]) == 0   ## all series: the fake file has two that are skipped
    out = capsys.readouterr().out
    assert "sub/a.LIF: 20x Images/XZ: skipped, axes L, X" in out
    assert "file done: 2 converted, 1 already there, 2 skipped in" in out and "Total: 10 of 10 series" in out

    lif2ome.main([str(root), "--only", "Both", "--out", str(tmp_path / "o")])
    assert sorted(f.relative_to(tmp_path / "o").as_posix() for f in (tmp_path / "o").rglob("*.tif")) == [
        "b - Both.ome.tif", "sub/a - Both.ome.tif"]


def test_empty_folder_is_not_a_failure(tmp_path, capsys):
    (tmp_path / "quiet" / "@Recycle").mkdir(parents=True)
    (tmp_path / "quiet" / "@Recycle" / "old.lif").touch()
    assert lif2ome.main([str(tmp_path / "quiet")]) == 0
    assert "no .lif files in this folder" in capsys.readouterr().out


def test_verbose_adds_the_details(fake):
    lif, _ = fake
    lines = []
    lif2ome.convert(lif, only="Both", report=lines.append, verbose=True)
    first, details = lines[0].split("\n")
    assert first.split()[:2] == ["ok", "Both"]
    assert details.strip().startswith("TCZYX (2, 2, 3, 8, 8) uint16, channels ['a', 'b'], 0.1000 µm/px, z step 0.5000 µm")


def test_one_file_has_no_total(fake, capsys):
    lif, _ = fake
    assert lif2ome.main([str(lif), "--only", "Both"]) == 0
    out = capsys.readouterr().out
    assert "[1/1] f.lif   1 series" in out and "file done: 1 converted in" in out and "Total:" not in out


@pytest.mark.parametrize('names, labels', [
    (['a:b', 'a_b'], ['a:b', 'a_b (2)']),
    (['A', 'A', 'A (2)'], ['A', 'A (2)', 'A (2) (2)']),
    (['Series1', 'series1'], ['Series1', 'series1 (2)']),
    (['A', 'B'], ['A', 'B']),
])
def test_plan_unique_paths(fake, monkeypatch, names, labels):
    lif, _ = fake
    monkeypatch.setattr(reader, 'series_list', lambda path: names)
    original = reader.read_info
    monkeypatch.setattr(reader, 'read_info', lambda path, s, tiles: original(path, 0, tiles))
    jobs = converter.plan(lif)
    assert [j[0] for j in jobs] == labels
    assert converter.plan(lif) == jobs
    assert converter.plan(lif, only=labels[1]) == [j for name, j in zip(names, jobs) if labels[1] in (name, j[0])]
    paths = [str(j[3]).casefold() for j in jobs if j[3]]
    assert len(paths) == len(set(paths))
    assert lif2ome.convert(lif, report=lambda line: None)["ok"] == len(names)
    assert lif2ome.convert(lif, report=lambda line: None)["there"] == len(names)


def test_expand_removes_resolved_duplicates(tmp_path):
    from lif2ome.cli import _expand
    root = tmp_path / 'share'
    root.mkdir()
    path = root / 'a.lif'
    path.touch()
    alias = tmp_path / 'alias.lif'
    alias.symlink_to(path)
    assert _expand([root, path, alias]) == [(path, 'a.lif')]


def test_main_output_collision(fake, tmp_path, capsys):
    first = tmp_path / 'x' / 'e.lif'
    second = tmp_path / 'y' / 'E.lif'
    assert lif2ome.main([str(first), str(second), '--out', str(tmp_path / 'o'), '--only', 'Both']) == 1
    text = capsys.readouterr().out
    assert f'FAILED, output already planned by {first}' in text
    assert '1 converted, 0 already there, 0 skipped, 1 failed' in text


def test_value_error_is_failure(fake, monkeypatch):
    def fail(*args):
        raise ValueError('invalid data_type')
    monkeypatch.setattr(reader, 'read_info', fail)
    lines = []
    assert lif2ome.convert(fake[0], only='Both', report=lines.append)['failed'] == 1
    assert 'FAILED' in lines[0] and 'ValueError: invalid data_type' in lines[0]
    assert issubclass(lif2ome.Unsupported, ValueError)


def test_failure_rate_and_lines(fake, monkeypatch):
    lif, _ = fake
    jobs = converter.plan(lif)[:2] + converter.plan(lif)[-1:]
    sizes = [converter.job_bytes(j) for j in jobs]
    times = iter([0, 10])
    monkeypatch.setattr(converter.time, 'time', lambda: next(times))
    def run(info, *args):
        return info.series != 0, 'written' if info.series else 'broken', ''
    monkeypatch.setattr(converter, '_convert_one', run)
    estimates = []
    monkeypatch.setattr(converter, '_time_left', lambda seconds: estimates.append(seconds) or 'about N s left')
    lines = []
    counts = lif2ome.convert(lif, jobs=jobs, report=lines.append)
    assert counts['failed'] == 1 and counts['ok'] == 2
    assert 'left' not in lines[0] and 'left' in lines[1] and 'left' not in lines[2]
    assert estimates == [sizes[2] * 10 / sizes[1]]


def test_only_across_files(fake, monkeypatch, capsys):
    lif, _ = fake
    other = lif.with_name('other.lif')
    original = reader.series_list
    monkeypatch.setattr(reader, 'series_list', lambda path: ['Else'] if path == other else original(path))
    assert lif2ome.main([str(other), str(lif), '--only', 'A (2)']) == 0
    text = capsys.readouterr().out
    assert 'other.lif: A (2): skipped, no series has that name' in text
    assert '1 converted, 0 already there, 1 skipped, 0 failed' in text
    assert 'left' not in text.split('Total:')[-1]
    assert lif2ome.main([str(other), str(lif), '--only', 'missing']) == 1
    text = capsys.readouterr().out
    assert 'Series names: Else' in text and 'Series names: A, A, 20x Images/XZ, Tiles, Both' in text
    assert '2 failed' in text


def test_summary_preserves_spaces(fake, monkeypatch, capsys):
    monkeypatch.setattr(reader, 'series_list', lambda path: ['Pos 1  merged'])
    def fail(*args):
        raise reader.Unsupported('unsupported axes')
    monkeypatch.setattr(reader, 'read_info', fail)
    assert lif2ome.main([str(fake[0])]) == 0
    assert 'f.lif: Pos 1  merged: skipped, unsupported axes' in capsys.readouterr().out


@pytest.fixture
def lif_images(monkeypatch):
    from xml.etree import ElementTree
    from types import SimpleNamespace
    images = []
    class File:
        def __init__(self, path):
            self.images = images
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.close()
        def close(self):
            pass
    monkeypatch.setattr(reader.liffile, 'LifFile', File)
    def add(dims, shape, coords=None, xml='<Element/>', path='Test'):
        data = stack(shape)
        im = SimpleNamespace(name=path.rsplit('/', 1)[-1], path=path, dims=tuple(dims), shape=shape, dtype=data.dtype,
                             coords=coords or {}, xml_element=ElementTree.fromstring(xml),
                             timestamps=None, asarray=lambda: data)
        images.append(im)
        return im, len(images) - 1
    return add


def test_reader_stack_and_part(lif_images):
    im, s = lif_images('ZCYX', (3, 2, 4, 5), {'Z': [4e-6, 2e-6, 0], 'Y': [0, 1e-6, 2e-6, 3e-6],
                                               'X': np.arange(5) * 0.5e-6, 'C': ['DAPI', 'GFP']})
    info = reader.read_info('f.lif', s)
    assert (info.axes, info.shape, info.kind) == ('ZCYX', im.shape, 'stack')
    assert (info.pixel_size, info.pixel_size_y, info.z_step, info.anisotropy) == pytest.approx((0.5, 1, 2, 4))
    data, axes = reader.read_series(info)
    assert axes == 'CZYX'
    np.testing.assert_array_equal(data, im.asarray().transpose(1, 0, 2, 3))
    np.testing.assert_array_equal(reader.read_part(info, [1], (1, 2), (1, 3, 2, 5)), data[1:2, 1:3, 1:3, 2:5])
    assert reader._lif_step({}, 'T') == 0
    assert reader._lif_step({'T': [2]}, 'T') == 0


def test_reader_cross_section(lif_images):
    im, s = lif_images('YCZX', (3, 2, 4, 5), {'Y': [0, 3e-6, 6e-6], 'Z': np.arange(4) * 2e-6,
                                               'X': np.arange(5) * 0.5e-6, 'C': ['DAPI', 'GFP']})
    info = reader.read_info('f.lif', s)
    assert info.axes == 'ZCYX' and info.shape == im.shape
    assert (info.pixel_size_y, info.z_step, info.anisotropy) == pytest.approx((2, 3, 6))
    assert info.notes == ['cross-section: Y of the file is Z of the microscope']
    data, axes = reader.read_series(info)
    assert axes == 'CZYX'
    np.testing.assert_array_equal(data, im.asarray().transpose(1, 0, 2, 3))


def test_reader_tiles(lif_images, monkeypatch):
    from types import SimpleNamespace
    im, s = lif_images('MZCYX', (3, 4, 2, 5, 6), {'C': ['DAPI', 'GFP']})
    with pytest.raises(reader.Unsupported, match='tile scan with 3 tiles'):
        reader.read_info('f.lif', s)
    info = reader.read_info('f.lif', s, tiles=True)
    assert info.axes == 'ZCYX' and info.shape == (4, 2, 5, 6) and info.tiles == 3
    calls = []
    def frames(image, *, M):
        assert image is im
        calls.append(M)
        return SimpleNamespace(asarray=lambda: im.asarray()[M])
    monkeypatch.setattr(reader.liffile, 'LifImageFrames', frames)
    data, axes = reader.read_series(info, tile=1)
    assert calls == [1] and axes == 'CZYX'
    np.testing.assert_array_equal(data, im.asarray()[1].transpose(1, 0, 2, 3))
    np.testing.assert_array_equal(reader.read_part(info, [0], tile=2), im.asarray()[2].transpose(1, 0, 2, 3)[:1])


@pytest.mark.parametrize('coords, channels, expected', [
    ({'C': ['DAPI', 'Ch1']}, '<ChannelDescription BytesInc="2" LUTName="Green"/><ChannelDescription BytesInc="0" LUTName="Blue"/>', ['DAPI', 'Green']),
    ({'C': ['Ch0', 'Ch1']}, '<ChannelDescription LUTName="Green"/><ChannelDescription LUTName="Green"/>', ['Green 1', 'Green 2']),
    ({}, '<ChannelDescription LUTName="Blue"><ChannelProperty><Key>DyeName</Key><Value>Leica/DAPI</Value></ChannelProperty></ChannelDescription>', ['DAPI']),
    ({}, '<ChannelDescription LUTName="Green"/>', ['Green']),
    ({}, '<ChannelDescription/>', ['Channel 1']),
])
def test_reader_channel_names(lif_images, coords, channels, expected):
    xml = f'<Element><Data><Image><ImageDescription><Channels>{channels}</Channels></ImageDescription></Image></Data></Element>'
    im, s = lif_images('CYX' if coords else 'YX', (2, 4, 5) if coords else (4, 5), coords, xml)
    assert reader._lif_channel_names(im) == expected
    assert reader.read_info('f.lif', s).channels == expected
    assert len(reader.read_info('f.lif', s).colors) == len(expected)


def test_reader_movie_and_axes_refusal(lif_images):
    _, s = lif_images('TYX', (3, 4, 5), {'T': [10, 12.5, 15]})
    info = reader.read_info('f.lif', s)
    assert info.kind == 'movie' and info.time_step == 2.5
    assert reader.read_settings('f.lif', s)['Time between frames (s)'] == '2.5'
    _, s = lif_images('LX', (4, 5))
    with pytest.raises(reader.Unsupported, match='axes L, X'):
        reader.read_info('f.lif', s)


def test_reader_sequential_settings(lif_images):
    xml = '''<Element><Data><Image><Attachment Name="HardwareSetting" SystemTypeName="SP8" Software="LAS X">
    <ATLConfocalSettingDefinition ObjectiveName="40x OIL" Zoom="0.75" Pinhole="0.000065"/>
    <LDM_Block_Sequential><LDM_Block_Sequential_Master><ATLConfocalSettingDefinition>
      <Detector Name="HyD1" Channel="0" IsActive="1" Gain="100" Offset="2"/>
      <MultiBand Channel="0" LeftWorld="500" RightWorld="550" DyeName="GFP"/>
    </ATLConfocalSettingDefinition></LDM_Block_Sequential_Master><LDM_Block_Sequential_List>
    <ATLConfocalSettingDefinition UserSettingName="Green">
      <LaserLineSetting LaserLine="488" IntensityDev="1"/>
      <LaserLineSetting LaserLine="561" IntensityDev="0"/>
      <Detector Name="HyD1" Channel="0" IsActive="1"/>
    </ATLConfocalSettingDefinition>
    <ATLConfocalSettingDefinition>
      <LaserLineSetting LaserLine="561" IntensityDev="2"/>
      <Detector Name="HyD1" Channel="0" IsActive="1" Gain="200"/>
      <MultiBand Channel="0" LeftWorld="570" RightWorld="620" DyeName="RFP"/>
    </ATLConfocalSettingDefinition>
    </LDM_Block_Sequential_List></LDM_Block_Sequential></Attachment></Image></Data></Element>'''
    im, s = lif_images('YX', (4, 5), xml=xml)
    im.timestamps = [np.datetime64('2026-11-09T10:20:30')]
    settings = reader.read_settings('f.lif', s)
    assert settings == {'Source file': 'f.lif', 'Series': 'Test', 'Acquired': '2026-11-09 10:20:30',
                        'Microscope': 'SP8', 'Software': 'LAS X', 'Objective': '40x OIL', 'Zoom': '0.75',
                        'Pinhole (µm)': '65', 'Green: Laser 488 nm (%)': '1',
                        'Green: Detector HyD1': 'gain 100, offset 2, 500 to 550 nm, GFP',
                        'Seq. 2: Laser 561 nm (%)': '2',
                        'Seq. 2: Detector HyD1': 'gain 200, offset 2, 570 to 620 nm, RFP'}


def test_single_channel_liffile_names(lif_images):
    im, s = lif_images('YX', (4, 5), xml='<Element><Data><Image><ImageDescription><Channels>'
                       '<ChannelDescription LUTName="Green"/></Channels></ImageDescription></Image></Data></Element>')
    im._channel_names = ('GFP',)
    assert reader.read_info('f.lif', s).channels == ['GFP']
    im._channel_names = ('Ch0',)
    assert reader.read_info('f.lif', s).channels == ['Green']


def test_main_total_after_failure(fake, monkeypatch, capsys):
    lif, _ = fake
    paths = [lif, lif.with_name('second.lif'), lif.with_name('third.lif')]
    def run(info, *args):
        return info.path != str(lif), 'finished' if info.path != str(lif) else 'broken', ''
    monkeypatch.setattr(converter, '_convert_one', run)
    assert lif2ome.main([*(str(p) for p in paths), '--only', 'Both']) == 1
    text = capsys.readouterr().out
    totals = [line for line in text.splitlines() if 'Total:' in line]
    assert len(totals) == 3
    assert 'left' not in totals[0] and 'left' in totals[1] and 'left' not in totals[2]
    assert 'left' not in text.split('Not converted:')[1]


def test_plan_tile_label_collision(fake, monkeypatch):
    lif, _ = fake
    original = reader.read_info
    monkeypatch.setattr(reader, 'series_list', lambda path: ['Tiles', 'Tiles - tile 01'])
    monkeypatch.setattr(reader, 'read_info', lambda path, s, tiles: original(path, 3 if s == 0 else 0, tiles))
    jobs = converter.plan(lif, tiles=True)
    assert [j[0] for j in jobs] == ['Tiles - tile 01', 'Tiles - tile 02', 'Tiles - tile 03', 'Tiles - tile 01 (2)']
    assert converter.plan(lif, only='Tiles - tile 01 (2)', tiles=True) == [jobs[-1]]


def scene_and_tiles(jitter=True):
    """ A picture with texture, cut into 3 x 3 tiles of 64 px with an overlap of 16 px. The true place of a tile is
    off from the place "the stage gives" by a few pixels, in the other direction on each second row """
    rng = np.random.default_rng(0)
    scene = (rng.random((220, 220)) * 255).astype(np.uint8)
    nominal, true = [], []
    for row in range(3):
        for col in range(3):
            nominal.append((col * 48, row * 48))
            dx = (5 if row % 2 else -4) if jitter else 0
            true.append((col * 48 + 8 + dx, row * 48 + 8 + (row if jitter else 0)))
    tiles = [scene[y:y + 64, x:x + 64] for x, y in true]
    return scene, nominal, true, tiles


def test_grid_reads_the_overlap_and_the_odd_tile():
    _, nominal, _, _ = scene_and_tiles()
    found = stitch.grid(nominal + [(70, 75)], (64, 64))
    assert (found["columns"], found["rows"], found["off_grid"]) == (3, 3, [9])
    assert found["overlap_x"] == pytest.approx(0.25) and found["overlap_y"] == pytest.approx(0.25)
    assert stitch.grid([(0, 0), (90, 0)], (100, 100))["overlap_x"] == pytest.approx(0.10)


def test_merge_moves_each_tile_to_fit_its_overlaps():
    scene, nominal, true, tiles = scene_and_tiles()
    data, found = stitch.merge(nominal, lambda k: tiles[k][np.newaxis])   ## one channel
    assert found["pairs"] == 20 and found["matched"] >= 12 and found["moved"] > 3   ## 12 pairs side by side, 8 at a corner
    x0, y0 = min(x for x, _ in true), min(y for _, y in true)
    for (x, y), tile in zip(true, tiles):   ## each tile lies where the scene has it
        assert np.array_equal(data[0, y - y0:y - y0 + 64, x - x0:x - x0 + 64], tile)

    ## with the stage positions only, the tiles would lie in the wrong place
    fitted, _, _ = stitch.place(nominal, [t.astype(np.float32) for t in tiles])
    assert not np.array_equal(fitted, np.array(nominal))


def test_merge_keeps_the_stage_positions_when_nothing_matches():
    rng = np.random.default_rng(1)
    tiles = [(rng.random((64, 64)) * 255).astype(np.uint8) for _ in range(4)]   ## four pictures with nothing in common
    nominal = [(0, 0), (48, 0), (0, 48), (48, 48)]
    data, found = stitch.merge(nominal, lambda k: tiles[k])
    assert found["matched"] == 0 and found["pairs"] == 6 and data.shape == (112, 112)
    assert np.array_equal(data[48:, 48:], tiles[3])


def test_convert_merges_a_tile_scan(fake, monkeypatch, capsys):
    lif, reads = fake
    scene, nominal, true, tiles = scene_and_tiles()
    info = lif2ome.ImageInfo(str(lif), "CYX", (1, 64, 64), "image", ["a"], series_name="Scan", dtype="uint8",
                             pixel_size=0.5, tiles=10, colors=[(0, 0, 255)])
    monkeypatch.setattr(reader, "series_list", lambda path: ["Scan"])
    monkeypatch.setattr(reader, "read_info", lambda path, s, tiles=False: info)
    monkeypatch.setattr(reader, "read_series", lambda info, tile=None: (tiles[min(tile, 8)][np.newaxis], "CYX"))
    monkeypatch.setattr(reader, "read_tile_positions", lambda path, s: nominal + [(70, 75)])
    lines = []
    assert lif2ome.convert(lif, report=lines.append, verbose=True) == {"ok": 1, "there": 0, "skipped": 0, "failed": 0}
    assert lines[0].split()[:2] == ["ok", "Scan"] and "merged 3 x 3 tiles, overlap 25% by 25%" in lines[0]
    assert "of 20 overlaps matched" in lines[0] and "CHECK" not in lines[0]
    assert "1 tile off the grid left out" in lines[0]   ## the tile that is in no row and no column
    assert "1 tile off the grid left out" in lines[0].split("\n")[0]   ## on the first line, with no --verbose
    assert [f.name for f in (lif.parent / "f").iterdir()] == ["f - Scan.ome.tif"]
    assert lif2ome.convert(lif, tiles=True, report=lambda line: None)["ok"] == 10   ## --tiles writes every tile
    merged = tifffile.imread(lif.parent / "f" / "f - Scan.ome.tif")
    x0, y0 = min(x for x, _ in true), min(y for _, y in true)
    assert np.array_equal(np.squeeze(merged)[true[4][1] - y0:true[4][1] - y0 + 64, true[4][0] - x0:true[4][0] - x0 + 64], tiles[4])
    assert lif2ome.read_metadata(lif.parent / "f" / "f - Scan.ome.tif")["colors"] == [(0, 0, 255)]
    assert "merged by lif2ome" in lif2ome.read_metadata(lif.parent / "f" / "f - Scan.ome.tif")["settings"]["Tile scan"]


def test_a_tile_scan_with_a_merged_copy_converts_both(fake, monkeypatch):
    lif, _ = fake
    _, nominal, _, tiles = scene_and_tiles()
    plain = stack((1, 8, 8))
    info = lif2ome.ImageInfo(str(lif), "CYX", (1, 64, 64), "image", ["a"], dtype="uint8", tiles=9)
    whole = lif2ome.ImageInfo(str(lif), "CYX", (1, 8, 8), "image", ["a"], dtype="uint16")
    monkeypatch.setattr(reader, "series_list", lambda path: ["Scan", "Scan_Merged"])
    monkeypatch.setattr(reader, "read_info", lambda path, s, tiles=False: info if s == 0 else whole)
    monkeypatch.setattr(reader, "read_tile_positions", lambda path, s: nominal)
    monkeypatch.setattr(reader, "read_series", lambda info, tile=None:
                        (plain if tile is None else tiles[tile][np.newaxis], "CYX"))
    lines = []
    assert lif2ome.convert(lif, report=lines.append) == {"ok": 2, "there": 0, "skipped": 0, "failed": 0}
    assert lines[0].split()[:2] == ["ok", "Scan"] and "merged 3 x 3 tiles" in lines[0]
    assert lines[1].split()[:2] == ["ok", "Scan_Merged"] and "merged" not in lines[1]
    scan = lif.parent / "f" / "f - Scan.ome.tif"
    las_x = lif.parent / "f" / "f - Scan_Merged.ome.tif"
    assert {f.name for f in scan.parent.iterdir()} == {scan.name, las_x.name}
    assert "merged by lif2ome" in lif2ome.read_metadata(scan)["settings"]["Tile scan"]
    assert "Tile scan" not in lif2ome.read_metadata(las_x)["settings"]
    assert np.array_equal(tifffile.imread(las_x), plain.squeeze())


@pytest.mark.parametrize("luts, expected", [
    (["bLuE", "GREEN", "unknown"], [(0, 0, 255), (0, 255, 0), None]),
    (["gRaY"], [(255, 255, 255)]),
    (["Red", "Cyan", "Magenta", "Yellow"], [(255, 0, 0), (0, 255, 255), (255, 0, 255), (255, 255, 0)]),
])
def test_reader_channel_colors(lif_images, luts, expected):
    channels = ''.join(f'<ChannelDescription BytesInc="{i}" LUTName="{lut}"/>'
                       for i, lut in reversed(list(enumerate(luts))))
    xml = f'<Element><Data><Image><ImageDescription><Channels>{channels}</Channels></ImageDescription></Image></Data></Element>'
    _, s = lif_images('CYX' if len(luts) > 1 else 'YX',
                      (len(luts), 4, 5) if len(luts) > 1 else (4, 5), xml=xml)
    info = reader.read_info('f.lif', s)
    assert info.channels == luts
    assert info.colors == expected


def test_channel_colors_round_trip_and_inspect(tmp_path, capsys):
    path = tmp_path / 'colors.ome.tif'
    names = ['Red', 'GFP', 'DAPI', 'White', 'Custom', 'Unknown']
    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255), (12, 34, 56), None]
    lif2ome.write_ome(path, stack((6, 4, 5)), 'CYX', names, channel_colors=colors)
    with tifffile.TiffFile(path) as tif:
        channels = ElementTree.fromstring(tif.ome_metadata).findall('.//{*}Channel')
    assert [c.get('Color') for c in channels] == ['-16776961', '16711935', '65535', '-1', '203569407', None]
    assert lif2ome.read_metadata(path)['colors'] == colors
    assert lif2ome.main(['--inspect', str(path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert '  Channels' in lines
    assert [line.split() for line in lines[2:8]] == [
        ['Red', 'red'], ['GFP', 'green'], ['DAPI', 'blue'], ['White', 'gray'],
        ['Custom', '12,', '34,', '56'], ['Unknown']]


@pytest.mark.parametrize('colors', [None, [], [None, None]])
def test_no_channel_colors(tmp_path, colors):
    path = tmp_path / 'plain.ome.tif'
    lif2ome.write_ome(path, stack(), 'CZYX', ['a', 'b'], channel_colors=colors)
    with tifffile.TiffFile(path) as tif:
        assert 'Color=' not in tif.ome_metadata
    assert lif2ome.read_metadata(path)['colors'] == [None, None]


def test_series_paths(lif_images, monkeypatch):
    lif_images('YX', (4, 5), path='20x Images/Position 3')
    lif_images('YX', (4, 5), path='Position 4')
    assert lif2ome.series_list('f.lif') == ['Position 3', 'Position 4']
    assert lif2ome.series_paths('f.lif') == ['20x Images/Position 3', 'Position 4']
    assert reader.read_settings('f.lif')['Series'] == '20x Images/Position 3'
    from types import SimpleNamespace
    monkeypatch.setattr(reader, '_bioio_image', lambda path: SimpleNamespace(scenes=['Scene 1', 'Scene 2']))
    assert reader.series_paths('f.czi') == reader.series_list('f.czi')


@pytest.mark.parametrize('only', ['Position 3', '20x Images/Position 3', '20x Images_Position 3'])
def test_folder_series_conversion(lif_images, tmp_path, capsys, only):
    lif_images('YX', (4, 5), path='20x Images/Position 3')
    lif = tmp_path / 'f.lif'
    assert lif2ome.main([str(lif), '--only', only]) == 0
    assert (tmp_path / 'f' / 'f - 20x Images_Position 3.ome.tif').exists()
    assert '20x Images/Position 3' in capsys.readouterr().out


def test_same_series_name_in_two_folders(lif_images, tmp_path):
    for folder in ['20x Images', '40x Images']:
        lif_images('YX', (4, 5), path=f'{folder}/Position 1')
    lif = tmp_path / 'f.lif'
    jobs = converter.plan(lif, only='Position 1')
    assert [j[0] for j in jobs] == ['20x Images/Position 1', '40x Images/Position 1']
    assert [j[3].name for j in jobs] == ['f - 20x Images_Position 1.ome.tif', 'f - 40x Images_Position 1.ome.tif']
    assert converter.plan(lif, only='40x Images/Position 1') == [jobs[1]]
    assert '20x Images/Position 1, 40x Images/Position 1' in converter.plan(lif, only='missing')[0][4]


@pytest.mark.parametrize('only, series, shape, axes', [
    ('A', 0, (3, 2, 8, 8), 'ZCYX'),
    ('Both', 4, (2, 3, 2, 8, 8), 'TZCYX'),
])
def test_projections_values_and_resume(fake, only, series, shape, axes):
    lif, reads = fake
    lines, results = [], []
    kinds = ('max', 'sum', 'avg')
    counts = lif2ome.convert(lif, only=only, projections=kinds, report=lines.append,
                             result=lambda *args: results.append(args))
    assert counts == {'ok': 2 if only == 'A' else 1, 'there': 0, 'skipped': 0, 'failed': 0}
    source = lif2ome.out_path(lif, only)
    folder = source.parent / 'projections'
    assert sorted(p.name for p in folder.iterdir()) == sorted(f'{k.upper()}_{source.name}' for k in kinds)
    data = stack(shape) + series
    expected = [data.max(axis=axes.index('Z')), data.sum(axis=axes.index('Z'), dtype=np.float32),
                data.mean(axis=axes.index('Z'), dtype=np.float32)]
    for kind, want in zip(kinds, expected):
        with tifffile.TiffFile(folder / f'{kind.upper()}_{source.name}') as tif:
            assert tif.series[0].axes == axes.replace('Z', '')
            assert tif.series[0].dtype == (np.uint16 if kind == 'max' else np.float32)
            np.testing.assert_array_equal(tif.asarray(), want)
    assert lines[1].split()[:2] == ['proj', only]
    assert 'MAX, SUM, AVG written' in lines[1]
    assert results[1] == ('proj', only, 'MAX, SUM, AVG written')
    before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in source.parent.rglob('*.ome.tif')}
    reads.clear()
    lines.clear()
    counts = lif2ome.convert(lif, only=only, projections=kinds, report=lines.append)
    assert counts['there'] == (2 if only == 'A' else 1) and counts['failed'] == 0
    assert reads == []
    assert 'MAX, SUM, AVG there' in lines[1]
    assert before == {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in before}


def test_existing_file_gets_projections_and_metadata(fake, monkeypatch):
    lif, reads = fake
    jobs = converter.plan(lif, only='Both')
    jobs[0][1].pixel_size_y = 0.3
    jobs[0][1].time_step = 2.5
    lif2ome.convert(lif, jobs=jobs, metadata={'sample': 'old'}, report=lambda line: None)
    source = jobs[0][3]
    original = lif2ome.read_metadata(source)
    reads.clear()
    def no_stack(*args, **kwargs):
        raise AssertionError('a whole TIFF stack was read')
    monkeypatch.setattr(tifffile.TiffPageSeries, 'asarray', no_stack)
    lines = []
    assert lif2ome.convert(lif, jobs=jobs, projections=('max', 'sum', 'mean'), metadata={'sample': 'new'},
                           report=lines.append) == {'ok': 0, 'there': 1, 'skipped': 0, 'failed': 0}
    assert reads == [] and lines[0].split()[0] == 'there'
    for kind, word in [('MAX', 'maximum'), ('SUM', 'sum'), ('AVG', 'average')]:
        path = source.parent / 'projections' / f'{kind}_{source.name}'
        meta = lif2ome.read_metadata(path)
        for key in ('channels', 'colors', 'fields', 'leica_xml', 'pixel_size', 'pixel_size_y', 'time_step'):
            assert meta[key] == original[key]
        assert meta['settings'] == {**original['settings'], 'Projection': f'{word} of 3 planes along Z'}
        assert meta['z_step'] == 0
        with tifffile.TiffFile(path) as tif:
            assert 'PhysicalSizeZ' not in tif.ome_metadata
    (source.parent / 'projections' / f'MAX_{source.name}').unlink()
    lines.clear()
    lif2ome.convert(lif, jobs=jobs, projections=('max', 'avg'), report=lines.append)
    assert 'MAX written; AVG there' in lines[1]


@pytest.mark.parametrize('axes, shape', [('CYX', (2, 4, 5)), ('CZYX', (2, 1, 4, 5)),
                                         ('TCZYX', (3, 2, 1, 4, 5))])
def test_no_projection_without_z_stack(lif_images, tmp_path, axes, shape):
    lif_images(axes, shape, {'C': ['a', 'b']})
    lines = []
    assert lif2ome.convert(tmp_path / 'f.lif', projections=('max', 'sum', 'avg'), report=lines.append)['ok'] == 1
    assert len(lines) == 1
    assert not (tmp_path / 'f' / 'projections').exists()


@pytest.mark.parametrize('failure', ['check', 'write', 'read'])
def test_projection_failure_continues(fake, monkeypatch, failure):
    lif, _ = fake
    check, write = converter._same_pixels, converter.write_ome
    def checked(path, data, axes):
        if path.name.startswith('MAX_') and 'Both' not in path.name:
            assert path.name.endswith('.part') and path.exists()
            assert not path.with_suffix('').exists()
            if failure == 'read':
                raise OSError('cannot read projection')
            if failure == 'check':
                return False
        return check(path, data, axes)
    def written(path, *args, **kwargs):
        if failure == 'write' and path.name.startswith('MAX_') and 'Both' not in path.name:
            raise OSError('cannot write projection')
        return write(path, *args, **kwargs)
    monkeypatch.setattr(converter, '_same_pixels', checked)
    monkeypatch.setattr(converter, 'write_ome', written)
    lines, results = [], []
    counts = lif2ome.convert(lif, projections=('max', 'sum'), report=lines.append,
                             result=lambda *args: results.append(args))
    assert counts == {'ok': 3, 'there': 0, 'skipped': 2, 'failed': 1}
    folder = lif.with_suffix('') / 'projections'
    assert not (folder / 'MAX_f - A.ome.tif').exists()
    assert (folder / 'SUM_f - A.ome.tif').exists()
    assert (folder / 'MAX_f - Both.ome.tif').exists()
    assert not list(folder.glob('*.part'))
    assert 'MAX FAILED:' in lines[1] and 'SUM written' in lines[1]
    assert any(word == 'FAILED' and 'proj MAX FAILED' in reason for word, _, reason in results)
    assert len([line for line in lines if line.split()[0] == 'proj']) == 2


@pytest.mark.parametrize('value', ['median', '', 'max,', 'max,other'])
def test_projection_argparse_error(fake, capsys, value):
    with pytest.raises(SystemExit) as error:
        lif2ome.main([str(fake[0]), '--projections', value])
    assert error.value.code == 2
    assert 'max, sum, avg' in capsys.readouterr().err
    with pytest.raises(ValueError, match='max, sum, avg'):
        lif2ome.convert(fake[0], projections=(value,))


def test_projection_cli_and_inspect_heading(fake, capsys):
    lif, _ = fake
    assert lif2ome.main([str(lif), '--only', 'Both', '--projections', 'max,sum,mean']) == 0
    assert 'MAX, SUM, AVG written' in capsys.readouterr().out
    assert lif2ome.main([str(lif), '--inspect']) == 0
    assert '  Series 20x Images/XZ\n' in capsys.readouterr().out


def test_projection_of_merged_stack(fake, monkeypatch):
    lif, _ = fake
    jobs = converter.plan(lif, only='Tiles', tiles=True)
    label, info, _, out, _ = jobs[0]
    jobs = [(label, info, {'tiles': [0, 1], 'positions': [(0, 0), (8, 0)]}, out, None)]
    def fixed_place(positions, pictures):
        return np.array(positions, dtype=int), 0, 0
    monkeypatch.setattr(stitch, 'place', fixed_place)
    assert lif2ome.convert(lif, jobs=jobs, projections=('max', 'sum', 'avg'), report=lambda line: None)['ok'] == 1
    data = tifffile.imread(out)
    assert data.shape == (2, 3, 8, 16)
    for kind, reduce in [('max', np.max), ('sum', np.sum), ('avg', np.mean)]:
        projected = tifffile.imread(out.parent / 'projections' / f'{kind.upper()}_{out.name}')
        np.testing.assert_array_equal(projected, reduce(data, axis=1))


def test_failed_series_gets_no_projection(fake, monkeypatch):
    lif, _ = fake
    monkeypatch.setattr(converter, '_convert_one', lambda *args: (False, 'broken', ''))
    def unexpected(*args):
        pytest.fail('a failed series must not get a projection')
    monkeypatch.setattr(converter, '_project_one', unexpected)
    lines = []
    assert lif2ome.convert(lif, only='Both', projections=('max',), report=lines.append)['failed'] == 1
    assert len(lines) == 1 and 'FAILED' in lines[0]


@pytest.mark.parametrize('axes, shape', [('ZYX', (3, 4, 5)), ('TZCYX', (2, 3, 2, 4, 5))])
def test_projection_uses_saved_axes_and_signed_values(fake, axes, shape):
    lif, reads = fake
    jobs = converter.plan(lif, only='Both')
    source = jobs[0][3]
    source.parent.mkdir()
    data = -stack(shape, dtype=np.int16) - 1
    lif2ome.write_ome(source, data, axes)
    assert lif2ome.convert(lif, jobs=jobs, projections=('max', 'sum', 'avg'), report=lambda line: None)['there'] == 1
    assert reads == []
    for kind, reduce in [('max', np.max), ('sum', np.sum), ('avg', np.mean)]:
        path = source.parent / 'projections' / f'{kind.upper()}_{source.name}'
        actual = tifffile.imread(path)
        assert actual.dtype == (np.int16 if kind == 'max' else np.float32)
        np.testing.assert_array_equal(actual, reduce(data, axis=axes.index('Z')))
