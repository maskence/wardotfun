# Temporal map API

## `GET /api/map-state?date=YYYYMMDD`

The manifest is small, uses `Europe/Kyiv` calendar boundaries, and is the only
resource the browser polls every 30 seconds. Omitting `date` selects today.

Important response fields:

- `date` and `available_dates` drive the one global calendar.
- `mappers[]` and `fortifications` contain the snapshot selected as of the end
  of that date, layer paint metadata, freshness, and an immutable `tile_url`.
- `available: false` means the date predates that mapper's archive. It remains
  selectable when GeoConfirmed has data.
- `vector_tiles_enabled` is false until the production flag is enabled. The
  frontend then uses compatibility GeoJSON routes.

Each layer advertises `source_layer: "features"`. MVT features also contain
`layer_key`, `logical_key`, `identity_confidence`, and the original one-level
properties used by MapLibre paint expressions.

## `GET /api/map-tiles/{source}/{snapshot}/{z}/{x}/{y}.pbf`

The endpoint validates that the immutable snapshot belongs to the source. It
clips to the tile envelope with extent 4096 and buffer 64. Below zoom 12 it
uses topology-preserving half-pixel simplification in Web Mercator.

Successful responses use `application/vnd.mapbox-vector-tile`, a stable ETag,
and `Cache-Control: public, max-age=31536000, immutable`. A matching
`If-None-Match` returns 304.

## Compatibility routes

`/api/mapper-overlay` and `/api/fortifications` remain during the first week
after cutover. They now have ETags and gzip support. Remove them, their frontend
fallback, and tracked pickle caches only after that observation period passes.
