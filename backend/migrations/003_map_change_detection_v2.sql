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
    ), paired AS (
        SELECT COALESCE(n.logical_key, o.logical_key) AS logical_key,
               o.id AS old_id, n.id AS new_id,
               o.identity_confidence AS old_confidence,
               n.identity_confidence AS new_confidence,
               o.geometry AS old_geometry, n.geometry AS new_geometry,
               o.properties AS old_properties, n.properties AS new_properties,
               o.layer_key AS old_layer_key, n.layer_key AS new_layer_key
        FROM old_remaining o
        FULL JOIN new_remaining n USING(logical_key)
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
                       THEN ST_SymDifference(ST_MakeValid(old_geometry), ST_MakeValid(new_geometry))
                   ELSE new_geometry
               END AS footprint
        FROM changed
    )
    SELECT logical_key, kind, old_id, new_id, old_layer_key, new_layer_key,
           COALESCE(LEAST(old_confidence, new_confidence), old_confidence,
                    new_confidence, 0)::real,
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
        SELECT logical_key, change_type, 'before',
               ST_Intersection(
                   ST_Difference(ST_MakeValid(old_geometry), ST_MakeValid(new_geometry)),
                   clip
               )
        FROM changed
        WHERE change_type = 'modified' AND NOT ST_Equals(old_geometry, new_geometry)
        UNION ALL
        SELECT logical_key, change_type, 'after',
               ST_Intersection(
                   ST_Difference(ST_MakeValid(new_geometry), ST_MakeValid(old_geometry)),
                   clip
               )
        FROM changed
        WHERE change_type = 'modified' AND NOT ST_Equals(old_geometry, new_geometry)
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
