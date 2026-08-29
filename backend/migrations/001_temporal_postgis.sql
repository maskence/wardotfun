CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE map_sources (
    id text PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('mapper', 'fortifications')),
    display_name text NOT NULL,
    source_url text,
    attribution text NOT NULL DEFAULT '',
    upstream_type text NOT NULL,
    upstream_config jsonb NOT NULL DEFAULT '{}'::jsonb,
    refresh_policy jsonb NOT NULL DEFAULT '{}'::jsonb,
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE ingest_runs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id text NOT NULL REFERENCES map_sources(id),
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status text NOT NULL CHECK (status IN ('running', 'stored', 'unchanged', 'not_modified', 'failed', 'locked')),
    http_etag text,
    last_modified text,
    raw_content_hash text,
    raw_path text,
    normalized_hash text,
    snapshot_id uuid,
    feature_count integer,
    error text
);

CREATE INDEX ingest_runs_source_started_idx
    ON ingest_runs(source_id, started_at DESC);
CREATE INDEX ingest_runs_failed_idx
    ON ingest_runs(started_at DESC) WHERE status = 'failed';

CREATE TABLE raw_archives (
    sha256 text PRIMARY KEY,
    path text NOT NULL UNIQUE,
    content_type text,
    byte_count bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE ingest_run_raw_archives (
    ingest_run_id bigint NOT NULL REFERENCES ingest_runs(id) ON DELETE CASCADE,
    raw_sha256 text NOT NULL REFERENCES raw_archives(sha256),
    upstream_url text NOT NULL,
    PRIMARY KEY (ingest_run_id, raw_sha256, upstream_url)
);

CREATE TABLE map_snapshots (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id text NOT NULL REFERENCES map_sources(id),
    ingest_run_id bigint REFERENCES ingest_runs(id),
    captured_at timestamptz NOT NULL,
    calendar_date date NOT NULL,
    content_hash text NOT NULL,
    feature_count integer NOT NULL,
    bounds geometry(Geometry, 4326),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_id, content_hash)
);

CREATE INDEX map_snapshots_source_captured_idx
    ON map_snapshots(source_id, captured_at DESC);
CREATE INDEX map_snapshots_source_date_idx
    ON map_snapshots(source_id, calendar_date, captured_at DESC);
CREATE INDEX map_snapshots_date_idx ON map_snapshots(calendar_date);
CREATE INDEX map_snapshots_bounds_gist ON map_snapshots USING gist(bounds);

CREATE TABLE map_layer_versions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    snapshot_id uuid NOT NULL REFERENCES map_snapshots(id),
    layer_key text NOT NULL,
    label text NOT NULL,
    geometry_type text NOT NULL CHECK (geometry_type IN ('polygon', 'line', 'point')),
    paint jsonb NOT NULL DEFAULT '{}'::jsonb,
    feature_count integer NOT NULL,
    ordinal integer NOT NULL DEFAULT 0,
    UNIQUE(snapshot_id, layer_key)
);

CREATE INDEX map_layer_versions_snapshot_idx ON map_layer_versions(snapshot_id, ordinal);

CREATE TABLE feature_versions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id text NOT NULL REFERENCES map_sources(id),
    logical_key text NOT NULL,
    identity_confidence real NOT NULL CHECK (identity_confidence >= 0 AND identity_confidence <= 1),
    content_hash text NOT NULL,
    geometry geometry(Geometry, 4326) NOT NULL,
    properties jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(source_id, logical_key, content_hash)
);

CREATE INDEX feature_versions_geometry_gist ON feature_versions USING gist(geometry);
CREATE INDEX feature_versions_source_key_idx ON feature_versions(source_id, logical_key);
CREATE INDEX feature_versions_source_hash_idx ON feature_versions(source_id, content_hash);

CREATE TABLE snapshot_features (
    snapshot_id uuid NOT NULL REFERENCES map_snapshots(id),
    layer_version_id uuid NOT NULL REFERENCES map_layer_versions(id),
    feature_version_id uuid NOT NULL REFERENCES feature_versions(id),
    PRIMARY KEY(snapshot_id, layer_version_id, feature_version_id)
);

CREATE INDEX snapshot_features_layer_idx ON snapshot_features(layer_version_id);
CREATE INDEX snapshot_features_feature_idx ON snapshot_features(feature_version_id);

CREATE TABLE geolocation_metadata (
    key text PRIMARY KEY,
    value text NOT NULL
);

CREATE TABLE geolocation_icons (
    id text PRIMARY KEY,
    name text,
    upstream_path text,
    local_name text,
    content_type text,
    updated_at timestamptz
);

CREATE TABLE geolocation_events (
    uuid text PRIMARY KEY,
    event_date date NOT NULL,
    timestamp_text text NOT NULL,
    occurred_at timestamptz,
    time_precision text NOT NULL CHECK (time_precision IN ('day', 'minute')),
    location geometry(Point, 4326) NOT NULL,
    description text NOT NULL DEFAULT '',
    faction_id text NOT NULL DEFAULT '',
    faction_name text NOT NULL DEFAULT 'Unknown',
    faction_color text NOT NULL DEFAULT '#666666',
    icon_id text REFERENCES geolocation_icons(id),
    icon_name text,
    icon_path text,
    origin text NOT NULL DEFAULT '',
    equipment text NOT NULL DEFAULT '',
    units text NOT NULL DEFAULT '',
    plus_code text NOT NULL DEFAULT '',
    evidence_links jsonb NOT NULL DEFAULT '[]'::jsonb,
    geolocation_links jsonb NOT NULL DEFAULT '[]'::jsonb,
    gear_items jsonb NOT NULL DEFAULT '[]'::jsonb,
    orbat_units jsonb NOT NULL DEFAULT '[]'::jsonb,
    source_hash text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX geolocation_events_date_idx ON geolocation_events(event_date);
CREATE INDEX geolocation_events_location_gist ON geolocation_events USING gist(location);
CREATE INDEX geolocation_events_faction_idx ON geolocation_events(faction_id);
CREATE INDEX geolocation_events_icon_idx ON geolocation_events(icon_id);
CREATE INDEX geolocation_events_origin_idx ON geolocation_events(origin);

-- Snapshot state is append-only. Membership and feature rows referenced by a
-- snapshot are immutable as well, guaranteeing stable tile URLs forever.
CREATE FUNCTION reject_temporal_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is immutable', TG_TABLE_NAME;
END;
$$;

CREATE TRIGGER map_snapshots_immutable
    BEFORE UPDATE OR DELETE ON map_snapshots
    FOR EACH ROW EXECUTE FUNCTION reject_temporal_mutation();
CREATE TRIGGER map_layer_versions_immutable
    BEFORE UPDATE OR DELETE ON map_layer_versions
    FOR EACH ROW EXECUTE FUNCTION reject_temporal_mutation();
CREATE TRIGGER feature_versions_immutable
    BEFORE UPDATE OR DELETE ON feature_versions
    FOR EACH ROW EXECUTE FUNCTION reject_temporal_mutation();
CREATE TRIGGER snapshot_features_immutable
    BEFORE UPDATE OR DELETE ON snapshot_features
    FOR EACH ROW EXECUTE FUNCTION reject_temporal_mutation();
