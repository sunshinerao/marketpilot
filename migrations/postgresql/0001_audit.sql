BEGIN;

CREATE SCHEMA IF NOT EXISTS marketpilot;

CREATE TABLE IF NOT EXISTS marketpilot.audit_schema (
    key text PRIMARY KEY,
    value text NOT NULL
);

INSERT INTO marketpilot.audit_schema(key, value)
VALUES ('schema_version', '1')
ON CONFLICT (key) DO NOTHING;

CREATE TABLE IF NOT EXISTS marketpilot.decision_runs (
    run_id text PRIMARY KEY,
    recorded_at timestamptz NOT NULL,
    payload_json jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS marketpilot.alerts (
    alert_id text PRIMARY KEY,
    created_at timestamptz NOT NULL,
    payload_json jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS marketpilot.alert_feedback (
    feedback_id text PRIMARY KEY,
    alert_id text NOT NULL REFERENCES marketpilot.alerts(alert_id),
    recorded_at timestamptz NOT NULL,
    payload_json jsonb NOT NULL
);

CREATE INDEX IF NOT EXISTS alert_feedback_alert_idx
    ON marketpilot.alert_feedback(alert_id, recorded_at);

CREATE TABLE IF NOT EXISTS marketpilot.point_in_time_records (
    record_id text PRIMARY KEY,
    first_seen_at timestamptz NOT NULL,
    payload_json jsonb NOT NULL,
    CONSTRAINT pit_metadata_has_no_canonical_content CHECK (
        jsonb_typeof(payload_json) = 'object'
        AND (payload_json - ARRAY[
            'record_id', 'logical_key', 'published_at', 'first_seen_at', 'provider',
            'provider_version', 'schema_version', 'content_hash'
        ]) = '{}'::jsonb
        AND NOT (payload_json ? 'canonical_content')
    )
);

CREATE TABLE IF NOT EXISTS marketpilot.replay_manifests (
    manifest_hash text PRIMARY KEY,
    as_of timestamptz NOT NULL,
    payload_json jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS marketpilot.recovery_checkpoints (
    checkpoint_id text PRIMARY KEY,
    captured_at timestamptz NOT NULL,
    payload_json jsonb NOT NULL
);

-- Safe metadata only. Raw licensed plaintext, ciphertext, nonce and wrapped data keys
-- belong exclusively in the access-controlled encrypted landing store.
CREATE TABLE IF NOT EXISTS marketpilot.raw_landing_receipts (
    landing_id text PRIMARY KEY,
    first_seen_at timestamptz NOT NULL,
    payload_json jsonb NOT NULL,
    CONSTRAINT raw_receipt_forbids_payload_fields CHECK (
        jsonb_typeof(payload_json) = 'object'
        AND (payload_json - ARRAY[
            'landing_id', 'object_key', 'provider', 'dataset', 'logical_key_hash',
            'published_at', 'first_seen_at', 'plaintext_sha256', 'key_id',
            'algorithm', 'content_type'
        ]) = '{}'::jsonb
        AND
        NOT (payload_json ?| ARRAY[
            'payload', 'plaintext', 'ciphertext', 'nonce', 'canonical_content', 'wrapped_key'
        ])
    )
);

CREATE TABLE IF NOT EXISTS marketpilot.alert_stream_events (
    sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    projection_key text NOT NULL UNIQUE,
    recorded_at timestamptz NOT NULL,
    payload_json jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS marketpilot.stream_deliveries (
    delivery_id text PRIMARY KEY,
    stream_event_id bigint NOT NULL REFERENCES marketpilot.alert_stream_events(sequence),
    attempted_at timestamptz NOT NULL,
    payload_json jsonb NOT NULL
);

CREATE INDEX IF NOT EXISTS stream_delivery_event_idx
    ON marketpilot.stream_deliveries(stream_event_id, attempted_at);

CREATE TABLE IF NOT EXISTS marketpilot.attribution_tasks (
    task_id text PRIMARY KEY,
    signal_id text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL,
    payload_json jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS marketpilot.attribution_reviews (
    review_id text PRIMARY KEY,
    task_id text NOT NULL REFERENCES marketpilot.attribution_tasks(task_id),
    reviewed_at timestamptz NOT NULL,
    payload_json jsonb NOT NULL
);

CREATE INDEX IF NOT EXISTS attribution_review_task_idx
    ON marketpilot.attribution_reviews(task_id, reviewed_at);

CREATE TABLE IF NOT EXISTS marketpilot.governance_model_versions (
    model_id text NOT NULL,
    version text NOT NULL,
    parent_version text,
    trained_at timestamptz NOT NULL,
    payload_json jsonb NOT NULL,
    PRIMARY KEY (model_id, version),
    FOREIGN KEY (model_id, parent_version)
        REFERENCES marketpilot.governance_model_versions(model_id, version)
);

CREATE TABLE IF NOT EXISTS marketpilot.governance_approvals (
    approval_id text PRIMARY KEY,
    model_id text NOT NULL,
    approved_at timestamptz NOT NULL,
    payload_json jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS marketpilot.governance_events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    approval_id text NOT NULL UNIQUE
        REFERENCES marketpilot.governance_approvals(approval_id),
    model_id text NOT NULL,
    source_version text,
    target_version text NOT NULL,
    occurred_at timestamptz NOT NULL,
    payload_json jsonb NOT NULL,
    FOREIGN KEY (model_id, source_version)
        REFERENCES marketpilot.governance_model_versions(model_id, version),
    FOREIGN KEY (model_id, target_version)
        REFERENCES marketpilot.governance_model_versions(model_id, version)
);

CREATE INDEX IF NOT EXISTS governance_event_model_idx
    ON marketpilot.governance_events(model_id, event_id DESC);

CREATE TABLE IF NOT EXISTS marketpilot.governance_session_freezes (
    model_id text NOT NULL,
    session_id text NOT NULL,
    version text NOT NULL,
    frozen_at timestamptz NOT NULL,
    payload_json jsonb NOT NULL,
    PRIMARY KEY (model_id, session_id),
    FOREIGN KEY (model_id, version)
        REFERENCES marketpilot.governance_model_versions(model_id, version)
);

CREATE OR REPLACE FUNCTION marketpilot.deny_audit_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'append-only audit table' USING ERRCODE = '55000';
END;
$$;

DO $$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'decision_runs',
        'alerts',
        'alert_feedback',
        'point_in_time_records',
        'replay_manifests',
        'recovery_checkpoints',
        'raw_landing_receipts',
        'alert_stream_events',
        'stream_deliveries',
        'attribution_tasks',
        'attribution_reviews',
        'governance_model_versions',
        'governance_approvals',
        'governance_events',
        'governance_session_freezes'
    ]
    LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS deny_update ON marketpilot.%I', table_name);
        EXECUTE format('DROP TRIGGER IF EXISTS deny_delete ON marketpilot.%I', table_name);
        EXECUTE format(
            'CREATE TRIGGER deny_update BEFORE UPDATE ON marketpilot.%I '
            'FOR EACH ROW EXECUTE FUNCTION marketpilot.deny_audit_mutation()',
            table_name
        );
        EXECUTE format(
            'CREATE TRIGGER deny_delete BEFORE DELETE ON marketpilot.%I '
            'FOR EACH ROW EXECUTE FUNCTION marketpilot.deny_audit_mutation()',
            table_name
        );
    END LOOP;
END;
$$;

COMMIT;
