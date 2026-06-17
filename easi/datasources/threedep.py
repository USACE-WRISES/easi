"""USGS 3DEP reach geomorphology — DEM cross-sections -> entrenchment + BHR.

Fetches a 10 m 3DEP DEM over a buffer around the reach, casts perpendicular
transects along the centerline, samples station-elevation profiles, and hands
them to ``easi.geomorph`` for the Rosgen entrenchment ratio and bank-height
ratio. Buffer half-width scales with the regional bankfull width so the
flood-prone width is captured. Never raises — returns {} on failure.

10 m DEM makes small channels sub-pixel and bankfull is curve-estimated, so the
result is an approximate screening value (M/L confidence, overrideable).
"""
from __future__ import annotations

from .. import geomorph


def reach_geomorphology(reach_geojson: dict | None, da_sqkm: float,
                        spacing: float = 10.0, n_transects: int = 9,
                        bankfull: tuple[float, float] | None = None,
                        division: str | None = None) -> dict:
    """``bankfull`` = optional precomputed (width_m, depth_m) regional estimate
    (Bieger) for the analysis location; falls back to the national curve."""
    if not reach_geojson:
        return {}
    try:
        import geopandas as gpd
        import numpy as np
        import py3dep
        import xarray as xr
        from shapely.geometry import LineString

        feats = reach_geojson.get("features") or []
        coords = feats[0]["geometry"]["coordinates"] if feats else []
        if len(coords) < 2:
            return {}
        line = gpd.GeoSeries([LineString(coords)], crs=4326).to_crs(5070).iloc[0]

        w_bf = bankfull[0] if bankfull else geomorph.bankfull_geometry(da_sqkm or 1.0)[0]
        # Sample generously wide so there's terrain to extend the short bank into; the
        # thalweg is usually offset from the coarse flowline and one bank may rise much
        # farther than the other. ``balanced_profile`` recentres + trims for display.
        wide = min(max(8.0 * w_bf, 250.0), 800.0)
        buf4326 = gpd.GeoSeries([line.buffer(wide)], crs=5070).to_crs(4326).iloc[0]
        dem = py3dep.get_dem(buf4326, resolution=10).rio.reproject(5070)

        n_pts = int(2 * wide / spacing) + 1
        transects: list[tuple[list[float], list[float]]] = []
        for s in np.linspace(line.length * 0.15, line.length * 0.85, n_transects):
            p = line.interpolate(s)
            p2 = line.interpolate(min(s + 5.0, line.length))
            dx, dy = p2.x - p.x, p2.y - p.y
            norm = (dx * dx + dy * dy) ** 0.5 or 1.0
            nx, ny = -dy / norm, dx / norm  # unit perpendicular
            ts = np.linspace(-wide, wide, n_pts)
            zx = p.x + nx * ts
            zy = p.y + ny * ts
            z = dem.interp(x=xr.DataArray(zx, dims="t"),
                           y=xr.DataArray(zy, dims="t")).values
            z = np.asarray(z, dtype=float)
            ok = np.isfinite(z)
            if ok.sum() < 7:
                continue
            bal = geomorph.balanced_profile(ts[ok].tolist(), z[ok].tolist())
            if bal is not None:  # recentred on the thalweg, extended to the farther bank
                transects.append(bal)

        if not transects:
            return {}
        return geomorph.reach_summary(transects, da_sqkm or 1.0,
                                      bankfull=bankfull, division=division)
    except Exception:  # noqa: BLE001 - resilience by design
        return {}
