CREATE FUNCTION map_material_change_delta(
    old_geometry geometry,
    new_geometry geometry,
    delta_phase text
)
RETURNS SETOF geometry
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    WITH raw AS (
        SELECT CASE delta_phase
            WHEN 'before' THEN ST_Difference(ST_MakeValid(old_geometry), ST_MakeValid(new_geometry))
            WHEN 'after' THEN ST_Difference(ST_MakeValid(new_geometry), ST_MakeValid(old_geometry))
            ELSE NULL::geometry
        END AS geometry
    ), components AS (
        SELECT dumped.geom AS geometry
        FROM raw
        CROSS JOIN LATERAL ST_Dump(raw.geometry) dumped
        WHERE raw.geometry IS NOT NULL AND NOT ST_IsEmpty(raw.geometry)
    ), measured AS (
        SELECT geometry,
               CASE WHEN ST_Dimension(geometry) = 2
                    THEN ST_Area(geometry::geography) END AS area_m2,
               CASE WHEN ST_Dimension(geometry) = 2
                    THEN ST_Perimeter(geometry::geography) END AS perimeter_m,
               CASE WHEN ST_Dimension(geometry) = 2 THEN
                    (ST_MaximumInscribedCircle(ST_Transform(geometry, 3857))).radius
                    * cos(radians(ST_Y(ST_PointOnSurface(geometry))))
               END AS inradius_m
        FROM components
        WHERE NOT ST_IsEmpty(geometry)
    )
    SELECT geometry
    FROM measured
    WHERE ST_Dimension(geometry) <> 2
       OR NOT (
           area_m2 < 500000
           AND inradius_m < 50
           AND 4 * pi() * area_m2 / NULLIF(perimeter_m * perimeter_m, 0) < 0.01
       )
$$;

CREATE FUNCTION map_material_change_footprint(old_geometry geometry, new_geometry geometry)
RETURNS geometry
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT ST_Collect(component)
    FROM (
        SELECT map_material_change_delta(old_geometry, new_geometry, 'before') AS component
        UNION ALL
        SELECT map_material_change_delta(old_geometry, new_geometry, 'after') AS component
    ) material
$$;

CREATE OR REPLACE FUNCTION map_snapshot_feature_diff(old_snapshot uuid, new_snapshot uuid)
RETURNS TABLE(
    logical_key text,
    change_type text,
    old_feature_version_id uuid,
    new_feature_version_id uuid,
    old_layer_key text,
    new_layer_key text,
    identity_confidence real,
    bounds geometry
)
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    WITH old_features AS (
        SELECT fv.logical_key, split_part(fv.logical_key, '#', 1) AS base_key,
               fv.id, fv.identity_confidence, fv.content_hash, fv.geometry,
               fv.properties, mlv.layer_key,
               row_number() OVER (
                   PARTITION BY split_part(fv.logical_key, '#', 1), fv.content_hash
                   ORDER BY fv.logical_key
               ) AS content_ordinal
        FROM snapshot_features sf
        JOIN feature_versions fv ON fv.id = sf.feature_version_id
        JOIN map_layer_versions mlv ON mlv.id = sf.layer_version_id
        WHERE sf.snapshot_id = old_snapshot
    ), new_features AS (
        SELECT fv.logical_key, split_part(fv.logical_key, '#', 1) AS base_key,
               fv.id, fv.identity_confidence, fv.content_hash, fv.geometry,
               fv.properties, mlv.layer_key,
               row_number() OVER (
                   PARTITION BY split_part(fv.logical_key, '#', 1), fv.content_hash
                   ORDER BY fv.logical_key
               ) AS content_ordinal
        FROM snapshot_features sf
        JOIN feature_versions fv ON fv.id = sf.feature_version_id
        JOIN map_layer_versions mlv ON mlv.id = sf.layer_version_id
        WHERE sf.snapshot_id = new_snapshot
    ), exact_pairs AS (
        SELECT o.id AS old_id, n.id AS new_id
        FROM old_features o
        JOIN new_features n
          ON n.base_key = o.base_key
         AND n.content_hash = o.content_hash
         AND n.content_ordinal = o.content_ordinal
         AND n.layer_key = o.layer_key
    ), old_remaining AS (
        SELECT o.* FROM old_features o
        WHERE NOT EXISTS (SELECT 1 FROM exact_pairs p WHERE p.old_id = o.id)
    ), new_remaining AS (
        SELECT n.* FROM new_features n
        WHERE NOT EXISTS (SELECT 1 FROM exact_pairs p WHERE p.new_id = n.id)
    ), base_candidates AS (
        SELECT o.id AS old_id, n.id AS new_id, 0 AS priority,
               0::double precision AS overlap_ratio,
               ST_Distance(
                   ST_Centroid(ST_Envelope(o.geometry))::geography,
                   ST_Centroid(ST_Envelope(n.geometry))::geography
               ) AS distance_m
        FROM old_remaining o
        JOIN new_remaining n USING(base_key)
    ), cross_id_metrics AS (
        SELECT o.id AS old_id, n.id AS new_id,
               ST_Area(ST_Intersection(shape.old_polygon, shape.new_polygon)::geography)
               / NULLIF(ST_Area(ST_Union(shape.old_polygon, shape.new_polygon)::geography), 0)
                   AS overlap_ratio,
               ST_Distance(
                   ST_Centroid(ST_Envelope(o.geometry))::geography,
                   ST_Centroid(ST_Envelope(n.geometry))::geography
               ) AS distance_m
        FROM old_remaining o
        JOIN new_remaining n
          ON n.base_key <> o.base_key
         AND n.layer_key = o.layer_key
         AND map_visible_properties(n.properties) = map_visible_properties(o.properties)
        CROSS JOIN LATERAL (
            SELECT ST_CollectionExtract(ST_MakeValid(o.geometry), 3) AS old_polygon,
                   ST_CollectionExtract(ST_MakeValid(n.geometry), 3) AS new_polygon
        ) shape
        WHERE NOT ST_IsEmpty(shape.old_polygon)
          AND NOT ST_IsEmpty(shape.new_polygon)
          AND shape.old_polygon && shape.new_polygon
    ), pair_candidates AS (
        SELECT * FROM base_candidates
        UNION ALL
        SELECT old_id, new_id, 1 AS priority, overlap_ratio, distance_m
        FROM cross_id_metrics
        WHERE overlap_ratio >= 0.90
    ), ranked_candidates AS (
        SELECT *,
               row_number() OVER (
                   PARTITION BY old_id
                   ORDER BY priority, overlap_ratio DESC, distance_m, new_id
               ) AS old_rank,
               row_number() OVER (
                   PARTITION BY new_id
                   ORDER BY priority, overlap_ratio DESC, distance_m, old_id
               ) AS new_rank
        FROM pair_candidates
    ), spatial_pairs AS (
        SELECT old_id, new_id, priority
        FROM ranked_candidates
        WHERE old_rank = 1 AND new_rank = 1
    ), paired AS (
        SELECT n.logical_key,
               o.id AS old_id, n.id AS new_id,
               CASE WHEN pair.priority = 1
                    THEN LEAST(o.identity_confidence, n.identity_confidence, 0.90)
                    ELSE LEAST(o.identity_confidence, n.identity_confidence)
               END AS pair_confidence,
               o.identity_confidence AS old_confidence,
               n.identity_confidence AS new_confidence,
               o.geometry AS old_geometry, n.geometry AS new_geometry,
               o.properties AS old_properties, n.properties AS new_properties,
               o.layer_key AS old_layer_key, n.layer_key AS new_layer_key
        FROM spatial_pairs pair
        JOIN old_remaining o ON o.id = pair.old_id
        JOIN new_remaining n ON n.id = pair.new_id
        UNION ALL
        SELECT o.logical_key, o.id, NULL::uuid, NULL::real,
               o.identity_confidence, NULL::real,
               o.geometry, NULL::geometry, o.properties, NULL::jsonb,
               o.layer_key, NULL::text
        FROM old_remaining o
        WHERE NOT EXISTS (SELECT 1 FROM spatial_pairs p WHERE p.old_id = o.id)
        UNION ALL
        SELECT n.logical_key, NULL::uuid, n.id, NULL::real,
               NULL::real, n.identity_confidence,
               NULL::geometry, n.geometry, NULL::jsonb, n.properties,
               NULL::text, n.layer_key
        FROM new_remaining n
        WHERE NOT EXISTS (SELECT 1 FROM spatial_pairs p WHERE p.new_id = n.id)
    ), changed AS (
        SELECT *,
               CASE
                   WHEN old_id IS NULL THEN 'added'
                   WHEN new_id IS NULL THEN 'removed'
                   ELSE 'modified'
               END AS kind
        FROM paired
        WHERE old_id IS NULL OR new_id IS NULL
           OR NOT ST_Equals(old_geometry, new_geometry)
           OR map_visible_properties(old_properties)
                IS DISTINCT FROM map_visible_properties(new_properties)
           OR old_layer_key IS DISTINCT FROM new_layer_key
    ), footprints AS (
        SELECT *,
               CASE
                   WHEN kind = 'added' THEN new_geometry
                   WHEN kind = 'removed' THEN old_geometry
                   WHEN NOT ST_Equals(old_geometry, new_geometry)
                       THEN map_material_change_footprint(old_geometry, new_geometry)
                   ELSE new_geometry
               END AS footprint
        FROM changed
    )
    SELECT logical_key, kind, old_id, new_id, old_layer_key, new_layer_key,
           COALESCE(pair_confidence, LEAST(old_confidence, new_confidence),
                    old_confidence, new_confidence, 0)::real,
           footprint
    FROM footprints
    WHERE footprint IS NOT NULL AND NOT ST_IsEmpty(footprint)
$$;

CREATE OR REPLACE FUNCTION map_change_area_geometries(change_area uuid)
RETURNS TABLE(logical_key text, change_type text, phase text, geometry geometry)
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    WITH area AS (
        SELECT id, observation_id,
               ST_Expand(
                   bounds,
                   GREATEST(
                       ST_XMax(box3d(bounds)) - ST_XMin(box3d(bounds)),
                       ST_YMax(box3d(bounds)) - ST_YMin(box3d(bounds))
                   ) * 0.15 + 0.001
               ) AS clip
        FROM map_change_areas WHERE id = change_area
    ), changed AS (
        SELECT mcf.logical_key, mcf.change_type,
               old.geometry AS old_geometry, new.geometry AS new_geometry,
               area.clip
        FROM area
        JOIN map_change_area_features member ON member.area_id = area.id
        JOIN map_change_features mcf
          ON mcf.observation_id = member.observation_id
         AND mcf.logical_key = member.logical_key
         AND mcf.change_type = member.change_type
        LEFT JOIN feature_versions old ON old.id = mcf.old_feature_version_id
        LEFT JOIN feature_versions new ON new.id = mcf.new_feature_version_id
    ), semantic AS (
        SELECT logical_key, change_type, 'after'::text AS phase,
               ST_Intersection(new_geometry, clip) AS geometry
        FROM changed WHERE change_type = 'added'
        UNION ALL
        SELECT logical_key, change_type, 'before',
               ST_Intersection(old_geometry, clip)
        FROM changed WHERE change_type = 'removed'
        UNION ALL
        SELECT changed.logical_key, changed.change_type, 'before',
               ST_Intersection(delta.geometry, changed.clip)
        FROM changed
        CROSS JOIN LATERAL map_material_change_delta(
            changed.old_geometry, changed.new_geometry, 'before'
        ) delta
        WHERE changed.change_type = 'modified'
          AND NOT ST_Equals(changed.old_geometry, changed.new_geometry)
        UNION ALL
        SELECT changed.logical_key, changed.change_type, 'after',
               ST_Intersection(delta.geometry, changed.clip)
        FROM changed
        CROSS JOIN LATERAL map_material_change_delta(
            changed.old_geometry, changed.new_geometry, 'after'
        ) delta
        WHERE changed.change_type = 'modified'
          AND NOT ST_Equals(changed.old_geometry, changed.new_geometry)
        UNION ALL
        SELECT logical_key, change_type, 'style',
               ST_Intersection(new_geometry, clip)
        FROM changed
        WHERE change_type = 'modified' AND ST_Equals(old_geometry, new_geometry)
    )
    SELECT logical_key, change_type, phase, geometry
    FROM semantic
    WHERE geometry IS NOT NULL AND NOT ST_IsEmpty(geometry)
$$;
