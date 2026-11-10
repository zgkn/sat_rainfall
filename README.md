# sat_rainfall

Weekly satellite rainfall for **southeast Asia and Japan** (84-156E, 12S-50N, the same domain and basemap as
[model_forecast](https://github.com/zgkn/model_forecast)). A GitHub Actions workflow downloads the last 7 days of
**JAXA GSMaP** hourly rain, averages it per UTC day, and publishes an interactive map on GitHub Pages.

```
export GSMAP_USER=...  GSMAP_PASSWORD=...
pip install -r requirements.txt
python gsmap.py --assets ../model_forecast        # -> site/   (--assets only adds the coastlines)
python -m http.server -d site                      # open http://localhost:8000
python gsmap.py --demo                             # synthetic data, no account: checks the layout only
```

## The site

* Map of the **daily rain in mm/day** (mean hourly rate x 24, one colour hue, light to dark; under 1 mm/day is left clear).
* The bars show the **area-mean** of the whole map for each day; click a bar (or use the arrow keys, or play) to switch
  day. **7-day mean** averages the daily maps. A table with the hourly-file counts sits under the chart.
* Hover for the value under the cursor. Light and dark mode follow the system, like model_forecast.
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

1. **JAXA account** (free): register at <https://sharaku.eorc.jaxa.jp/GSMaP/registration.htm>; JAXA emails an FTP user
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
* **The server folder layout is the one part not verified against the live server** (it was written from JAXA's format
  description, and the author's environment could not reach JAXA). The script looks for
  `<dir>/YYYY/MM/DD/gsmap_*.YYYYMMDD.HH00*.dat[.gz]` in a few candidate folders (`REMOTE_DIRS` in `gsmap.py`). If the first
  run says *no usable days*, look at the real tree and point the script at it:

  ```
  python gsmap.py --list /                        # then --list /realtime, ...
  python gsmap.py --remote-dir /realtime/hourly   # or set the GSMAP_REMOTE_DIRS variable / edit REMOTE_DIRS
  ```

## Tests

`python -m pytest -q` covers the binary parsing and cropping (a marked cell lands where it should), the daily mean,
the cache and partial-day logic, file-name matching and the build output. None of them needs network access.

Data: GSMaP (c) JAXA. Basemap: Esri. Coastlines: model_forecast.
