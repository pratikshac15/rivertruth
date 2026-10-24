// Minimal stand-in for Leaflet in browser tests (no map tiles offline). Markers are recorded, not drawn,
// and window.__clickGauge(lid) clicks the marker for that gauge the way a person would.
window.L = (function () {
  const markers = [];
  function ev() { const h = {}; return { on(n, f) { (h[n] = h[n] || []).push(f); return this; }, fire(n) { (h[n] || []).forEach((f) => f()); } }; }
  window.__clickGauge = (lid) => { const m = markers.find((x) => x.lid === lid); if (!m) throw new Error("no marker " + lid); m.fire("click"); };
  window.__markers = markers;
  return {
    map(id) {
      const e = ev(); let c = { lat: 38.5, lng: -96.5 }, z = 4;
      const m = Object.assign(e, {
        setView(ll, zz) { c = { lat: ll[0], lng: ll[1] }; z = zz; setTimeout(() => e.fire("moveend"), 5); return m; },
        getBounds() { return { getWest: () => c.lng - 30, getEast: () => c.lng + 30, getSouth: () => c.lat - 15, getNorth: () => c.lat + 15 }; },
        getCenter() { return c; }, getZoom() { return z; },
      });
      document.getElementById(id).textContent = "[map]";
      return m;
    },
    tileLayer() { return { addTo() { return this; } }; },
    layerGroup() { return { addTo() { return this; }, getLayers() { return markers; }, clearLayers() { markers.length = 0; } }; },
    circleMarker(ll, o) {
      const e = ev();
      const m = Object.assign(e, { addTo() { markers.push(m); return m; }, bindTooltip(t) { m.tip = t; return m; } });
      return m;
    },
  };
})();
