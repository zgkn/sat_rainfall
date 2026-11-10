# sat_rainfall

Weekly satellite rainfall for **southeast Asia and Japan** (84-156E, 12S-50N, the same domain and basemap as
[model_forecast](https://github.com/zgkn/model_forecast)). A GitHub Actions workflow downloads the last 7 days of
**JAXA GSMaP** hourly rain, averages it per UTC day, and publishes an interactive map on GitHub Pages.

```
export GSMAP_USER=...  GSMAP_PASSWORD=...
pip install -r requirements.txt
python gsmap.py --assets ../model_forecast        # -> site/   (--assets: coastlines and the areas of the time series)
python -m http.server -d site                      # open http://localhost:8000
python gsmap.py --demo                             # synthetic data, no account: checks the layout only
```

## The site

* Map of the **daily rain in mm/day** (mean hourly rate x 24, one colour hue, light to dark; under 1 mm/day is left clear).
* The bars show the **area-mean** of the whole map for each day; click a bar (or use the arrow keys, or play) to switch
  day. **7-day mean** averages the daily maps. A table with the hourly-file counts sits under the chart.
* Hover for the value under the cursor. Light and dark mode follow the system, like model_forecast.
* **Time series for areas**: click an outlined area (or pick it in the menu) to open a window with the **hourly rain
  rate (mm/h)** and the **daily rain (mm/day)** of the week. **Mean** is the area mean, **Max** the largest cell
  (of the cells at least half inside the area). Hover a chart for the value; click a day to show it on the map. The
  areas are the ones of model_forecast (`regions.geojson` there): Singapore, six Indonesian provinces, five mainland
  countries, two Johor districts and Kyushu. Each cell counts by the fraction of it inside the outline (`places.py`).
* A day is a **UTC** day (07:00-07:00 local in western Indonesia). The window is the 7 complete days ending yesterday.

## The workflow (`.github/workflows/gsmap.yml`)

| Trigger | What runs |
|---|---|
| **Run workflow** (Actions tab) | tests, download, build. Inputs: number of days, `nrt` or `gauge`, `demo` |
| Daily at 05:17 UTC | the same, so the page always shows the last full week |
| Push to any branch | tests only |

The default branch also **deploys GitHub Pages**; other branches (and `demo` runs) only attach the built `site` as a run
artifact. The cropped daily sums are kept in the Actions cache, so after the first run only the newest day is downloaded.

### One-time setup

1. **JAXA account** (free): register at <https://sharaku.eorc.jaxa.jp/GSMaP/registration.html>; JAXA emails an FTP user
   name and password for `hokusai.eorc.jaxa.jp`. Follow JAXA's data policy and keep the credit on the page.
2. Repository **Settings -> Secrets and variables -> Actions**: add `GSMAP_USER` and `GSMAP_PASSWORD`.
3. **Settings -> Pages -> Source: GitHub Actions**.
4. Actions tab -> *Weekly GSMaP rainfall* -> **Run workflow**.

## Data

`gsmap.py` reads the hourly binary files: 3600 x 1200 little-endian float32, 0.1 degree, 60N-60S, row 0 = north,
rain rate in mm/h, negative = missing. A file with the wrong size or implausible values is rejected rather than plotted.

* `--product nrt` (default) is GSMaP_NRT, `--product gauge` the gauge-adjusted GSMaP_Gauge_NRT.
* A day needs at least `--min-hours` (18) hourly files and a cell at least that many valid hours, otherwise it is left
  out; the header of each day shows the file count. Missing hours are retried on the next run.
* Hours are labelled with the time stamp of the JAXA file (`HH00`, UTC). The cache of a day holds the sums and the hourly
  series of every area, so changing the list of areas makes the next run download the week again.
* The hourly files are found as `<dir>/YYYY/MM/DD/gsmap_*.YYYYMMDD.HH00*.dat[.gz]` in a few candidate folders
  (`REMOTE_DIRS` in `gsmap.py`); the first of them worked on JAXA's server in the test run. If JAXA moves them and a run
  says *no usable days*, look at the real tree and point the script at it:

  ```
  python gsmap.py --list /                        # then --list /realtime, ...
  python gsmap.py --remote-dir /realtime/hourly   # or set the GSMAP_REMOTE_DIRS variable / edit REMOTE_DIRS
  ```

## Tests

`python -m pytest -q` covers the binary parsing and cropping (a marked cell lands where it should), the daily mean,
the cache and partial-day logic, file-name matching, the area weights (exact fractions, holes, missing data), the whole
build with a fake server, and the build output. None of them needs network access; the region test reads
`$MODEL_FORECAST_DIR/regions.geojson` (default `../model_forecast`) and is skipped without it.

Data: GSMaP (c) JAXA. Basemap: Esri. Coastlines and areas: model_forecast (boundaries: Natural Earth; Johor districts (c) geoBoundaries, CC BY 4.0).
