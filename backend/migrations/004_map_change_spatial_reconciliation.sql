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
    ), candidate_pairs AS (
        SELECT o.id AS old_id, n.id AS new_id,
               row_number() OVER (
                   PARTITION BY o.id
                   ORDER BY ST_Distance(
                       ST_Centroid(ST_Envelope(o.geometry))::geography,
                       ST_Centroid(ST_Envelope(n.geometry))::geography
                   ), n.logical_key
               ) AS old_rank,
               row_number() OVER (
                   PARTITION BY n.id
                   ORDER BY ST_Distance(
                       ST_Centroid(ST_Envelope(o.geometry))::geography,
                       ST_Centroid(ST_Envelope(n.geometry))::geography
                   ), o.logical_key
               ) AS new_rank
        FROM old_remaining o
        JOIN new_remaining n USING(base_key)
    ), spatial_pairs AS (
        SELECT old_id, new_id FROM candidate_pairs
        WHERE old_rank = 1 AND new_rank = 1
    ), paired AS (
        SELECT COALESCE(n.logical_key, o.logical_key) AS logical_key,
               o.id AS old_id, n.id AS new_id,
               o.identity_confidence AS old_confidence,
               n.identity_confidence AS new_confidence,
               o.geometry AS old_geometry, n.geometry AS new_geometry,
               o.properties AS old_properties, n.properties AS new_properties,
               o.layer_key AS old_layer_key, n.layer_key AS new_layer_key
        FROM spatial_pairs pair
        JOIN old_remaining o ON o.id = pair.old_id
        JOIN new_remaining n ON n.id = pair.new_id
        UNION ALL
        SELECT o.logical_key, o.id, NULL::uuid, o.identity_confidence, NULL::real,
               o.geometry, NULL::geometry, o.properties, NULL::jsonb,
               o.layer_key, NULL::text
        FROM old_remaining o
        WHERE NOT EXISTS (SELECT 1 FROM spatial_pairs p WHERE p.old_id = o.id)
        UNION ALL
        SELECT n.logical_key, NULL::uuid, n.id, NULL::real, n.identity_confidence,
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
