// Weekly GSMaP rainfall viewer: one uint16 grid per UTC day (tenths of mm/day, 65535 = missing).
// The grid is painted once per day into a mercator-aligned image that Leaflet stretches over the map, so panning and
// zooming are done by the browser (no redraw while the map moves). Areas can be clicked for their hourly/daily series.
(function () {
  "use strict";
  var el = function (id) { return document.getElementById(id); };
  var RAD = Math.PI / 180, PLAY_MS = 900, DAY = 86400, HOUR = 3600;

  function fail(msg) { el("wv-valid").textContent = msg; }

  fetch("data/manifest.json").then(function (r) { if (!r.ok) throw new Error("manifest " + r.status); return r.json(); })
    .then(function (M) { return load(M).then(function (arr) { start(M, arr); }); })
    .catch(function (e) { fail("Could not load the data (" + e.message + "). Serve the folder over HTTP."); });

  function load(M) {
    return fetch(M.files.rain).then(function (r) { if (!r.ok) throw new Error("rain " + r.status); return r.arrayBuffer(); })
      .then(function (buf) {
        var b = new Uint8Array(buf);
        if (b[0] !== 0x1f || b[1] !== 0x8b) return buf;      // the server already decoded it
        return new Response(new Blob([buf]).stream().pipeThrough(new DecompressionStream("gzip"))).arrayBuffer();
      })
      .then(function (buf) { return new Uint16Array(buf); });
  }

  function start(M, arr) {
    var G = M.grid, NX = G.nlon, NY = G.nlat, N = NX * NY, S = M.scale, MISS = S.missing, nd = M.days.length;
    var state = { day: nd - 1, week: false }, timer = null;
    var BOUNDS = [[G.lat_s, G.lon_w], [G.lat_n, G.lon_e]];

    // ---- map ------------------------------------------------------------------------------------
    var map = L.map("map", {
      zoomControl: true, zoomSnap: 0.25, minZoom: 3, maxZoom: 9, worldCopyJump: false,
      maxBounds: [[G.lat_s - 10, G.lon_w - 10], [G.lat_n + 10, G.lon_e + 10]]
    });
    L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}",
      { noWrap: true, maxNativeZoom: 16,
        attribution: "GSMaP &copy; JAXA &middot; Tiles &copy; Esri &mdash; Esri, DeLorme, NAVTEQ" }).addTo(map);
    map.fitBounds(BOUNDS);
    map.createPane("shade").style.zIndex = 410;
    map.createPane("coast").style.zIndex = 420;
    map.createPane("sg-halo").style.zIndex = 455;
    map.createPane("sg").style.zIndex = 460;
    if (M.files.coast) {
      fetch(M.files.coast).then(function (r) { return r.json(); }).then(function (gj) {
        L.geoJSON(gj, { pane: "coast", interactive: false, style: { color: "#1a1a19", weight: 1, opacity: 0.7, fill: false } }).addTo(map);
      }).catch(function () {});
    }
    ["wv-top", "wv-bottom", "wv-legend", "wv-sg"].forEach(function (id) {
      L.DomEvent.disableClickPropagation(el(id)); L.DomEvent.disableScrollPropagation(el(id));
    });

    // ---- colours: value (tenths of mm) -> packed ABGR, via a lookup table --------------------------
    var ALPHA = 0.86;
    function pack(hex) {
      var r = parseInt(hex.slice(1, 3), 16), g = parseInt(hex.slice(3, 5), 16), b = parseInt(hex.slice(5, 7), 16);
      return ((Math.round(255 * ALPHA) << 24) | (b << 16) | (g << 8) | r) >>> 0;
    }
    var packed = S.colors.map(pack);
    var lut = new Uint32Array(65536);                       // 0 = transparent
    (function () {
      var edges = S.bins.map(function (b) { return Math.round(b / S.step); }), k = -1;
      for (var v = 0; v < MISS; v++) {
        while (k + 1 < edges.length && v >= edges[k + 1]) k++;
        lut[v] = k >= 0 ? packed[k] : 0;
      }
    })();

    // ---- fields -----------------------------------------------------------------------------------
    var weekArr = new Uint16Array(N);
    (function () {                                          // mean of the days that have a value
      for (var i = 0; i < N; i++) {
        var s = 0, c = 0;
        for (var d = 0; d < nd; d++) { var v = arr[d * N + i]; if (v !== MISS) { s += v; c++; } }
        weekArr[i] = c ? Math.round(s / c) : MISS;
      }
    })();
    function key() { return state.week ? "week" : state.day; }
    function field() { return state.week ? weekArr : arr.subarray(state.day * N, (state.day + 1) * N); }

    // ---- raster: rows follow web mercator, so Leaflet only has to stretch the picture over the bounds ---------
    var OS = 2, OW = NX * OS;                               // 2 output pixels per cell across
    function merc(lat) { return Math.log(Math.tan(Math.PI / 4 + lat * RAD / 2)); }
    var yN = merc(G.lat_n), yS = merc(G.lat_s);
    var OH = Math.round(OW * (yN - yS) / ((G.lon_e - G.lon_w) * RAD));
    var colI = new Int32Array(OW), rowI = new Int32Array(OH), x, y;
    for (x = 0; x < OW; x++) colI[x] = Math.min(NX - 1, Math.floor((x + 0.5) / OW * NX));
    for (y = 0; y < OH; y++) {
      var lat = (2 * Math.atan(Math.exp(yN - (y + 0.5) / OH * (yN - yS))) - Math.PI / 2) / RAD;
      rowI[y] = Math.max(0, Math.min(NY - 1, Math.floor((G.lat_n - lat) / G.d)));
    }
    var cv = document.createElement("canvas"); cv.width = OW; cv.height = OH;
    var ctx = cv.getContext("2d"), urls = {};
    function pictureUrl() {
      var k = key();
      if (urls[k]) return urls[k];
      var f = field(), img = ctx.createImageData(OW, OH), px = new Uint32Array(img.data.buffer);
      for (var yy = 0; yy < OH; yy++) {
        var base = rowI[yy] * NX, o = yy * OW;
        for (var xx = 0; xx < OW; xx++) px[o + xx] = lut[f[base + colI[xx]]];
      }
      ctx.putImageData(img, 0, 0);
      return (urls[k] = cv.toDataURL("image/png"));
    }
    var overlay = L.imageOverlay(pictureUrl(), BOUNDS, { pane: "shade", interactive: false }).addTo(map);
    function draw() { overlay.setUrl(pictureUrl()); }

    // ---- hover readout ----------------------------------------------------------------------------
    var ro = el("wv-readout");
    map.on("mousemove", function (e) {
      var i = Math.floor((e.latlng.lng - G.lon_w) / G.d), j = Math.floor((G.lat_n - e.latlng.lat) / G.d);
      if (i < 0 || i >= NX || j < 0 || j >= NY) { ro.hidden = true; return; }
      var v = field()[j * NX + i];
      ro.hidden = false;
      ro.textContent = Math.abs(e.latlng.lat).toFixed(2) + (e.latlng.lat < 0 ? "°S " : "°N ") + e.latlng.lng.toFixed(2) + "°E  " +
        (v === MISS ? "no data" : (v * S.step).toFixed(1) + " mm/day" + (state.week ? " (7-day mean)" : ""));
    });
    map.on("mouseout", function () { ro.hidden = true; });

    // ---- legend ----------------------------------------------------------------------------------
    var bar = el("wv-legend-bar"), ticks = el("wv-legend-ticks");
    S.colors.forEach(function (c, i) {
      var s = document.createElement("span"); s.style.background = c; bar.appendChild(s);
      var t = document.createElement("span"); t.style.left = (i / S.colors.length * 100) + "%"; t.textContent = S.bins[i]; ticks.appendChild(t);
    });

    // ---- day chart + table ---------------------------------------------------------------------
    function fmtDay(s, long) {
      var d = new Date(s + "T00:00:00Z");
      return long ? d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", year: "numeric", timeZone: "UTC" })
                  : d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", timeZone: "UTC" });
    }
    var chart = el("wv-chart"), axis = document.createElement("div"), buttons = [];
    axis.id = "wv-axis"; chart.parentNode.insertBefore(axis, chart.nextSibling);
    var maxMean = Math.max.apply(null, M.days.map(function (d) { return d.mean || 0; })) || 1, BAR_PX = 76;
    M.days.forEach(function (d, i) {
      var b = document.createElement("button"); b.type = "button";
      b.setAttribute("aria-label", fmtDay(d.date, true) + ": " + (d.mean == null ? "no data" : d.mean.toFixed(1) + " mm/day area mean"));
      var v = document.createElement("div"); v.className = "val"; v.textContent = d.mean == null ? "–" : d.mean.toFixed(1);
      var r = document.createElement("div"); r.className = "bar"; r.style.height = Math.max(2, (d.mean || 0) / maxMean * BAR_PX) + "px";
      b.appendChild(v); b.appendChild(r); chart.appendChild(b); buttons.push(b);
      b.addEventListener("click", function () { stop(); select(i); });
      var a = document.createElement("span"); a.textContent = fmtDay(d.date, false); axis.appendChild(a);
      var tr = el("wv-table").tBodies[0].insertRow();
      [d.date, d.hours + "/24", d.mean == null ? "–" : d.mean.toFixed(1)].forEach(function (t) { tr.insertCell().textContent = t; });
    });

    function select(i) { state.day = i; state.week = false; update(); }
    function update() {
      buttons.forEach(function (b, i) { b.setAttribute("aria-pressed", String(!state.week && i === state.day)); });
      chart.classList.toggle("all", state.week);
      el("wv-week").setAttribute("aria-pressed", String(state.week));
      var first = M.days[0].date, last = M.days[nd - 1].date, d = M.days[state.day];
      el("wv-valid").textContent = state.week ? "7-day mean, " + fmtDay(first, true).replace(/^\w+ /, "") + " – " + fmtDay(last, true).replace(/^\w+ /, "")
                                              : fmtDay(d.date, true) + " (UTC day)";
      el("wv-run").textContent = M.source + " · " + (state.week ? "mean of the daily values" : d.hours + "/24 hourly files") + " · built " + M.generated;
      draw();
      if (sgOpen) sgRender();
    }

    // ---- controls -----------------------------------------------------------------------------
    function stop() { if (timer) { clearInterval(timer); timer = null; } el("wv-play").innerHTML = "&#9654;"; el("wv-play").setAttribute("aria-label", "Play"); }
    el("wv-play").addEventListener("click", function () {
      if (timer) return stop();
      state.week = false; el("wv-play").innerHTML = "&#10074;&#10074;"; el("wv-play").setAttribute("aria-label", "Pause");
      timer = setInterval(function () { select((state.day + 1) % nd); }, PLAY_MS);
    });
    el("wv-week").addEventListener("click", function () { stop(); state.week = !state.week; update(); });
    document.addEventListener("keydown", function (e) {
      if (/^(SELECT|INPUT|TEXTAREA)$/.test(e.target.tagName)) return;
      if (e.key === "ArrowLeft") { stop(); select((state.day + nd - 1) % nd); }
      else if (e.key === "ArrowRight") { stop(); select((state.day + 1) % nd); }
    });

    // ---- area time series: click an outline (or pick a place) --------------------------------------
    var P = M.places || [], sgPlace = P.length ? P[0].id : null, sgOpen = false, sgStat = "mean", sgGeo = null;
    var SG_VARS = [["hourly", "Hourly rain"], ["daily", "Daily rain"]], sgSel = ["hourly", "daily"];
    try {
      if (localStorage.getItem("sat-sg-stat") === "max") sgStat = "max";
      var saved = JSON.parse(localStorage.getItem("sat-sg-vars") || "null");
      if (Array.isArray(saved) && saved.length) sgSel = saved.filter(function (v) { return SG_VARS.some(function (s) { return s[0] === v; }); });
      if (!sgSel.length) sgSel = ["hourly", "daily"];
    } catch (e) { /* storage blocked */ }
    var SVGNS = "http://www.w3.org/2000/svg";
    function svgEl(name, attrs, text) {
      var e = document.createElementNS(SVGNS, name);
      Object.keys(attrs).forEach(function (k) { e.setAttribute(k, attrs[k]); });
      if (text != null) e.textContent = text;
      return e;
    }
    function placeObj() { return P.filter(function (p) { return p.id === sgPlace; })[0]; }
    var dayStart = M.days.map(function (d) { return Date.parse(d.date + "T00:00:00Z") / 1000; });
    function niceMax(v, floor) {
      var m = Math.max(v, floor), p = Math.pow(10, Math.floor(Math.log10(m)));
      return [1, 2, 5, 10].map(function (k) { return k * p; }).filter(function (c) { return c >= m; })[0];
    }
    function fmtT(t, withHour) {
      var o = { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" };
      if (withHour) { o.hour = "2-digit"; o.minute = "2-digit"; o.hour12 = false; }
      return new Date(t * 1000).toLocaleString("en-GB", o) + (withHour ? " UTC" : "");
    }
    function fix(v, nd) { return v == null ? "no data" : v.toFixed(nd); }

    function sgChips() {
      var box = el("wv-sg-vars"); box.innerHTML = "";
      SG_VARS.forEach(function (v) {
        var b = document.createElement("button"); b.type = "button"; b.textContent = v[1];
        b.setAttribute("aria-pressed", String(sgSel.indexOf(v[0]) >= 0));
        b.addEventListener("click", function () {
          var k = sgSel.indexOf(v[0]);
          if (k >= 0) { if (sgSel.length === 1) return; sgSel.splice(k, 1); } else sgSel.push(v[0]);
          try { localStorage.setItem("sat-sg-vars", JSON.stringify(sgSel)); } catch (e) { /* keep it for this visit */ }
          sgRender();
        });
        box.appendChild(b);
      });
    }

    function sgRender() {
      var pl = placeObj(), s = M.series[sgPlace], box = el("wv-sg-chart");
      el("wv-sg-place").value = sgPlace;
      el("wv-sg-sub").textContent = (pl.kind === "box" ? "0.5° × 0.5° box" : "about " + Math.round(pl.cells) + " grid cells") + " · " +
        (sgStat === "max" ? "maximum of the cells at least half inside" : "area mean") + " · times in UTC";
      document.querySelectorAll("#wv-sg-stat button").forEach(function (b) { b.setAttribute("aria-selected", String(b.dataset.stat === sgStat)); });
      sgChips();
      box.innerHTML = ""; box.classList.toggle("all", state.week); sgGeo = null;
      var tag = sgStat === "max" ? " · max of cells" : "";
      var specs = [];
      SG_VARS.forEach(function (v) {
        if (sgSel.indexOf(v[0]) < 0) return;
        if (v[0] === "hourly") specs.push({ id: "hourly", title: "Hourly rain rate (mm/h)" + tag, t: M.hours, dur: HOUR, v: s.hourly[sgStat], floor: 1 });
        else specs.push({ id: "daily", title: "Daily rain (mm/day)" + tag, t: dayStart, dur: DAY, v: s.daily[sgStat], floor: 10 });
      });
      var W = Math.max(240, box.clientWidth), PL = 32, PR = 6, PH = window.innerWidth < 760 ? 46 : 54, GAP = 22, AX = 16, pw = W - PL - PR;
      var t0 = dayStart[0], t1 = dayStart[nd - 1] + DAY, span = t1 - t0;
      function X(t) { return PL + (t - t0) / span * pw; }
      var H = specs.length * (GAP + PH) + AX;
      var svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H, width: W, height: H, role: "img",
                               "aria-label": "Rain time series at " + pl.name });
      specs.forEach(function (sp, k) {
        var y0 = k * (GAP + PH) + GAP, m = 0;
        sp.v.forEach(function (v) { if (v != null && v > m) m = v; });
        var vmax = niceMax(m, sp.floor);
        function Y(v) { return y0 + PH - Math.min(v, vmax) / vmax * PH; }
        svg.appendChild(svgEl("text", { "class": "sg-title", x: 0, y: y0 - 10 }, sp.title));
        if (!state.week) svg.appendChild(svgEl("rect", { "class": "sg-sel", x: X(dayStart[state.day]), y: y0, width: DAY / span * pw, height: PH }));
        [0, vmax / 2, vmax].forEach(function (v) {
          svg.appendChild(svgEl("line", { "class": "sg-grid", x1: PL, x2: W - PR, y1: Y(v), y2: Y(v) }));
          svg.appendChild(svgEl("text", { x: PL - 4, y: Y(v) + 3, "text-anchor": "end" }, String(+v.toFixed(2))));
        });
        dayStart.forEach(function (t) { svg.appendChild(svgEl("line", { "class": "sg-day", x1: X(t), x2: X(t), y1: y0, y2: y0 + PH })); });
        sp.v.forEach(function (v, i) {
          if (v == null || v <= 0) return;
          var w = sp.dur / span * pw, daily = sp.id === "daily";
          svg.appendChild(svgEl("rect", { "class": daily ? "sg-bar-day" + (!state.week && i === state.day ? " on" : "") : "sg-bar",
            x: X(sp.t[i]) + (daily ? 2 : 0.25), y: Y(v), width: Math.max(1, w - (daily ? 4 : 0.5)), height: y0 + PH - Y(v) }));
        });
      });
      var ay = H - AX + 2, stride = Math.max(1, Math.ceil(nd * 40 / pw));
      dayStart.forEach(function (t, i) {
        if (i % stride) return;
        svg.appendChild(svgEl("text", { x: X(t) + DAY / span * pw / 2, y: ay + 9, "text-anchor": "middle" },
          new Date(t * 1000).toLocaleDateString("en-GB", { weekday: "short", day: "numeric", timeZone: "UTC" })));
      });
      svg.appendChild(svgEl("line", { id: "wv-sg-hov", "class": "sg-hover", x1: 0, x2: 0, y1: GAP - 4, y2: H - AX, visibility: "hidden" }));
      box.appendChild(svg);
      sgGeo = { X: X, t0: t0, span: span, PL: PL, pw: pw, W: W, svg: svg };

      function timeAt(ev) {
        var r = svg.getBoundingClientRect(), xx = (ev.clientX - r.left) / r.width * W;
        return t0 + Math.min(1, Math.max(0, (xx - PL) / pw)) * span;
      }
      svg.addEventListener("pointermove", function (ev) { sgHover(timeAt(ev)); });
      svg.addEventListener("pointerleave", function () { sgHover(null); });
      svg.addEventListener("pointerdown", function (ev) {            // clicking a day shows it on the map
        var i = Math.min(nd - 1, Math.max(0, Math.floor((timeAt(ev) - t0) / DAY)));
        stop(); select(i);
      });
      sgHover(null);
    }

    function sgHover(t) {                  // t: seconds under the pointer, or null for the day shown on the map
      var line = el("wv-sg-hov"), s = M.series[sgPlace], read = el("wv-sg-read");
      if (line && sgGeo) {
        line.setAttribute("x1", t == null ? 0 : sgGeo.X(t)); line.setAttribute("x2", t == null ? 0 : sgGeo.X(t));
        line.setAttribute("visibility", t == null ? "hidden" : "visible");
      }
      var tag = sgStat === "max" ? " (max of cells)" : "";
      if (t == null) {
        if (state.week) {
          var vals = s.daily[sgStat].filter(function (v) { return v != null; });
          read.textContent = vals.length ? "7-day mean " + (vals.reduce(function (a, b) { return a + b; }, 0) / vals.length).toFixed(1) + " mm/day" + tag : "no data";
          return;
        }
        var hs = s.hourly[sgStat].slice(state.day * 24, state.day * 24 + 24), pk = -1;
        hs.forEach(function (v, h) { if (v != null && (pk < 0 || v > hs[pk])) pk = h; });
        read.textContent = fmtT(dayStart[state.day], false) + " · " + fix(s.daily[sgStat][state.day], 1) + " mm/day" + tag +
          (pk >= 0 ? " · peak hour " + String(pk).padStart(2, "0") + ":00 UTC, " + hs[pk].toFixed(2) + " mm/h" : "");
        return;
      }
      var di = Math.min(nd - 1, Math.max(0, Math.floor((t - dayStart[0]) / DAY))), hi = di * 24 + Math.floor((t - dayStart[di]) / HOUR);
      read.textContent = fmtT(M.hours[hi], true) + " · " + fix(s.hourly[sgStat][hi], 2) + " mm/h · " +
        fmtT(dayStart[di], false) + " total " + fix(s.daily[sgStat][di], 1) + " mm/day" + tag;
    }

    var sgLayers = {}, SG_STYLE = { color: "#1a1a19", weight: 1.6, fillColor: "#ffffff", fillOpacity: 0.08 };
    var SG_HALO = { color: "#ffffff", weight: 4.5, opacity: 0.85, fill: false, interactive: false };   // white casing so the line reads on any shading
    function sgStyle() {                   // the place shown in the panel is drawn heavier and with more fill
      P.forEach(function (p) {
        var layer = sgLayers[p.id];
        if (layer) layer.setStyle(sgOpen && p.id === sgPlace ? { weight: 3, fillOpacity: 0.25 } : { weight: SG_STYLE.weight, fillOpacity: SG_STYLE.fillOpacity });
      });
    }
    function sgShow(open, id) {
      if (id) sgPlace = id;
      sgOpen = open; el("wv-sg").hidden = !open;
      document.body.classList.toggle("wv-sg-open", open);
      if (open) sgRender();
      sgStyle();
    }
    if (P.length) {
      var psel = el("wv-sg-place"), og = {};
      P.forEach(function (p) {
        var g = p.group || "Places";
        if (!og[g]) { og[g] = document.createElement("optgroup"); og[g].label = g; psel.appendChild(og[g]); }
        og[g].appendChild(new Option(p.name, p.id));
        if (p.kind !== "box") return;
        L.rectangle(p.box, Object.assign({ pane: "sg-halo" }, SG_HALO)).addTo(map);
        var rect = L.rectangle(p.box, Object.assign({ pane: "sg" }, SG_STYLE));
        var hit = L.circleMarker(L.latLngBounds(p.box).getCenter(),    // a click target that stays usable when zoomed out
          { pane: "sg", radius: 9, stroke: false, fillColor: "#ffffff", fillOpacity: 0.01 });
        [rect, hit].forEach(function (l) { l.addTo(map); l.on("click", function () { sgShow(true, p.id); }); });
        sgLayers[p.id] = rect;
      });
      if (M.files.regions) {
        fetch(M.files.regions).then(function (r) { return r.json(); }).then(function (gj) {
          L.geoJSON(gj, { pane: "sg-halo", style: SG_HALO, interactive: false }).addTo(map);
          L.geoJSON(gj, { pane: "sg", style: SG_STYLE,
            onEachFeature: function (f, layer) {
              sgLayers[f.properties.id] = layer;
              layer.on("click", function () { sgShow(true, f.properties.id); });
            } }).addTo(map);
          sgStyle();
        }).catch(function () { /* the outlines are optional: the place menu still works */ });
      }
      psel.addEventListener("change", function () { sgShow(true, psel.value); });
      el("wv-sg-close").addEventListener("click", function () { sgShow(false); });
      document.querySelectorAll("#wv-sg-stat button").forEach(function (b) {
        b.addEventListener("click", function () {
          if (sgStat === b.dataset.stat) return;
          sgStat = b.dataset.stat;
          try { localStorage.setItem("sat-sg-stat", sgStat); } catch (e) { /* storage blocked */ }
          sgRender();
        });
      });
      window.addEventListener("resize", function () { if (sgOpen) sgRender(); });
    } else {
      el("wv-hint").hidden = true;
    }

    if (M.demo) el("wv-demo").hidden = false;
    update();
  }
})();
