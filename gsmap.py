#!/usr/bin/env python3
"""Download JAXA GSMaP hourly rain for the past week, average it per UTC day and build a small website.

    export GSMAP_USER=...  GSMAP_PASSWORD=...        # free JAXA registration, see README
    python gsmap.py                                   # last 7 complete UTC days -> site/
    python gsmap.py --demo                            # synthetic data, no account needed (layout check only)
    python -m http.server -d site

GSMaP files are plain binary: 3600 x 1200 little-endian float32 (0.1 degree, 60N-60S, row 0 = north, column 0 = 0E),
rain rate in mm/h, negative = missing. The map shows the *daily mean rain rate x 24* in mm/day, cropped to the same
domain as the model_forecast site (84-156E, 12S-50N). The time-series window uses the same areas as model_forecast
(`--assets <model_forecast checkout>` supplies its regions.geojson).
"""
import argparse
import datetime as dt
import ftplib
import gzip
import io
import json
import os
import re
import shutil
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

import places as areas

HERE = Path(__file__).resolve().parent

# ---- grid ------------------------------------------------------------------------------------------------
G_NLON, G_NLAT, G_D = 3600, 1200, 0.1      # global GSMaP grid
G_LAT_N, G_LON_W = 60.0, 0.0               # north edge of row 0, west edge of column 0
REGION = {"west": 84.0, "east": 156.0, "south": -12.0, "north": 50.0}   # as in model_forecast
COL0 = int(round((REGION["west"] - G_LON_W) / G_D))
COL1 = int(round((REGION["east"] - G_LON_W) / G_D))
ROW0 = int(round((G_LAT_N - REGION["north"]) / G_D))
ROW1 = int(round((G_LAT_N - REGION["south"]) / G_D))
NX, NY = COL1 - COL0, ROW1 - ROW0
LAT_C0 = G_LAT_N - ROW0 * G_D - G_D / 2    # latitude of the centre of the first (northern) row
LON_C0 = G_LON_W + COL0 * G_D + G_D / 2
GRID = areas.Grid(lat_n=REGION["north"], lon_w=REGION["west"], d=G_D, ny=NY, nx=NX)

# ---- JAXA server -----------------------------------------------------------------------------------------
HOST = "hokusai.eorc.jaxa.jp"
# Candidate directories per product; the day folder is <dir>/YYYY/MM/DD/. The first one that lists files wins.
# Override with --remote-dir (or GSMAP_REMOTE_DIRS) if JAXA moves them: `python gsmap.py --list /` shows the tree.
REMOTE_DIRS = {
    "nrt": ["/realtime_ver/v8/hourly", "/realtime_ver/v7/hourly", "/realtime/hourly"],
    "gauge": ["/realtime_ver/v8/hourly_G", "/realtime_ver/v7/hourly_G", "/realtime/hourly_G"],
}
PRODUCT_NAME = {"nrt": "GSMaP NRT", "gauge": "GSMaP Gauge NRT"}

# ---- colour scale: daily rain in mm/day, one blue hue, light to dark ------------------------------------
BINS = [1, 2, 5, 10, 20, 30, 50, 75, 100, 150]       # lower edges; below BINS[0] is drawn transparent
COLORS = ["#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#5598e7", "#3987e5", "#2a78d6", "#1c5cab", "#184f95", "#0d366b"]
MISSING_U16 = 65535


def warn(msg):
    print(f"WARNING: {msg}", file=sys.stderr, flush=True)
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::warning title=gsmap::{msg}", flush=True)


# ---- reading one hourly file -----------------------------------------------------------------------------
def parse_hourly(raw):
    """Bytes of one (already gunzipped) GSMaP hourly file -> float32 (NY, NX) rain rate in mm/h, NaN where missing."""
    if len(raw) != G_NLON * G_NLAT * 4:
        raise ValueError(f"unexpected file size {len(raw)} (want {G_NLON * G_NLAT * 4}: 3600 x 1200 float32)")
    a = np.frombuffer(raw, dtype="<f4").reshape(G_NLAT, G_NLON)[ROW0:ROW1, COL0:COL1].astype(np.float32)
    a[~np.isfinite(a) | (a < 0)] = np.nan
    nz = np.abs(a[np.isfinite(a) & (a != 0)])
    if nz.size and (nz.max() > 1000 or np.median(nz) < 1e-6):   # junk values: wrong byte order or layout
        raise ValueError("implausible rain rates: the file is not little-endian float32")
    return a


# ---- FTP -------------------------------------------------------------------------------------------------
class Ftp:
    """Small retrying wrapper: lists a day folder once and downloads the hourly files from it."""

    def __init__(self, host, user, password, dirs, timeout=60):
        self.host, self.user, self.password, self.dirs, self.timeout = host, user, password, dirs, timeout
        self.ftp = None

    def _connect(self):
        self.close()
        self.ftp = ftplib.FTP(self.host, timeout=self.timeout)
        self.ftp.login(self.user, self.password)
        self.ftp.set_pasv(True)

    def close(self):
        if self.ftp is not None:
            try:
                self.ftp.quit()
            except Exception:
                pass
            self.ftp = None

    def _retry(self, fn, tries=3):
        last = None
        for i in range(tries):
            try:
                if self.ftp is None:
                    self._connect()
                return fn(self.ftp)
            except ftplib.error_perm:                        # bad login, no permission: retrying cannot help
                raise
            except (ftplib.Error, OSError, EOFError, socket.timeout) as e:
                last = e
                self.close()
                time.sleep(2 * (i + 1))
        raise last

    def list_dir(self, path):
        def go(f):
            try:
                return [n.rsplit("/", 1)[-1] for n in f.nlst(path)]
            except ftplib.error_perm as e:
                if str(e).startswith("550"):                  # no such folder / empty
                    return []
                raise
        return self._retry(go)

    def day_files(self, day):
        """{hour: remote path} for one UTC day."""
        ymd = day.strftime("%Y%m%d")
        for base in self.dirs:
            folder = f"{base.rstrip('/')}/{day:%Y}/{day:%m}/{day:%d}"
            found = {}
            for name in sorted(self.list_dir(folder)):
                m = re.fullmatch(rf"gsmap_[a-z_]*\.{ymd}\.(\d\d)00(?:\.[\w]+)*?\.dat(?:\.gz)?", name)
                if m and int(m.group(1)) not in found:
                    found[int(m.group(1))] = f"{folder}/{name}"
            if found:
                return found
        return {}

    def download(self, path):
        def go(f):
            buf = io.BytesIO()
            f.retrbinary(f"RETR {path}", buf.write)
            return buf.getvalue()
        raw = self._retry(go)
        return gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw


# ---- one day: hourly files -> running sum and count, plus the hourly series of every place -------------------
def day_cache(cache_dir, day):
    return Path(cache_dir) / f"day_{day:%Y%m%d}.npz"


def load_day(path, ids):
    """Cached sums of a day, or an empty day. The hourly place series cannot be rebuilt from the sums, so a cache made for
    another set of places is dropped (its hours are downloaded again)."""
    if path.exists():
        try:
            with np.load(path) as z:
                if z["sum"].shape == (NY, NX) and list(z["ids"]) == ids:
                    return z["sum"].copy(), z["cnt"].copy(), z["hours"].copy(), z["tsm"].copy(), z["tsx"].copy()
        except Exception as e:
            warn(f"ignoring unreadable cache {path.name}: {e}")
    nan = np.full((24, len(ids)), np.nan, np.float32)
    return np.zeros((NY, NX), np.float32), np.zeros((NY, NX), np.uint8), np.zeros(24, bool), nan, nan.copy()


def fetch_day(client, day, cache_dir, places=()):
    """Add every hour of `day` that is not cached yet. Returns (sum, cnt, hours, mean series, max series) and writes the
    cache. The series are (24, n places) hourly rain rates in mm/h."""
    path = day_cache(cache_dir, day)
    ids = [p.id for p in places]
    s, c, hours, tsm, tsx = load_day(path, ids)
    if hours.all():
        return s, c, hours, tsm, tsx
    files = client.day_files(day)
    todo = [h for h in sorted(files) if not hours[h]]
    for h in todo:
        try:
            a = parse_hourly(client.download(files[h]))
        except Exception as e:
            warn(f"{day} {h:02d}Z: {files[h]}: {e}")
            continue
        ok = np.isfinite(a)
        s[ok] += a[ok]
        c += ok
        for k, p in enumerate(places):
            tsm[h, k], tsx[h, k] = p.stats(a)
        hours[h] = True
    print(f"{day}: {hours.sum()}/24 hourly files ({len(todo)} new, {len(files)} on the server)", flush=True)
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, sum=s, cnt=c, hours=hours, tsm=tsm, tsx=tsx, ids=np.array(ids))
    return s, c, hours, tsm, tsx


def daily_mm(s, c, min_hours):
    """Daily mean rain rate x 24 = mm/day; NaN where fewer than `min_hours` hourly values exist."""
    with np.errstate(invalid="ignore", divide="ignore"):
        mm = s / c * 24.0
    mm[c < min_hours] = np.nan
    return mm.astype(np.float32)


# ---- demo data (layout check only; never mistake it for observations) -----------------------------------
def demo_days(days, places=()):
    rng = np.random.default_rng(7)
    lat = LAT_C0 - np.arange(NY)[:, None] * G_D
    lon = LON_C0 + np.arange(NX)[None, :] * G_D
    diurnal = 1 + 0.8 * np.sin(2 * np.pi * (np.arange(24) - 9) / 24)        # peak in the afternoon (UTC+8)
    out = []
    for i, day in enumerate(days):
        mm = np.zeros((NY, NX), np.float32)
        for _ in range(14):
            la, lo = rng.uniform(-10, 30), rng.uniform(95, 150)
            r, amp = rng.uniform(2, 7), rng.uniform(15, 140)
            mm += amp * np.exp(-(((lat - la) ** 2) + ((lon - lo) * 0.9) ** 2) / (2 * r * r))
        itcz = 8 * np.exp(-((lat - 5 + 3 * np.sin(i)) ** 2) / 30)
        mm = (mm + itcz * rng.uniform(0.5, 1.5)).astype(np.float32)
        tsm = np.full((24, len(places)), np.nan, np.float32)
        tsx = tsm.copy()
        for h in range(24):
            hour = mm / 24 * diurnal[h] * rng.uniform(0.6, 1.4)
            for k, p in enumerate(places):
                tsm[h, k], tsx[h, k] = p.stats(hour)
        out.append({"date": day, "mm": mm, "hours": 24, "tsm": tsm, "tsx": tsx})
    return out


# ---- output ----------------------------------------------------------------------------------------------
def encode(mm):
    """mm/day -> uint16 in tenths of a mm, 65535 = missing."""
    u = np.full(mm.shape, MISSING_U16, np.uint16)
    ok = np.isfinite(mm)
    u[ok] = np.clip(np.rint(mm[ok] * 10), 0, MISSING_U16 - 1).astype(np.uint16)
    return u


def domain_mean(mm):
    """Area-weighted (cos latitude) mean over the valid cells, mm/day."""
    lat = LAT_C0 - np.arange(NY) * G_D
    w = np.cos(np.radians(lat))[:, None] * np.ones((1, NX))
    ok = np.isfinite(mm)
    return float((mm[ok] * w[ok]).sum() / w[ok].sum()) if ok.any() else None


def clean(values, nd):
    """Numbers for the manifest: rounded, None (JSON null) where there is no value."""
    return [None if not np.isfinite(v) else round(float(v), nd) for v in values]


def write_site(out, days, places, product, demo, assets):
    """days: [{date, mm (mm/day field), hours, tsm, tsx}] oldest first."""
    out = Path(out)
    data = out / "data"
    if out.exists():
        shutil.rmtree(out)
    data.mkdir(parents=True)
    for f in ("index.html", "viewer.js", "viewer.css"):
        shutil.copy(HERE / "web" / f, out / f)
    stack = np.stack([encode(d["mm"]) for d in days])
    (data / "rain.bin.gz").write_bytes(gzip.compress(stack.astype("<u2").tobytes(), 9, mtime=0))
    coast = Path(assets) / "coastlines.geojson" if assets else None
    if coast and coast.exists():
        shutil.copy(coast, data / "coastlines.json")
    else:
        warn("coastlines.geojson not found (use --assets <model_forecast checkout>); the map is drawn without coastlines")
    regions = Path(assets) / "regions.geojson" if assets else None
    has_regions = bool(regions and regions.exists())
    if has_regions:
        gj = json.loads(regions.read_text())
        for f in gj["features"]:
            f["properties"] = {k: f["properties"][k] for k in ("id", "name", "group") if k in f["properties"]}
        (data / "regions.json").write_text(json.dumps(gj, separators=(",", ":")))
    elif len(places) > len(areas.BOXES):
        warn("regions.geojson not found: no region outlines")
    hours_t = [int(dt.datetime(d["date"].year, d["date"].month, d["date"].day, h, tzinfo=dt.timezone.utc).timestamp())
               for d in days for h in range(24)]
    series = {}
    for k, p in enumerate(places):
        daily = [p.stats(d["mm"]) for d in days]
        series[p.id] = {
            "hourly": {"mean": clean(np.concatenate([d["tsm"][:, k] for d in days]), 2),
                       "max": clean(np.concatenate([d["tsx"][:, k] for d in days]), 2)},
            "daily": {"mean": clean([m for m, _ in daily], 1), "max": clean([x for _, x in daily], 1)}}
    manifest = {
        "title": "Weekly satellite rainfall, southeast Asia and Japan",
        "source": "JAXA GSMaP (demo data)" if demo else f"JAXA {PRODUCT_NAME[product]}",
        "demo": demo,
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "grid": {"nlat": NY, "nlon": NX, "d": G_D, "lat_n": REGION["north"], "lat_s": REGION["south"],
                 "lon_w": REGION["west"], "lon_e": REGION["east"]},
        "scale": {"unit": "mm/day", "bins": BINS, "colors": COLORS, "step": 0.1, "missing": MISSING_U16},
        "days": [{"date": str(d["date"]), "hours": int(d["hours"]), "mean": domain_mean(d["mm"])} for d in days],
        "hours": hours_t,
        "places": [p.describe() for p in places],
        "series": series,
        "files": {"rain": "data/rain.bin.gz", "coast": "data/coastlines.json" if coast and coast.exists() else None,
                  "regions": "data/regions.json" if has_regions else None},
    }
    (data / "manifest.json").write_text(json.dumps(manifest, separators=(",", ":")))
    (out / ".nojekyll").write_text("")
    print(f"wrote {out}: {len(days)} days, {len(places)} places, "
          f"{sum(p.stat().st_size for p in out.rglob('*') if p.is_file()) / 1e6:.1f} MB", flush=True)
    return manifest


# ---- main ------------------------------------------------------------------------------------------------
def window(end, n):
    return [end - dt.timedelta(days=n - 1 - i) for i in range(n)]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=7, help="number of complete UTC days (default 7)")
    ap.add_argument("--end", type=dt.date.fromisoformat, help="last day, YYYY-MM-DD (default: yesterday, UTC)")
    ap.add_argument("--product", choices=list(REMOTE_DIRS), default="nrt",
                    help="nrt = GSMaP_NRT (default); gauge = gauge-adjusted GSMaP_Gauge_NRT")
    ap.add_argument("--host", default=os.environ.get("GSMAP_HOST", HOST))
    ap.add_argument("--remote-dir", default=os.environ.get("GSMAP_REMOTE_DIRS"),
                    help="comma-separated hourly base folder(s) on the server, replacing the built-in guesses")
    ap.add_argument("--min-hours", type=int, default=18,
                    help="a day needs this many hourly files, and a cell this many valid hours (default 18)")
    ap.add_argument("--workers", type=int, default=3, help="parallel FTP connections (default 3)")
    ap.add_argument("--cache-dir", type=Path, default=HERE / "cache")
    ap.add_argument("--assets", type=Path, help="folder holding coastlines.geojson (a model_forecast checkout)")
    ap.add_argument("--out", type=Path, default=HERE / "site")
    ap.add_argument("--demo", action="store_true", help="synthetic data instead of downloading (layout check only)")
    ap.add_argument("--list", metavar="PATH", help="print the server folder PATH and exit (to find the data layout)")
    a = ap.parse_args(argv)

    user = os.environ.get("GSMAP_USER", "").strip()          # pasted secrets often carry a trailing newline or space
    pw = os.environ.get("GSMAP_PASSWORD", "").strip()
    dirs = [d.strip() for d in a.remote_dir.split(",")] if a.remote_dir else REMOTE_DIRS[a.product]
    end = a.end or (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1))
    days = window(end, a.days)

    if a.list is not None:
        if not (user and pw):
            sys.exit("set GSMAP_USER and GSMAP_PASSWORD first")
        c = Ftp(a.host, user, pw, dirs)
        print("\n".join(c.list_dir(a.list)))
        return 0

    regions = a.assets / "regions.geojson" if a.assets else None
    if regions is not None and not regions.exists():
        warn(f"{regions} not found: only the Singapore box gets a time series")
    places = areas.load_places(GRID, regions)

    if a.demo:
        result = demo_days(days, places)
    else:
        if not (user and pw):
            sys.exit("GSMAP_USER and GSMAP_PASSWORD are not set (register free at "
                     "https://sharaku.eorc.jaxa.jp/GSMaP/registration.htm), or use --demo")

        def one(day):
            c = Ftp(a.host, user, pw, dirs)
            try:
                return fetch_day(c, day, a.cache_dir, places)
            finally:
                c.close()

        try:
            with ThreadPoolExecutor(max(1, a.workers)) as pool:
                fetched = list(pool.map(one, days))
        except ftplib.error_perm as e:
            if str(e).startswith("530"):
                sys.exit(f"JAXA rejected the login ({e}). Check GSMAP_USER / GSMAP_PASSWORD: use exactly the user name and "
                         f"password from JAXA's registration email, with no quotes or spaces. "
                         f"Test: curl -u 'USER:PASSWORD' ftp://{a.host}/")
            raise
        result = []
        for day, (s, c, hours, tsm, tsx) in zip(days, fetched):
            n = int(hours.sum())
            if n < a.min_hours:
                warn(f"{day}: only {n} hourly files (need {a.min_hours}); the day is left out")
                continue
            result.append({"date": day, "mm": daily_mm(s, c, a.min_hours), "hours": n, "tsm": tsm, "tsx": tsx})
        keep = {day_cache(a.cache_dir, d).name for d in days}
        for old in Path(a.cache_dir).glob("day_*.npz"):          # the cache only ever holds the current window
            if old.name not in keep:
                old.unlink()
        if not result:
            sys.exit(f"no usable days. Check the account and the server layout: python gsmap.py --list / "
                     f"(tried {', '.join(dirs)})")
    write_site(a.out, result, places, a.product, a.demo, a.assets)
    return 0


if __name__ == "__main__":
    sys.exit(main())
