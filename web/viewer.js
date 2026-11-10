// Weekly GSMaP rainfall viewer: one uint16 grid per UTC day (tenths of mm/day, 65535 = missing), drawn on a canvas
// that is re-projected from the equirectangular grid to web mercator whenever the map moves.
(function () {
  "use strict";
  var el = function (id) { return document.getElementById(id); };
  var RAD = Math.PI / 180, PLAY_MS = 900, PAD = 0.25;

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

    // ---- map ------------------------------------------------------------------------------------
    var map = L.map("map", {
      zoomControl: true, zoomSnap: 0.25, minZoom: 3, maxZoom: 9, zoomAnimation: false, worldCopyJump: false,
      maxBounds: [[G.lat_s - 10, G.lon_w - 10], [G.lat_n + 10, G.lon_e + 10]]
    });
    L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}",
      { noWrap: true, maxNativeZoom: 16,
        attribution: "GSMaP &copy; JAXA &middot; Tiles &copy; Esri &mdash; Esri, DeLorme, NAVTEQ" }).addTo(map);
    map.fitBounds([[G.lat_s, G.lon_w], [G.lat_n, G.lon_e]]);
    map.createPane("shade").style.zIndex = 410;
    map.createPane("coast").style.zIndex = 420;
    if (M.files.coast) {
      fetch(M.files.coast).then(function (r) { return r.json(); }).then(function (gj) {
        L.geoJSON(gj, { pane: "coast", interactive: false, style: { color: "#1a1a19", weight: 1, opacity: 0.7, fill: false } }).addTo(map);
      }).catch(function () {});
    }
    ["wv-top", "wv-bottom", "wv-legend"].forEach(function (id) {
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

    // ---- current field ----------------------------------------------------------------------------
    var weekArr = new Uint16Array(N);
    (function () {                                          // mean of the days that have a value
      for (var i = 0; i < N; i++) {
        var s = 0, c = 0;
        for (var d = 0; d < nd; d++) { var v = arr[d * N + i]; if (v !== MISS) { s += v; c++; } }
        weekArr[i] = c ? Math.round(s / c) : MISS;
      }
    })();
    function field() { return state.week ? weekArr : arr.subarray(state.day * N, (state.day + 1) * N); }

    // ---- canvas layer -----------------------------------------------------------------------------
    var cv = document.createElement("canvas");
    cv.id = "wv-shade";
    map.getPane("shade").appendChild(cv);
    var ctx = cv.getContext("2d");
    function draw() {
      var sz = map.getSize(), w = Math.round(sz.x * (1 + 2 * PAD)), h = Math.round(sz.y * (1 + 2 * PAD));
      var ox = Math.round(sz.x * PAD), oy = Math.round(sz.y * PAD);
      cv.width = w; cv.height = h;
      L.DomUtil.setPosition(cv, map.containerPointToLayerPoint([-ox, -oy]));
      var colI = new Int32Array(w), rowI = new Int32Array(h), x, y;
      for (x = 0; x < w; x++) {
        var i = Math.floor((map.containerPointToLatLng([x - ox + 0.5, 0]).lng - G.lon_w) / G.d);
        colI[x] = i < 0 || i >= NX ? -1 : i;
      }
      for (y = 0; y < h; y++) {
        var j = Math.floor((G.lat_n - map.containerPointToLatLng([0, y - oy + 0.5]).lat) / G.d);
        rowI[y] = j < 0 || j >= NY ? -1 : j;
      }
      var f = field(), img = ctx.createImageData(w, h), px = new Uint32Array(img.data.buffer);
      for (y = 0; y < h; y++) {
        var j2 = rowI[y]; if (j2 < 0) continue;
        var base = j2 * NX, o = y * w;
        for (x = 0; x < w; x++) { var i2 = colI[x]; if (i2 >= 0) px[o + x] = lut[f[base + i2]]; }
      }
      ctx.putImageData(img, 0, 0);
    }
    map.on("move", function () { L.DomUtil.setPosition(cv, map.containerPointToLayerPoint([-Math.round(map.getSize().x * PAD), -Math.round(map.getSize().y * PAD)])); });
    map.on("moveend zoomend resize", draw);

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
      if (e.key === "ArrowLeft") { stop(); select((state.day + nd - 1) % nd); }
      else if (e.key === "ArrowRight") { stop(); select((state.day + 1) % nd); }
    });

    if (M.demo) el("wv-demo").hidden = false;
    update();
  }
})();
