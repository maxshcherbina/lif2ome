"""No test needs a microscope file: convert() runs on a made-up file through a patched reader.
A real file: LIF2OME_TEST_LIF=/path/to/file.lif uv run pytest"""
import os
from pathlib import Path

import numpy as np
import pytest
import tifffile

import lif2ome
from lif2ome import converter, reader


def stack(shape=(2, 3, 8, 8), dtype=np.uint16):
    return np.arange(np.prod(shape), dtype=dtype).reshape(shape)


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
            raise ValueError("axes L, X: only T, Z, C, Y and X are read")
        if name == "Tiles" and not tiles:
            raise ValueError("tile scan with 3 tiles, skipped; convert the merged series")
        return lif2ome.ImageInfo(str(path), axes, shape, lif2ome.guess_kind(axes, shape), ["a", "b"], series=s,
                                 series_name=name, dtype="uint16", pixel_size=0.1, z_step=0.5,
                                 tiles=3 if name == "Tiles" else 0)

    def read_series(info, tile=None):
        reads.append((info.series, tile))
        data = stack(info.shape) + info.series + (tile or 0) * 7
        axes = "".join(ax for ax in "TCZYX" if ax in info.axes)
        return np.transpose(data, [info.axes.index(ax) for ax in axes]), axes

    monkeypatch.setattr(reader, "series_list", lambda path: [v[0] for v in series.values()])
    monkeypatch.setattr(reader, "read_info", read_info)
    monkeypatch.setattr(reader, "read_series", read_series)
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
    assert lines[2].split()[:2] == ["skipped", "XZ"] and "axes L, X" in lines[2]
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
    assert "A, A, XZ, Tiles, Both" in lines[0]
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
    assert meta["fields"] == {"genotype": "Ecad <Lariat>", "trial": "1"}
    assert meta["settings"] == {"Series": "Both", "Zoom": "0.75"}
    assert meta["leica_xml"] == '<Element Name="Both"><A b="1 &lt; 2">é\n</A></Element>'

    assert lif2ome.main(["--inspect", str(f)]) == 0
    out = capsys.readouterr().out
    assert "genotype  Ecad <Lariat>" in out and "Zoom    0.75" in out and "Leica XML: 53 characters" in out


def test_file_without_metadata(tmp_path):
    f = tmp_path / "x.ome.tif"
    lif2ome.write_ome(f, stack(), "CZYX")
    assert lif2ome.read_metadata(f) == {"fields": {}, "settings": {}, "leica_xml": ""}


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
    assert "sub/a.LIF: XZ: skipped, axes L, X" in out
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
