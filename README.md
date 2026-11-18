# Data branch: GSMaP rainfall, southeast Asia and Japan

Machine-readable results of the weekly satellite rainfall page of `sat_rainfall`
(<https://zgkn.github.io/sat_rainfall/>). The workflow force-pushes this branch as a **single commit** after every build
on the default branch (every 3 hours), so it only ever holds the latest results. Read the files raw, for example
`https://raw.githubusercontent.com/zgkn/sat_rainfall/data/summary.json`.

Source: JAXA GSMaP hourly rain rate (0.1 degree). All times are **UTC**; an hour is labelled with the time stamp of the
JAXA file (`HH:00`). Data: GSMaP (c) JAXA.

## summary.json (start here)

```
generated_at   UTC time of the build
product        "nrt" (GSMaP_NRT) or "gauge" (gauge-adjusted GSMaP_Gauge_NRT)
units          {"daily": "mm/day", "hourly": "mm/h"}
days           [{date (UTC), hours (hourly files used, 0-24), partial (bool)}], oldest first
areas          {id: {name, kind, group, members?, cells,
                     daily_mean, daily_max      one value per entry of `days` (null = no data), mm/day
                     hourly_mean, hourly_max    [{time, value}], one entry per hour, mm/h (null = file missing)}}
```

* The last day is **today** (UTC), `partial: true`, built from the hourly files there are so far (`hours`). Its daily values
  are the mean rain *rate* of those hours times 24 (not an accumulation), so they move during the day. Its hourly list
  stops at the last hour that exists. Earlier days list all 24 hours; `partial` is true for them only when files are
  missing (a day needs at least 18 hourly files to appear).
* `mean` is the mean of the grid cells, each weighted by the fraction of it inside the area; `max` is the largest cell
  among those at least half inside the area (the best covered cell if none is). The areas are the ones of the
  `model_forecast` site: Singapore, Riau, Jambi, South Sumatra, West/Central/South Kalimantan, Cambodia, Lao PDR,
  Myanmar, Thailand, Vietnam, Kota Tinggi, Mersing, Kyushu, plus the combined areas `sumatra`, `kalimantan` and
  `mainland_north` (`members` lists their parts; their cell weights are the sum of the members').
* `daily_*` is the mean rate over the day times 24, in mm/day.
* The mean includes edge cells that are less than half inside the area and the max does not, so in small areas (Singapore,
  the Johor districts) the hourly **max can be a little below the mean** (by a few hundredths of mm/h) when the only
  rain falls on an edge cell.

## manifest.json and rain.bin.gz (the map of the page)

Copies of the page's `data/` files, for the **complete days only** (today is not in them).

* `manifest.json`: `grid` (`lat_n`, `lat_s`, `lon_w`, `lon_e` edges, `d` = 0.1 degree, `nlat`, `nlon`), `days`, `scale`, the
  page's `places` and their `series`, and `hours` (Unix seconds of every hourly value).
* `rain.bin.gz`: gzip of little-endian `uint16`, shape `(len(days), nlat, nlon)`, row 0 = north, column 0 = west. Value =
  tenths of mm/day (`x * 0.1`); `65535` = no data. In Python:
  `np.frombuffer(gzip.decompress(raw), "<u2").reshape(len(m["days"]), m["grid"]["nlat"], m["grid"]["nlon"])`.
