"""Areas with a time series: the same Singapore box and regions as the model_forecast site.

A place is a weight per grid cell: the fraction of the cell that lies inside the outline (cells are the 0.1 degree
squares of the GSMaP grid). The two statistics are the weighted mean of the cells and the maximum over the cells that are
at least half inside the place (the best covered cell when there is none). `regions.geojson` is model_forecast's file.
"""
import json
from pathlib import Path
from typing import NamedTuple

import numpy as np

OVERSAMPLE = 10             # sub-pixels per cell side when measuring how much of a cell is inside an outline
MAX_COVERAGE = 0.5          # a cell counts for the maximum when at least this much of it is inside
MIN_VALID = 0.5             # the mean needs valid cells covering at least this share of the place

# same box as model_forecast (2 x 2 cells of its 0.25 degree grid around the island), edges in degrees
BOXES = [{"id": "sg", "name": "Singapore", "south": 1.125, "north": 1.625, "west": 103.625, "east": 104.125}]


# areas made of several places: the cell weights of the members are added (a cell shared by two members counts once, at
# most fully), so the mean and the "at least half inside" maximum are those of the joined area
COMBINED = [
    {"id": "sumatra", "name": "Sumatra (Jambi+South Sumatra)", "members": ["jambi", "ssum"]},
    {"id": "kalimantan", "name": "Kalimantan (West+Central+South)", "members": ["wkal", "ckal", "skal"]},
    {"id": "mainland_north", "name": "Mainland north (5 countries)", "members": ["khm", "lao", "mmr", "tha", "vnm"]},
]


class Grid(NamedTuple):
    lat_n: float            # north edge of row 0
    lon_w: float            # west edge of column 0
    d: float                # cell size, degrees
    ny: int
    nx: int


def _polygons(geometry):
    """[[ring, ring, ...], ...]: the first ring of each polygon is the outline, the others are holes."""
    return [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]


def bounds(geometry):
    """[[south, west], [north, east]] of a GeoJSON Polygon/MultiPolygon."""
    pts = np.array([p for poly in _polygons(geometry) for ring in poly for p in ring])
    return [[float(pts[:, 1].min()), float(pts[:, 0].min())], [float(pts[:, 1].max()), float(pts[:, 0].max())]]


def _fill(rings, h, w):
    """(h, w) bool mask of the pixels whose centre (at +0.5) is inside the rings (even-odd rule, so holes are cleared).
    Ring coordinates are in pixels, x to the right and y down. Edges are half-open: a pixel centre exactly on a left or
    top edge counts as inside, one on a right or bottom edge as outside, so neighbouring polygons never double count."""
    mask = np.zeros((h, w), bool)
    e = np.concatenate([np.hstack([r[:-1], r[1:]]) for r in rings])      # one row per edge: x0, y0, x1, y1
    e = e[e[:, 1] != e[:, 3]]                                           # horizontal edges never cross a scanline
    x0, y0, x1, y1 = e.T
    lo, hi = np.minimum(y0, y1), np.maximum(y0, y1)
    for r in range(max(0, int(np.floor(lo.min()))), min(h, int(np.ceil(hi.max())))):
        yc = r + 0.5
        sel = (lo <= yc) & (yc < hi)
        if not sel.any():
            continue
        xi = np.sort(x0[sel] + (yc - y0[sel]) * (x1[sel] - x0[sel]) / (y1[sel] - y0[sel]))
        for a, b in zip(xi[0::2], xi[1::2]):
            c0, c1 = max(0, int(np.ceil(a - 0.5))), min(w, int(np.ceil(b - 0.5)))
            if c1 > c0:
                mask[r, c0:c1] = True
    return mask


def coverage(geometry, grid, oversample=OVERSAMPLE):
    """(ny, nx) fraction of every cell that lies inside the outline."""
    (s, w), (n, e) = bounds(geometry)
    j0 = max(0, int(np.floor((grid.lat_n - n) / grid.d)))
    j1 = min(grid.ny, int(np.ceil((grid.lat_n - s) / grid.d)))
    i0 = max(0, int(np.floor((w - grid.lon_w) / grid.d)))
    i1 = min(grid.nx, int(np.ceil((e - grid.lon_w) / grid.d)))
    out = np.zeros((grid.ny, grid.nx), "float32")
    if j1 <= j0 or i1 <= i0:
        return out
    h, wd = (j1 - j0) * oversample, (i1 - i0) * oversample
    fine = np.zeros((h, wd), bool)
    for poly in _polygons(geometry):                                    # the polygons of a MultiPolygon are joined
        rings = [np.column_stack([((np.asarray(r)[:, 0] - grid.lon_w) / grid.d - i0) * oversample,
                                  ((grid.lat_n - np.asarray(r)[:, 1]) / grid.d - j0) * oversample]) for r in poly]
        fine |= _fill(rings, h, wd)
    out[j0:j1, i0:i1] = fine.reshape(j1 - j0, oversample, i1 - i0, oversample).mean(axis=(1, 3))
    return out


class Place:
    def __init__(self, pid, name, kind, group, box, weights, members=None):
        self.id, self.name, self.kind, self.group, self.box = pid, name, kind, group, box
        self.members = list(members) if members else None
        self.shape = weights.shape
        flat = weights.reshape(-1)
        self.idx = np.flatnonzero(flat > 1e-6)
        if not self.idx.size:
            raise ValueError(f"{name} covers no cell of the map grid")
        self.w = flat[self.idx].astype("float64")
        top = self.w >= MAX_COVERAGE
        self.top = self.idx[top] if top.any() else self.idx[[int(self.w.argmax())]]
        self.cells = float(self.w.sum())

    def stats(self, field):
        """(weighted mean, maximum over the best covered cells) of a (ny, nx) field; NaN where there is no data."""
        flat = np.asarray(field).reshape(-1)
        v = flat[self.idx]
        ok = np.isfinite(v)
        mean = float((v[ok] * self.w[ok]).sum() / self.w[ok].sum()) if self.w[ok].sum() >= MIN_VALID * self.w.sum() else np.nan
        t = flat[self.top]
        t = t[np.isfinite(t)]
        return mean, (float(t.max()) if t.size else np.nan)

    def dense(self):
        """The cell weights as a (ny, nx) array."""
        out = np.zeros(int(np.prod(self.shape)), "float32")
        out[self.idx] = self.w
        return out.reshape(self.shape)

    def describe(self):
        d = {"id": self.id, "name": self.name, "kind": self.kind, "group": self.group, "box": self.box,
             "cells": round(self.cells, 1)}
        if self.members:
            d["members"] = self.members
        return d


def load_places(grid, regions_path=None):
    """The Singapore box first, then every region of the file (when there is one)."""
    places = []
    for b in BOXES:
        ring = [[b["west"], b["south"]], [b["east"], b["south"]], [b["east"], b["north"]], [b["west"], b["north"]], [b["west"], b["south"]]]
        places.append(Place(b["id"], b["name"], "box", "Boxes", [[b["south"], b["west"]], [b["north"], b["east"]]],
                            coverage({"type": "Polygon", "coordinates": [ring]}, grid)))
    if regions_path and Path(regions_path).exists():
        for f in json.loads(Path(regions_path).read_text())["features"]:
            p = f["properties"]
            places.append(Place(p["id"], p["name"], "region", p.get("group", "Regions"), bounds(f["geometry"]),
                                coverage(f["geometry"], grid)))
    return places


def combined_places(places, specs=COMBINED):
    """(the combined places that can be built from `places`, the ids of the members that are missing)."""
    by_id = {p.id: p for p in places}
    out, missing = [], []
    for spec in specs:
        gone = [m for m in spec["members"] if m not in by_id]
        if gone:
            missing += gone
            continue
        members = [by_id[m] for m in spec["members"]]
        w = np.minimum(sum(m.dense() for m in members), 1.0).astype("float32")
        box = [[min(m.box[0][0] for m in members), min(m.box[0][1] for m in members)],
               [max(m.box[1][0] for m in members), max(m.box[1][1] for m in members)]]
        out.append(Place(spec["id"], spec["name"], "combined", "Combined", box, w, members=spec["members"]))
    return out, missing
