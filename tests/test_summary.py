"""summary.json and the files of the `data` branch, built from a fake JAXA server with known rain."""
import datetime as dt
import ftplib
import gzip
import json
import re

import numpy as np
import pytest

import gsmap

TODAY = dt.date(2026, 11, 10)
BASE = 1.0                                  # mm/h outside the squares, before the hourly factor
# 1 x 1 degree squares (exactly 100 cells each) with their own rain rate; ids as in model_forecast's regions.geojson
SQUARES = {"jambi": ((101, -2, 102, -1), 3.0), "ssum": ((103, -4, 104, -3), 5.0),
           "wkal": ((110, 0, 111, 1), 2.0), "ckal": ((112, -2, 113, -1), 4.0), "skal": ((114, -4, 115, -3), 6.0),
           "khm": ((104, 11, 105, 12), 1.0), "lao": ((102, 17, 103, 18), 2.0), "mmr": ((96, 20, 97, 21), 3.0),
           "tha": ((100, 15, 101, 16), 4.0), "vnm": ((106, 21, 107, 22), 5.0)}
SECRET_USER, SECRET_PW = "jaxa-user-SENTINEL-1f3a", "jaxa-pass-SENTINEL-9c7d"


def field(scale):
    a = np.full((gsmap.G_NLAT, gsmap.G_NLON), BASE * scale, "<f4")
    for (w, s, e, n), v in SQUARES.values():
        a[round((60 - n) / 0.1):round((60 - s) / 0.1), round(w / 0.1):round(e / 0.1)] = v * scale
    return a.tobytes()


class Server:
    """Stands in for gsmap.Ftp. The rain of hour h is the field times (h + 1) / 10, so every hour is different."""

    def __init__(self, hours_today=6, missing=None):
        self.hours_today, self.missing = hours_today, missing or {}

    def __call__(self, *a, **k):
        return self

    def close(self):
        pass

    def day_files(self, day):
        hours = range(self.hours_today) if day == TODAY else range(24)
        gone = set(self.missing.get(day, ()))
        return {h: f"/x/{day:%Y%m%d}.{h:02d}00.dat.gz" for h in hours if h not in gone}

    def download(self, path):
        return field((int(path.split(".")[1][:2]) + 1) / 10)


def regions_file(assets, ids=None):
    assets.mkdir(parents=True, exist_ok=True)
    feats = []
    for pid, ((w, s, e, n), _) in SQUARES.items():
        if ids is None or pid in ids:
            feats.append({"type": "Feature", "properties": {"id": pid, "name": pid.upper(), "group": "Test"},
                          "geometry": {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}})
    (assets / "regions.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    return assets


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setenv("GSMAP_USER", SECRET_USER)
    monkeypatch.setenv("GSMAP_PASSWORD", SECRET_PW)

    def go(server=None, *extra, name="a", end="2026-11-09", regions=True):
        monkeypatch.setattr(gsmap, "Ftp", server or Server())
        assets = regions_file(tmp_path / f"assets_{name}") if regions else tmp_path / f"assets_{name}"
        site, pub, cache = tmp_path / f"site_{name}", tmp_path / f"pub_{name}", tmp_path / f"cache_{name}"
        args = ["--end", end, "--today", str(TODAY), "--out", str(site), "--cache-dir", str(cache), "--assets", str(assets),
                "--workers", "1", *extra]
        gsmap.main(args)
        return site, pub, cache
    return go


def load(pub):
    return json.loads((pub / "summary.json").read_text())


def test_summary_days_and_areas(run, tmp_path):
    _, pub, _ = run(Server(), "--include-today", "--publish-dir", str(tmp_path / "pub_a"))
    s = load(pub)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", s["generated_at"])
    assert s["product"] == "nrt" and s["units"] == {"daily": "mm/day", "hourly": "mm/h"}
    assert [d["date"] for d in s["days"]] == [f"2026-11-{d:02d}" for d in range(3, 11)]
    assert [d["hours"] for d in s["days"]] == [24] * 7 + [6]
    assert [d["partial"] for d in s["days"]] == [False] * 7 + [True]
    assert set(s["areas"]) == {"sg", *SQUARES, "sumatra", "kalimantan", "mainland_north"}
    a = s["areas"]["sumatra"]
    assert a["name"] == "Sumatra (Jambi+South Sumatra)" and a["kind"] == "combined" and a["members"] == ["jambi", "ssum"]
    for area in s["areas"].values():
        assert len(area["daily_mean"]) == len(area["daily_max"]) == 8
        assert len(area["hourly_mean"]) == len(area["hourly_max"]) == 7 * 24 + 6


def test_summary_values_follow_the_area_weights(run, tmp_path):
    _, pub, _ = run(Server(), "--include-today", "--publish-dir", str(tmp_path / "pub_a"))
    s = load(pub)["areas"]
    full_day, today = sum(range(1, 25)) / 24 / 10 * 24, sum(range(1, 7)) / 6 / 10 * 24      # mean of (h + 1) / 10, times 24
    assert s["jambi"]["daily_mean"][0] == pytest.approx(3.0 * full_day, abs=0.1)
    assert s["jambi"]["daily_mean"][-1] == pytest.approx(3.0 * today, abs=0.1)
    # combined: the two squares have the same size, so the mean is halfway and the maximum is the larger one
    assert s["sumatra"]["daily_mean"][0] == pytest.approx(4.0 * full_day, abs=0.1)
    assert s["sumatra"]["daily_max"][0] == pytest.approx(5.0 * full_day, abs=0.1)
    assert s["kalimantan"]["daily_mean"][0] == pytest.approx(4.0 * full_day, abs=0.1)      # (2 + 4 + 6) / 3
    assert s["kalimantan"]["daily_max"][0] == pytest.approx(6.0 * full_day, abs=0.1)
    assert s["mainland_north"]["daily_mean"][0] == pytest.approx(3.0 * full_day, abs=0.1)  # (1 + 2 + 3 + 4 + 5) / 5
    assert s["mainland_north"]["daily_max"][0] == pytest.approx(5.0 * full_day, abs=0.1)
    assert s["sg"]["daily_mean"][0] == pytest.approx(BASE * full_day, abs=0.1)             # Singapore: outside every square
    # hourly: a timestamp per hour, mm/h
    hm = s["sumatra"]["hourly_mean"]
    assert hm[0] == {"time": "2026-11-03T00:00:00Z", "value": pytest.approx(0.4)}          # 4.0 * (0 + 1) / 10
    assert hm[23]["time"] == "2026-11-03T23:00:00Z" and hm[23]["value"] == pytest.approx(0.4 * 24)
    assert hm[-1] == {"time": "2026-11-10T05:00:00Z", "value": pytest.approx(0.4 * 6)}     # today stops at its last hour
    assert s["sumatra"]["hourly_max"][-1]["value"] == pytest.approx(0.5 * 6)


def test_today_has_no_minimum_of_hours_but_other_days_still_do(run, tmp_path):
    server = Server(hours_today=1, missing={dt.date(2026, 11, 4): range(10, 24)})           # today: 1 file; 4 Nov: 10 files
    site, pub, _ = run(server, "--include-today", "--publish-dir", str(tmp_path / "pub_a"))
    s = load(pub)
    assert [d["date"] for d in s["days"]] == ["2026-11-03", "2026-11-05", "2026-11-06", "2026-11-07", "2026-11-08",
                                              "2026-11-09", "2026-11-10"]                    # 4 Nov is left out, as before
    assert s["days"][-1] == {"date": "2026-11-10", "hours": 1, "partial": True}
    assert s["areas"]["jambi"]["daily_mean"][-1] == pytest.approx(3.0 * 0.1 * 24)           # one hour: its rate times 24
    assert len(s["areas"]["jambi"]["hourly_mean"]) == 6 * 24 + 1
    assert [d["date"] for d in json.loads((site / "data" / "manifest.json").read_text())["days"]][-1] == "2026-11-09"


def test_today_without_any_file_is_listed_with_nulls(run, tmp_path):
    _, pub, _ = run(Server(hours_today=0), "--include-today", "--publish-dir", str(tmp_path / "pub_a"))
    s = load(pub)
    assert s["days"][-1] == {"date": "2026-11-10", "hours": 0, "partial": True}
    assert s["areas"]["jambi"]["daily_mean"][-1] is None and s["areas"]["jambi"]["daily_max"][-1] is None
    assert len(s["areas"]["jambi"]["hourly_mean"]) == 7 * 24                                  # no hour of today exists yet


def test_a_missing_hour_is_null_and_flags_its_day_partial(run, tmp_path):
    _, pub, _ = run(Server(missing={dt.date(2026, 11, 5): {7}}), "--include-today", "--publish-dir", str(tmp_path / "pub_a"))
    s = load(pub)
    day = s["days"][2]
    assert day == {"date": "2026-11-05", "hours": 23, "partial": True}
    hm = s["areas"]["jambi"]["hourly_mean"][2 * 24 + 7]
    assert hm == {"time": "2026-11-05T07:00:00Z", "value": None}
    assert s["areas"]["jambi"]["daily_mean"][2] is not None


def test_include_today_leaves_the_site_alone(run, tmp_path):
    site_a, _, _ = run(Server(), name="a")
    site_b, pub_b, _ = run(Server(), "--include-today", "--publish-dir", str(tmp_path / "pub_b"), name="b")
    ma, mb = (json.loads((s / "data" / "manifest.json").read_text()) for s in (site_a, site_b))
    ma.pop("generated"), mb.pop("generated")
    assert ma == mb and len(mb["days"]) == 7 and mb["days"][-1]["date"] == "2026-11-09"
    assert {p["id"] for p in mb["places"]} == {"sg", *SQUARES}                                # no combined area on the page
    assert (site_a / "data" / "rain.bin.gz").read_bytes() == (site_b / "data" / "rain.bin.gz").read_bytes()
    for f in ("index.html", "viewer.js", "viewer.css", "data/regions.json"):
        assert (site_a / f).read_bytes() == (site_b / f).read_bytes()
    assert "mainland_north" in load(pub_b)["areas"]


def test_the_data_branch_files(run, tmp_path):
    site, pub, cache = run(Server(), "--include-today", "--publish-dir", str(tmp_path / "pub_a"))
    assert sorted(p.name for p in pub.iterdir()) == ["README.md", "manifest.json", "rain.bin.gz", "summary.json"]
    assert (pub / "manifest.json").read_bytes() == (site / "data" / "manifest.json").read_bytes()
    assert (pub / "rain.bin.gz").read_bytes() == (site / "data" / "rain.bin.gz").read_bytes()
    assert "summary.json" in (pub / "README.md").read_text()
    assert len(gzip.decompress((pub / "rain.bin.gz").read_bytes())) == 7 * gsmap.NY * gsmap.NX * 2
    assert (cache / "day_20261110.npz").exists()                                              # today is cached like the others


def test_today_is_cached_and_only_new_hours_are_fetched(run, tmp_path):
    first = Server(hours_today=3)
    run(first, "--include-today", "--publish-dir", str(tmp_path / "pub_a"))
    later = Server(hours_today=5)
    downloads = []
    later.download = lambda path, orig=later.download: (downloads.append(path), orig(path))[1]
    _, pub, _ = run(later, "--include-today", "--publish-dir", str(tmp_path / "pub_a"))
    assert downloads == ["/x/20261110.0300.dat.gz", "/x/20261110.0400.dat.gz"]              # 2 new hours of today, nothing else
    assert load(pub)["days"][-1]["hours"] == 5


def test_without_the_flag_today_is_not_fetched_and_its_cache_is_dropped(run, tmp_path):
    _, _, cache = run(Server(), "--include-today", "--publish-dir", str(tmp_path / "pub_a"))
    assert (cache / "day_20261110.npz").exists()
    _, _, cache = run(Server(), "--publish-dir", str(tmp_path / "pub_a"))                    # same cache dir name "a"
    assert not (cache / "day_20261110.npz").exists()
    assert load(tmp_path / "pub_a")["days"][-1]["date"] == "2026-11-09"


def test_today_already_in_the_window_is_not_added_twice(run, tmp_path):
    _, pub, _ = run(Server(hours_today=24), "--include-today", "--publish-dir", str(tmp_path / "pub_a"), end="2026-11-10")
    s = load(pub)
    assert [d["date"] for d in s["days"]][-1] == "2026-11-10" and len(s["days"]) == 7
    assert s["days"][-1]["partial"] is False


def test_publishing_needs_every_area(run, tmp_path):
    with pytest.raises(SystemExit) as e:
        run(Server(), "--publish-dir", str(tmp_path / "pub_a"), regions=False)
    assert "missing" in str(e.value) and not (tmp_path / "pub_a" / "summary.json").exists()


def test_credentials_are_never_written_or_printed(run, tmp_path, capsys):
    run(Server(), "--include-today", "--publish-dir", str(tmp_path / "pub_a"))
    out = capsys.readouterr()
    for secret in (SECRET_USER, SECRET_PW):
        assert secret not in out.out and secret not in out.err
        for f in tmp_path.rglob("*"):
            if f.is_file():
                assert secret.encode() not in f.read_bytes(), f


def test_a_rejected_login_does_not_print_the_credentials(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GSMAP_USER", SECRET_USER)
    monkeypatch.setenv("GSMAP_PASSWORD", SECRET_PW)

    class Refusing(Server):
        def day_files(self, day):
            raise ftplib.error_perm("530 Login incorrect.")

    monkeypatch.setattr(gsmap, "Ftp", Refusing())
    with pytest.raises(SystemExit) as e:
        gsmap.main(["--end", "2026-11-09", "--today", str(TODAY), "--include-today", "--out", str(tmp_path / "s"),
                    "--cache-dir", str(tmp_path / "c"), "--workers", "1"])
    out = capsys.readouterr()
    for secret in (SECRET_USER, SECRET_PW):
        assert secret not in str(e.value) and secret not in out.out and secret not in out.err
