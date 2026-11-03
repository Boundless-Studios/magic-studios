-- T1: MotionFrameMetadata.media_kind became a required field. Manifests and
-- release snapshots written before that change store candidate metadata
-- objects without it. Backfill media_kind into every stored manifest and
-- release snapshot so AnimationProjectManifest.model_validate(...) keeps
-- loading them, deriving "video" for artifact keys ending in a known video
-- extension and "sheet" otherwise. Idempotent: only candidates whose
-- metadata object lacks the media_kind key are touched.
--
-- 2026-09-05 incident (Sentry MAGIC-VIDEO-EDITOR-BACKEND-4/5/6/A): this runs on
-- every start and rewrites manifests. For a motion whose candidates array is
-- EMPTY, jsonb_agg over zero rows is NULL and jsonb_set(motion, '{candidates}',
-- NULL) returns NULL, so the whole motion became a JSON null and the manifest
-- could no longer be read. Every deploy nulled every motion without takes. The
-- inner aggregate is now COALESCEd to '[]' and the per-motion result is guarded
-- with COALESCE(..., motion), so a motion can never be replaced by null.

CREATE OR REPLACE FUNCTION studio_backfill_media_kind(manifest jsonb) RETURNS jsonb AS $$
DECLARE
    backfilled_motions jsonb;
BEGIN
    IF NOT (manifest ? 'motions') OR jsonb_typeof(manifest->'motions') != 'array' THEN
        RETURN manifest;
    END IF;

    SELECT jsonb_agg(
        COALESCE(CASE
            WHEN motion ? 'candidates' AND jsonb_typeof(motion->'candidates') = 'array' THEN
                jsonb_set(motion, '{candidates}', COALESCE((
                    SELECT jsonb_agg(
                        CASE
                            WHEN jsonb_typeof(candidate->'metadata') = 'object'
                                 AND NOT (candidate->'metadata' ? 'media_kind') THEN
                                jsonb_set(
                                    candidate,
                                    '{metadata,media_kind}',
                                    to_jsonb(
                                        CASE
                                            WHEN (candidate->>'artifact_key') ~* '\.(mp4|webm|mov)$'
                                                THEN 'video'
                                            ELSE 'sheet'
                                        END
                                    )
                                )
                            ELSE candidate
                        END
                        ORDER BY ordinality
                    )
                    FROM jsonb_array_elements(motion->'candidates') WITH ORDINALITY AS c(candidate, ordinality)
                ), '[]'::jsonb))
            ELSE motion
        END, motion)
        ORDER BY ordinality
    )
    INTO backfilled_motions
    FROM jsonb_array_elements(manifest->'motions') WITH ORDINALITY AS m(motion, ordinality);

    RETURN jsonb_set(manifest, '{motions}', COALESCE(backfilled_motions, '[]'::jsonb));
END;
$$ LANGUAGE plpgsql IMMUTABLE;

UPDATE studio_manifest_revisions
SET manifest = studio_backfill_media_kind(manifest)
WHERE manifest != studio_backfill_media_kind(manifest);

UPDATE studio_releases
SET manifest_snapshot = studio_backfill_media_kind(manifest_snapshot)
WHERE manifest_snapshot != studio_backfill_media_kind(manifest_snapshot);

DROP FUNCTION studio_backfill_media_kind(jsonb);
