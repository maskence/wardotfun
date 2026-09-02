CREATE FUNCTION map_change_area_styled_geometries(change_area uuid)
RETURNS TABLE(
    logical_key text,
    change_type text,
    phase text,
    geometry geometry,
    properties jsonb,
    paint jsonb
)
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    WITH area AS (
        SELECT a.id, a.observation_id, o.snapshot_id, o.previous_snapshot_id
        FROM map_change_areas a
        JOIN map_snapshot_observations o ON o.id = a.observation_id
        WHERE a.id = change_area
    ), members AS (
        SELECT mcf.*
        FROM area
        JOIN map_change_area_features member ON member.area_id = area.id
        JOIN map_change_features mcf
          ON mcf.observation_id = member.observation_id
         AND mcf.logical_key = member.logical_key
         AND mcf.change_type = member.change_type
    )
    SELECT delta.logical_key, delta.change_type, delta.phase, delta.geometry,
           CASE WHEN delta.phase = 'before'
                THEN COALESCE(old_feature.properties, '{}'::jsonb)
                ELSE COALESCE(new_feature.properties, '{}'::jsonb)
           END AS properties,
           CASE WHEN delta.phase = 'before'
                THEN COALESCE(old_layer.paint, '{}'::jsonb)
                ELSE COALESCE(new_layer.paint, '{}'::jsonb)
           END AS paint
    FROM area
    CROSS JOIN LATERAL map_change_area_geometries(area.id) delta
    JOIN members
      ON members.logical_key = delta.logical_key
     AND members.change_type = delta.change_type
    LEFT JOIN feature_versions old_feature ON old_feature.id = members.old_feature_version_id
    LEFT JOIN feature_versions new_feature ON new_feature.id = members.new_feature_version_id
    LEFT JOIN map_layer_versions old_layer
      ON old_layer.snapshot_id = area.previous_snapshot_id
     AND old_layer.layer_key = members.old_layer_key
    LEFT JOIN map_layer_versions new_layer
      ON new_layer.snapshot_id = area.snapshot_id
     AND new_layer.layer_key = members.new_layer_key
$$;
