import datetime as dt
import gzip
import json

import numpy as np
import pytest

import gsmap

DAY = dt.date(2026, 11, 9)


def global_file(fill=0.0, cells=None):
    """A synthetic hourly file (3600 x 1200 float32 LE) with a few marked cells {(lat, lon): value}."""
    a = np.full((gsmap.G_NLAT, gsmap.G_NLON), fill, dtype="<f4")
    for (lat, lon), v in (cells or {}).items():
        a[int((60 - lat) / 0.1), int(lon / 0.1)] = v
    return a.tobytes()


def test_crop_shape_and_origin():
    assert (gsmap.NY, gsmap.NX) == (620, 720)
    assert gsmap.LAT_C0 == pytest.approx(49.95) and gsmap.LON_C0 == pytest.approx(84.05)


def test_parse_places_cells_north_up():
    raw = global_file(cells={(49.97, 84.03): 7.0, (-11.97, 155.97): 3.0, (1.32, 103.87): 12.5})   # NW corner, SE corner, Singapore
    a = gsmap.parse_hourly(raw)
    assert a.shape == (gsmap.NY, gsmap.NX)
    assert a[0, 0] == 7.0 and a[-1, -1] == 3.0
    j, i = round((gsmap.LAT_C0 - 1.35) / 0.1), round((103.85 - gsmap.LON_C0) / 0.1)
    assert a[j, i] == 12.5


def test_parse_missing_becomes_nan():
    a = gsmap.parse_hourly(global_file(fill=-999.9, cells={(10.05, 100.05): 1.0}))
    assert np.isnan(a).sum() == a.size - 1


@pytest.mark.parametrize("raw", [b"x" * 10, np.zeros((1200, 3600), ">f4").tobytes()[:-4]])
def test_parse_rejects_wrong_size(raw):
    with pytest.raises(ValueError):
        gsmap.parse_hourly(raw)


def test_parse_rejects_big_endian():
    be = np.full((gsmap.G_NLAT, gsmap.G_NLON), 5.0, dtype=">f4").tobytes()
    with pytest.raises(ValueError):
        gsmap.parse_hourly(be)


class FakeClient:
    """Stands in for gsmap.Ftp: 24 hourly files of a constant rate, optionally with some hours missing."""

    def __init__(self, rate=1.0, missing=()):
        self.rate, self.missing, self.downloads = rate, set(missing), []

    def day_files(self, day):
        return {h: f"/x/{day:%Y%m%d}.{h:02d}00.dat.gz" for h in range(24) if h not in self.missing}

    def download(self, path):
        self.downloads.append(path)
        return global_file(fill=self.rate)

    def close(self):
        pass


def test_daily_mean_times_24(tmp_path):
    s, c, hours, _, _ = gsmap.fetch_day(FakeClient(rate=0.5), DAY, tmp_path)
    assert hours.all() and c.max() == 24
    assert gsmap.daily_mm(s, c, 18)[10, 10] == pytest.approx(12.0)        # 0.5 mm/h x 24 h


def test_cache_is_reused_and_missing_hours_are_filled_later(tmp_path):
    first = FakeClient(missing=range(20, 24))
    s, c, hours, _, _ = gsmap.fetch_day(first, DAY, tmp_path)
    assert hours.sum() == 20 and len(first.downloads) == 20
    second = FakeClient()                                                # the last hours arrive
    s, c, hours, _, _ = gsmap.fetch_day(second, DAY, tmp_path)
    assert hours.all() and len(second.downloads) == 4                    # only the new ones are downloaded
    third = FakeClient()
    gsmap.fetch_day(third, DAY, tmp_path)
    assert third.downloads == []                                         # complete day: nothing to do


def test_cells_with_too_few_valid_hours_are_masked():
    s = np.full((gsmap.NY, gsmap.NX), 10.0, np.float32)
    c = np.full((gsmap.NY, gsmap.NX), 24, np.uint8)
    c[0, 0] = 5
    mm = gsmap.daily_mm(s, c, 18)
    assert np.isnan(mm[0, 0]) and mm[1, 1] == pytest.approx(10.0 / 24 * 24)


def test_encode_roundtrip_and_missing():
    mm = np.array([[0.0, 12.34, np.nan, 1e6]], np.float32)
    u = gsmap.encode(mm)
    assert list(u[0]) == [0, 123, gsmap.MISSING_U16, gsmap.MISSING_U16 - 1]


def test_domain_mean_ignores_missing():
    mm = np.full((gsmap.NY, gsmap.NX), 10.0, np.float32)
    mm[:, : gsmap.NX // 2] = np.nan
    assert gsmap.domain_mean(mm) == pytest.approx(10.0)
    assert gsmap.domain_mean(np.full_like(mm, np.nan)) is None


def test_remote_file_names_are_matched():
    class Listing(gsmap.Ftp):
        def list_dir(self, path):
            return ["gsmap_nrt.20261109.0000.dat.gz", "gsmap_nrt.20261109.0100.dat.gz", "gsmap_nrt.20261109.0100.dat.gz.md5",
                    "gsmap_nrt.20261108.0200.dat.gz", "gsmap_nrt.20261109.0300.v8.dat", "README.txt"] if path == "/real/2026/11/09" else []

    got = Listing("h", "u", "p", ["/nothing", "/real"]).day_files(DAY)
    assert sorted(got) == [0, 1, 3]
    assert got[0] == "/real/2026/11/09/gsmap_nrt.20261109.0000.dat.gz"


def test_scale_is_consistent():
    assert len(gsmap.COLORS) == len(gsmap.BINS) and gsmap.BINS == sorted(gsmap.BINS)


def test_window_is_consecutive_and_ends_on_end():
    w = gsmap.window(DAY, 7)
    assert len(w) == 7 and w[-1] == DAY and w[0] == dt.date(2026, 11, 3)


def test_demo_site_is_written(tmp_path):
    out = tmp_path / "site"
    assert gsmap.main(["--demo", "--end", "2026-11-09", "--out", str(out), "--assets", str(tmp_path)]) == 0
    m = json.loads((out / "data" / "manifest.json").read_text())
    assert [d["date"] for d in m["days"]][0] == "2026-11-03" and len(m["days"]) == 7 and m["demo"] is True
    assert [p["id"] for p in m["places"]] == ["sg"] and len(m["hours"]) == 7 * 24       # no regions.geojson in tmp_path
    sg = m["series"]["sg"]
    assert len(sg["hourly"]["mean"]) == 168 and len(sg["daily"]["mean"]) == 7
    raw = gzip.decompress((out / "data" / "rain.bin.gz").read_bytes())
    assert len(raw) == 7 * gsmap.NY * gsmap.NX * 2
    assert (out / "index.html").exists() and (out / "viewer.js").exists()


def test_missing_credentials_exit_with_a_hint(monkeypatch, tmp_path):
    monkeypatch.delenv("GSMAP_USER", raising=False)
    monkeypatch.delenv("GSMAP_PASSWORD", raising=False)
    with pytest.raises(SystemExit) as e:
        gsmap.main(["--out", str(tmp_path / "s")])
    assert "GSMAP_USER" in str(e.value)


def test_rejected_login_explains_itself(monkeypatch, tmp_path):
    import ftplib
    monkeypatch.setenv("GSMAP_USER", " user \n")
    monkeypatch.setenv("GSMAP_PASSWORD", "pw")

    def refuse(self, *a, **k):
        assert (self.user, self.password) == ("user", "pw")        # whitespace was stripped
        raise ftplib.error_perm("530 Login incorrect.")

    monkeypatch.setattr(gsmap.Ftp, "day_files", refuse)
    with pytest.raises(SystemExit) as e:
        gsmap.main(["--out", str(tmp_path / "s"), "--cache-dir", str(tmp_path / "c"), "--workers", "1"])
    assert "rejected the login" in str(e.value)


def test_hourly_place_series_are_cached_and_follow_the_places(tmp_path):
    import places
    sg = places.load_places(gsmap.GRID)                                  # the Singapore box only
    fake = FakeClient(rate=2.0)
    _, _, hours, tsm, tsx = gsmap.fetch_day(fake, DAY, tmp_path, sg)
    assert hours.all() and np.allclose(tsm[:, 0], 2.0) and np.allclose(tsx[:, 0], 2.0)
    again = FakeClient()
    _, _, _, tsm2, _ = gsmap.fetch_day(again, DAY, tmp_path, sg)
    assert again.downloads == [] and np.allclose(tsm2[:, 0], 2.0)        # served from the cache
    other = FakeClient(rate=3.0)                                         # another set of places: the cache cannot be used
    _, _, _, tsm3, _ = gsmap.fetch_day(other, DAY, tmp_path, [])
    assert len(other.downloads) == 24 and tsm3.shape == (24, 0)


def test_whole_pipeline_with_a_fake_server(monkeypatch, tmp_path):
    """main(): 1 mm/h everywhere for 7 days -> 24 mm/day, hourly series of 1 mm/h, one timestamp per hour."""
    monkeypatch.setenv("GSMAP_USER", "u")
    monkeypatch.setenv("GSMAP_PASSWORD", "p")
    monkeypatch.setattr(gsmap, "Ftp", lambda *a, **k: FakeClient(rate=1.0))
    out = tmp_path / "site"
    assert gsmap.main(["--end", "2026-11-09", "--out", str(out), "--cache-dir", str(tmp_path / "c")]) == 0
    m = json.loads((out / "data" / "manifest.json").read_text())
    assert [d["hours"] for d in m["days"]] == [24] * 7 and all(d["mean"] == pytest.approx(24.0) for d in m["days"])
    sg = m["series"]["sg"]
    assert sg["daily"]["mean"] == [24.0] * 7 and sg["daily"]["max"] == [24.0] * 7
    assert sg["hourly"]["mean"] == [1.0] * 168
    assert m["hours"][0] == 1793664000 and m["hours"][1] - m["hours"][0] == 3600 and m["hours"][24] - m["hours"][0] == 86400   # 2026-11-03 00:00 UTC
    raw = np.frombuffer(gzip.decompress((out / "data" / "rain.bin.gz").read_bytes()), "<u2")
    assert raw.size == 7 * gsmap.NY * gsmap.NX and set(np.unique(raw)) == {240}                                   # 24.0 mm/day in tenths


def test_a_day_with_few_files_is_left_out_but_the_rest_is_kept(monkeypatch, tmp_path):
    monkeypatch.setenv("GSMAP_USER", "u")
    monkeypatch.setenv("GSMAP_PASSWORD", "p")
    monkeypatch.setattr(gsmap, "Ftp", lambda *a, **k: FakeClient(missing=range(10, 24)))      # only 10 hours of every day
    with pytest.raises(SystemExit) as e:
        gsmap.main(["--end", "2026-11-09", "--out", str(tmp_path / "s"), "--cache-dir", str(tmp_path / "c")])
    assert "no usable days" in str(e.value)
