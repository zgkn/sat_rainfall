import json
import os
from pathlib import Path

import numpy as np
import pytest

import gsmap
import places

GRID = gsmap.GRID
MODEL_FORECAST = Path(os.environ.get("MODEL_FORECAST_DIR", "../model_forecast")) / "regions.geojson"


def square(w, s, e, n):
    return {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}


def test_coverage_of_a_box_aligned_with_the_cells():
    cov = places.coverage(square(100.0, 0.0, 100.5, 0.5), GRID)         # exactly 5 x 5 cells
    assert cov.sum() == pytest.approx(25, abs=0.2)
    assert cov.max() == pytest.approx(1.0, abs=0.01)
    j, i = round((GRID.lat_n - 0.25) / 0.1), round((100.25 - GRID.lon_w) / 0.1)
    assert cov[j, i] == pytest.approx(1.0, abs=0.01)
    assert cov[j - 10, i] == 0


def test_coverage_is_fractional_at_the_edges():
    cov = places.coverage(square(100.05, 0.0, 100.15, 0.1), GRID)       # half of two neighbouring cells
    assert cov.sum() == pytest.approx(1.0, abs=0.05)
    assert (cov > 0.4).sum() == 2


def test_holes_are_cleared():
    outer = [[100, 0], [101, 0], [101, 1], [100, 1], [100, 0]]
    hole = [[100.4, 0.4], [100.6, 0.4], [100.6, 0.6], [100.4, 0.6], [100.4, 0.4]]
    full = places.coverage({"type": "Polygon", "coordinates": [outer]}, GRID).sum()
    holed = places.coverage({"type": "Polygon", "coordinates": [outer, hole]}, GRID).sum()
    assert full == pytest.approx(100, abs=0.5) and holed == pytest.approx(96, abs=0.5)


def test_multipolygon_and_outside_the_grid():
    mp = {"type": "MultiPolygon", "coordinates": [square(100, 0, 100.2, 0.2)["coordinates"], square(110, 5, 110.2, 5.2)["coordinates"]]}
    assert places.coverage(mp, GRID).sum() == pytest.approx(8, abs=0.1)
    assert places.coverage(square(10, 10, 11, 11), GRID).sum() == 0     # in Africa: outside the grid


def test_singapore_box_is_the_model_forecast_box():
    sg = places.load_places(GRID)[0]
    assert sg.id == "sg" and sg.cells == pytest.approx(25, abs=0.3)
    assert sg.box == [[1.125, 103.625], [1.625, 104.125]]


def test_stats_mean_is_weighted_and_max_uses_well_covered_cells():
    w = np.zeros((GRID.ny, GRID.nx), "float32")
    w[10, 10], w[10, 11], w[10, 12] = 1.0, 0.25, 0.25                   # the last two are under half inside
    p = places.Place("x", "X", "box", "Boxes", [[0, 0], [1, 1]], w)
    f = np.zeros((GRID.ny, GRID.nx), "float32")
    f[10, 10], f[10, 11], f[10, 12] = 10.0, 30.0, 50.0
    mean, mx = p.stats(f)
    assert mean == pytest.approx((10 + 0.25 * 30 + 0.25 * 50) / 1.5)
    assert mx == 10.0                                                   # 30 and 50 are in cells less than half inside


def test_stats_with_missing_data():
    w = np.zeros((GRID.ny, GRID.nx), "float32")
    w[10, 10:14] = 1.0
    p = places.Place("x", "X", "box", "Boxes", [[0, 0], [1, 1]], w)
    f = np.full((GRID.ny, GRID.nx), np.nan, "float32")
    assert all(np.isnan(v) for v in p.stats(f))
    f[10, 10] = 4.0                                                     # 1 of 4 cells valid: under half the place
    assert np.isnan(p.stats(f)[0]) and p.stats(f)[1] == 4.0
    f[10, 11] = 6.0
    assert p.stats(f) == (5.0, 6.0)


def test_a_place_must_cover_some_cell():
    with pytest.raises(ValueError):
        places.Place("x", "X", "box", "Boxes", [[0, 0], [1, 1]], np.zeros((GRID.ny, GRID.nx), "float32"))


@pytest.mark.skipif(not MODEL_FORECAST.exists(), reason="model_forecast checkout not available")
def test_all_model_forecast_regions_load_inside_the_grid():
    ps = places.load_places(GRID, MODEL_FORECAST)
    names = {p.name for p in ps}
    assert len(ps) == 1 + len(json.loads(MODEL_FORECAST.read_text())["features"])
    assert {"Singapore", "Riau", "Thailand", "Kyushu (Japan)", "Kota Tinggi (Johor)"} <= names
    by = {p.id: p for p in ps}
    assert 4000 < by["tha"].cells < 4700                                 # Thailand: ~513,000 km2 at ~121 km2 per 0.1 degree cell
    assert by["ktinggi"].cells > 20


def _place(pid, cells):
    w = np.zeros((GRID.ny, GRID.nx), "float32")
    for (j, i), v in cells.items():
        w[j, i] = v
    return places.Place(pid, pid.upper(), "region", "Test", [[0, 0], [1, 1]], w)


def test_combined_area_adds_the_member_weights():
    a, b = _place("a", {(10, 10): 1.0, (10, 11): 0.3}), _place("b", {(20, 20): 1.0, (10, 11): 0.3})
    (c,), missing = places.combined_places([a, b], [{"id": "c", "name": "C", "members": ["a", "b"]}])
    assert missing == [] and c.kind == "combined" and c.members == ["a", "b"] and c.group == "Combined"
    assert c.cells == pytest.approx(2.6)
    assert c.dense()[10, 11] == pytest.approx(0.6)                      # the shared cell counts for both members
    assert c.describe()["members"] == ["a", "b"]


def test_combined_maximum_uses_the_summed_coverage():
    a, b = _place("a", {(10, 10): 1.0, (10, 11): 0.3}), _place("b", {(20, 20): 1.0, (10, 11): 0.3})
    (c,), _ = places.combined_places([a, b], [{"id": "c", "name": "C", "members": ["a", "b"]}])
    f = np.zeros((GRID.ny, GRID.nx), "float32")
    f[10, 10], f[10, 11], f[20, 20] = 1.0, 9.0, 2.0
    assert a.stats(f)[1] == 1.0 and b.stats(f)[1] == 2.0                # alone, the shared cell is under half inside
    assert c.stats(f)[1] == 9.0                                         # together it is 0.6 inside


def test_combined_area_with_a_missing_member_is_skipped():
    a = _place("a", {(10, 10): 1.0})
    out, missing = places.combined_places([a], [{"id": "c", "name": "C", "members": ["a", "zzz"]}])
    assert out == [] and missing == ["zzz"]


@pytest.mark.skipif(not MODEL_FORECAST.exists(), reason="model_forecast checkout not available")
def test_the_three_combined_areas_exist_for_the_real_regions():
    base = places.load_places(GRID, MODEL_FORECAST)
    combined, missing = places.combined_places(base)
    assert missing == [] and [c.id for c in combined] == ["sumatra", "kalimantan", "mainland_north"]
    by = {p.id: p for p in base}
    assert combined[0].cells == pytest.approx(by["jambi"].cells + by["ssum"].cells, abs=1.0)
