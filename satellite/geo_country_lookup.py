#!/usr/bin/env python3
"""
geo_country_lookup.py - assign a country name to a (latitude, longitude) point
using Natural Earth's 110m-resolution country polygons.

This is the standalone equivalent of the point-in-polygon lookup duplicated in
cmm-data-tool's eda_carbon_mapper.ipynb and eda_kayrros_methane_watch.ipynb,
which used geopandas + shapely to test each point against the same shapefile
(copied into data/naturalearth_country_shapefiles/ here - see that folder's
provenance note in satellite_plume_dataset.py). Neither geopandas nor shapely
is available in this project's environment, so this reimplements point-in-
polygon directly (even-odd ray casting, with holes) over the shapefile's
.geojson twin, using only numpy. Like the notebooks, a point is assigned to
the first country (in the file's row order) whose polygon contains it; a
point inside no polygon returns None.

Known limitation (inherited from the notebooks, not introduced here): the
110m set is Natural Earth's coarsest resolution, so coastal/near-border points
can fall just outside a simplified polygon and get no country at all.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

DEFAULT_GEOJSON = Path("data") / "naturalearth_country_shapefiles" / "ne_110m_admin_0_countries.geojson"


def _rings_from_geometry(geometry: dict) -> list[tuple[np.ndarray, list[np.ndarray]]]:
    """A geometry's list of (exterior_ring, [hole_rings]) pairs, as (lon, lat) arrays."""
    gtype = geometry["type"]
    if gtype == "Polygon":
        polygons = [geometry["coordinates"]]
    elif gtype == "MultiPolygon":
        polygons = geometry["coordinates"]
    else:
        raise ValueError(f"unsupported geometry type: {gtype}")

    out = []
    for poly in polygons:
        exterior = np.asarray(poly[0], dtype=float)
        holes = [np.asarray(ring, dtype=float) for ring in poly[1:]]
        out.append((exterior, holes))
    return out


class CountryLookup:
    """Loads Natural Earth country polygons once; reuse across many points."""

    def __init__(self, geojson_path: Path):
        with geojson_path.open(encoding="utf-8") as fh:
            gj = json.load(fh)

        self.names: list[str] = []
        self.polygons: list[list[tuple[np.ndarray, list[np.ndarray]]]] = []
        self.bbox: list[tuple[float, float, float, float]] = []  # lon_min, lon_max, lat_min, lat_max

        for feature in gj["features"]:
            name = feature["properties"]["NAME"]
            rings = _rings_from_geometry(feature["geometry"])
            all_pts = np.vstack([exterior for exterior, _holes in rings])
            self.names.append(name)
            self.polygons.append(rings)
            self.bbox.append(
                (all_pts[:, 0].min(), all_pts[:, 0].max(), all_pts[:, 1].min(), all_pts[:, 1].max())
            )

    @staticmethod
    def _points_in_ring(x: np.ndarray, y: np.ndarray, ring: np.ndarray) -> np.ndarray:
        """Even-odd ray-casting test, vectorised over all points at once."""
        rx, ry = ring[:, 0], ring[:, 1]
        inside = np.zeros(x.shape, dtype=bool)
        n = len(rx)
        j = n - 1
        for i in range(n):
            xi, yi = rx[i], ry[i]
            xj, yj = rx[j], ry[j]
            denom = yj - yi
            # Guard divide-by-zero on horizontal ring edges (contributes no crossing).
            with np.errstate(divide="ignore", invalid="ignore"):
                x_at_y = np.where(denom != 0, (xj - xi) * (y - yi) / np.where(denom == 0, 1, denom) + xi, np.inf)
            crosses = ((yi > y) != (yj > y)) & (x < x_at_y)
            inside ^= crosses
            j = i
        return inside

    def _points_in_polygons(self, x: np.ndarray, y: np.ndarray, rings) -> np.ndarray:
        inside = np.zeros(x.shape, dtype=bool)
        for exterior, holes in rings:
            in_ext = self._points_in_ring(x, y, exterior)
            for hole in holes:
                in_ext &= ~self._points_in_ring(x, y, hole)
            inside |= in_ext
        return inside

    def assign(self, latitude: np.ndarray, longitude: np.ndarray) -> list[str | None]:
        """First-match-wins country name per point, None if inside no polygon."""
        lat = np.asarray(latitude, dtype=float)
        lon = np.asarray(longitude, dtype=float)
        result: list[str | None] = [None] * len(lat)
        unresolved = np.ones(lat.shape, dtype=bool)

        for name, rings, (lon_min, lon_max, lat_min, lat_max) in zip(self.names, self.polygons, self.bbox):
            if not unresolved.any():
                break
            candidates = (
                unresolved
                & (lon >= lon_min)
                & (lon <= lon_max)
                & (lat >= lat_min)
                & (lat <= lat_max)
            )
            if not candidates.any():
                continue
            idx = np.where(candidates)[0]
            hits = self._points_in_polygons(lon[idx], lat[idx], rings)
            for i in idx[hits]:
                result[i] = name
            unresolved[idx[hits]] = False

        return result


def load_lookup(geojson_path: Path | None = None) -> CountryLookup:
    path = geojson_path or (Path(__file__).parent / DEFAULT_GEOJSON)
    return CountryLookup(path)
