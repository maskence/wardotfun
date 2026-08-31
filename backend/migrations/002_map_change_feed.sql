CREATE FUNCTION map_visible_properties(properties jsonb) RETURNS jsonb
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT COALESCE(jsonb_object_agg(lower(key), value ORDER BY lower(key)), '{}'::jsonb)
    FROM jsonb_each(COALESCE(properties, '{}'::jsonb))
    WHERE lower(key) = ANY(ARRAY[
        'name', 'title', 'label', 'description',
        'fill_color', 'fill_opacity', 'line_color', 'line_opacity', 'line_width',
        'circle_color', 'circle_opacity', 'circle_radius',
        'circle_stroke_color', 'circle_stroke_width'
    ])
$$;

CREATE FUNCTION map_snapshot_feature_diff(old_snapshot uuid, new_snapshot uuid)
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
        SELECT fv.logical_key, fv.id, fv.identity_confidence, fv.content_hash, fv.geometry,
               fv.properties, mlv.layer_key
        FROM snapshot_features sf
        JOIN feature_versions fv ON fv.id = sf.feature_version_id
        JOIN map_layer_versions mlv ON mlv.id = sf.layer_version_id
        WHERE sf.snapshot_id = old_snapshot
    ), new_features AS (
        SELECT fv.logical_key, fv.id, fv.identity_confidence, fv.content_hash, fv.geometry,
               fv.properties, mlv.layer_key
        FROM snapshot_features sf
        JOIN feature_versions fv ON fv.id = sf.feature_version_id
        JOIN map_layer_versions mlv ON mlv.id = sf.layer_version_id
        WHERE sf.snapshot_id = new_snapshot
    ), changed AS (
        SELECT COALESCE(n.logical_key, o.logical_key) AS logical_key,
               o.id AS old_id, n.id AS new_id,
               o.identity_confidence AS old_confidence,
               n.identity_confidence AS new_confidence,
               o.geometry AS old_geometry, n.geometry AS new_geometry,
               o.layer_key AS old_layer_key, n.layer_key AS new_layer_key,
               CASE
                   WHEN o.id IS NULL THEN ARRAY['added']::text[]
                   WHEN n.id IS NULL THEN ARRAY['removed']::text[]
                   WHEN LEAST(o.identity_confidence, n.identity_confidence) >= 0.85
                       THEN ARRAY['modified']::text[]
                   ELSE ARRAY['removed', 'added']::text[]
               END AS change_types
        FROM old_features o
        FULL JOIN new_features n USING(logical_key)
        WHERE o.id IS NULL OR n.id IS NULL
           OR (o.content_hash IS DISTINCT FROM n.content_hash AND (
                  NOT ST_Equals(o.geometry, n.geometry)
                  OR map_visible_properties(o.properties) IS DISTINCT FROM map_visible_properties(n.properties)
              ))
           OR o.layer_key IS DISTINCT FROM n.layer_key
    )
    SELECT c.logical_key, kind,
           CASE WHEN kind IN ('removed', 'modified') THEN c.old_id END,
           CASE WHEN kind IN ('added', 'modified') THEN c.new_id END,
           c.old_layer_key, c.new_layer_key,
           COALESCE(LEAST(c.old_confidence, c.new_confidence), c.old_confidence,
                    c.new_confidence, 0)::real,
           ST_Envelope(CASE
               WHEN kind = 'added' THEN c.new_geometry
               WHEN kind = 'removed' THEN c.old_geometry
               ELSE ST_Collect(c.old_geometry, c.new_geometry)
           END)
    FROM changed c
    CROSS JOIN LATERAL unnest(c.change_types) AS kind
$$;

CREATE TABLE map_snapshot_observations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id text NOT NULL REFERENCES map_sources(id),
    snapshot_id uuid NOT NULL REFERENCES map_snapshots(id),
    previous_snapshot_id uuid REFERENCES map_snapshots(id),
    ingest_run_id bigint UNIQUE REFERENCES ingest_runs(id),
    observed_at timestamptz NOT NULL,
    calendar_date date NOT NULL,
    is_baseline boolean NOT NULL DEFAULT false,
    added_count integer NOT NULL DEFAULT 0,
    removed_count integer NOT NULL DEFAULT 0,
    modified_count integer NOT NULL DEFAULT 0,
    style_count integer NOT NULL DEFAULT 0,
    bounds geometry(Geometry, 4326),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(source_id, observed_at, snapshot_id)
);

CREATE INDEX map_snapshot_observations_source_time_idx
    ON map_snapshot_observations(source_id, observed_at DESC, id DESC);
CREATE INDEX map_snapshot_observations_date_idx
    ON map_snapshot_observations(calendar_date, observed_at DESC, id DESC);
CREATE INDEX map_snapshot_observations_snapshot_idx
    ON map_snapshot_observations(snapshot_id);
CREATE INDEX map_snapshot_observations_bounds_gist
    ON map_snapshot_observations USING gist(bounds);

CREATE TABLE map_change_features (
    observation_id uuid NOT NULL REFERENCES map_snapshot_observations(id),
    logical_key text NOT NULL,
    change_type text NOT NULL CHECK (change_type IN ('added', 'removed', 'modified')),
    old_feature_version_id uuid REFERENCES feature_versions(id),
    new_feature_version_id uuid REFERENCES feature_versions(id),
    old_layer_key text,
    new_layer_key text,
    identity_confidence real NOT NULL CHECK (identity_confidence >= 0 AND identity_confidence <= 1),
    bounds geometry(Geometry, 4326) NOT NULL,
    PRIMARY KEY(observation_id, logical_key, change_type)
);

CREATE INDEX map_change_features_observation_idx
    ON map_change_features(observation_id, change_type);
CREATE INDEX map_change_features_old_idx ON map_change_features(old_feature_version_id);
CREATE INDEX map_change_features_new_idx ON map_change_features(new_feature_version_id);
CREATE INDEX map_change_features_bounds_gist ON map_change_features USING gist(bounds);

CREATE TABLE map_change_areas (
    id uuid PRIMARY KEY,
    observation_id uuid NOT NULL REFERENCES map_snapshot_observations(id),
    ordinal integer NOT NULL,
    bounds geometry(Geometry, 4326) NOT NULL,
    added_count integer NOT NULL DEFAULT 0,
    removed_count integer NOT NULL DEFAULT 0,
    modified_count integer NOT NULL DEFAULT 0,
    style_count integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(observation_id, ordinal)
);

CREATE INDEX map_change_areas_observation_idx ON map_change_areas(observation_id, ordinal);
CREATE INDEX map_change_areas_bounds_gist ON map_change_areas USING gist(bounds);

CREATE TABLE map_change_area_features (
    area_id uuid NOT NULL REFERENCES map_change_areas(id),
    observation_id uuid NOT NULL,
    logical_key text NOT NULL,
    change_type text NOT NULL,
    PRIMARY KEY(area_id, logical_key, change_type),
    FOREIGN KEY(observation_id, logical_key, change_type)
        REFERENCES map_change_features(observation_id, logical_key, change_type)
);

CREATE INDEX map_change_area_features_change_idx
    ON map_change_area_features(observation_id, logical_key, change_type);

CREATE TRIGGER map_snapshot_observations_immutable
    BEFORE UPDATE OR DELETE ON map_snapshot_observations
    FOR EACH ROW EXECUTE FUNCTION reject_temporal_mutation();
CREATE TRIGGER map_change_features_immutable
    BEFORE UPDATE OR DELETE ON map_change_features
    FOR EACH ROW EXECUTE FUNCTION reject_temporal_mutation();
CREATE TRIGGER map_change_areas_immutable
    BEFORE UPDATE OR DELETE ON map_change_areas
    FOR EACH ROW EXECUTE FUNCTION reject_temporal_mutation();
CREATE TRIGGER map_change_area_features_immutable
    BEFORE UPDATE OR DELETE ON map_change_area_features
    FOR EACH ROW EXECUTE FUNCTION reject_temporal_mutation();
