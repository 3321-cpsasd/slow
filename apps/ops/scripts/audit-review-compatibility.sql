\set ON_ERROR_STOP on

BEGIN TRANSACTION READ ONLY;

SELECT 'alembic_version' AS metric, version_num AS value
FROM alembic_version
ORDER BY version_num;

SELECT 'review_assignment_status.' || status AS metric, count(*)::text AS value
FROM review_assignments
GROUP BY status
ORDER BY status;

SELECT 'review_states.due_now' AS metric, count(*)::text AS value
FROM review_states
WHERE next_due_at IS NOT NULL
  AND next_due_at <= CURRENT_TIMESTAMP;

SELECT 'assessment_answer_versions.total' AS metric, count(*)::text AS value
FROM assessment_answer_versions;

SELECT 'assessment_items.total' AS metric, count(*)::text AS value
FROM assessment_item_versions
UNION ALL
SELECT 'assessment_items.payload_with_inline_correct', count(*)::text
FROM assessment_item_versions
WHERE payload_json::jsonb ? 'correct'
UNION ALL
SELECT 'assessment_items.payload_identity_matches', count(*)::text
FROM assessment_item_versions
WHERE payload_json::jsonb ->> 'id' = id
  AND payload_json::jsonb ->> 'assessmentTargetId' = assessment_target_id;

WITH per_quiz AS (
    SELECT
        quiz_set_id,
        count(*) AS item_count,
        min(position) AS min_position,
        max(position) AS max_position,
        count(DISTINCT position) AS distinct_positions
    FROM assessment_item_versions
    GROUP BY quiz_set_id
)
SELECT 'assessment_quizzes.contiguous_item_positions' AS metric,
       count(*)::text AS value
FROM per_quiz
WHERE min_position = 0
  AND max_position = item_count - 1
  AND distinct_positions = item_count;

WITH latest_governance AS (
    SELECT DISTINCT ON (quiz_set_id)
        quiz_set_id,
        allowed,
        assessment_eligible
    FROM governance_decision_snapshots
    WHERE decision_scope = 'quiz_publication'
    ORDER BY quiz_set_id, created_at DESC, id DESC
), evaluated AS (
    SELECT
        assignment.id,
        selection.selection_date,
        EXISTS (
            SELECT 1
            FROM assessment_answer_versions answer
            WHERE answer.assessment_item_version_id = item.id
              AND answer.publication_status = 'published'
        ) AS source_has_published_answer,
        CASE
            WHEN observation.id IS NULL THEN 'missing_exact_observation'
            WHEN quiz.id IS NULL THEN 'missing_source_quiz'
            WHEN quiz.publication_status NOT IN ('published', 'superseded')
                THEN 'source_quiz_not_publishable'
            WHEN content.id IS NULL OR contract.id IS NULL
                THEN 'missing_content_or_contract'
            WHEN quiz.section_id <> assignment.source_section_id
              OR quiz.content_version_id <> assignment.content_version_id
              OR quiz.learning_contract_version_id
                    <> assignment.learning_contract_version_id
              OR content.section_id <> assignment.source_section_id
              OR content.learning_contract_version_id
                    <> assignment.learning_contract_version_id
              OR contract.section_id <> assignment.source_section_id
                THEN 'version_mismatch'
            WHEN content.publication_status NOT IN ('published', 'superseded')
                THEN 'source_content_not_publishable'
            WHEN EXISTS (
                SELECT 1
                FROM learning_evidence_invalidations invalidation
                WHERE invalidation.quiz_set_id = quiz.id
            ) THEN 'source_invalidated'
            WHEN governance.quiz_set_id IS NULL
              OR NOT governance.allowed
              OR NOT governance.assessment_eligible
                THEN 'source_governance_invalid'
            WHEN item.id IS NULL THEN 'missing_immutable_item'
            WHEN EXISTS (
                SELECT 1
                FROM assessment_item_versions quiz_item
                WHERE quiz_item.quiz_set_id = quiz.id
                  AND (
                      quiz_item.payload_json::jsonb ->> 'id' <> quiz_item.id
                      OR quiz_item.payload_json::jsonb ->> 'assessmentTargetId'
                            <> quiz_item.assessment_target_id
                  )
            ) THEN 'immutable_quiz_payload_invalid'
            WHEN (
                SELECT count(*)
                FROM assessment_answer_versions answer
                JOIN assessment_item_versions answered_item
                  ON answered_item.id = answer.assessment_item_version_id
                WHERE answered_item.quiz_set_id = quiz.id
            ) NOT IN (
                0,
                (SELECT count(*)
                 FROM assessment_item_versions quiz_item
                 WHERE quiz_item.quiz_set_id = quiz.id)
            ) THEN 'answer_versions_incomplete'
            WHEN EXISTS (
                SELECT 1
                FROM assessment_answer_versions answer
                JOIN assessment_item_versions answered_item
                  ON answered_item.id = answer.assessment_item_version_id
                WHERE answered_item.quiz_set_id = quiz.id
                  AND answer.publication_status <> 'published'
            ) THEN 'answer_not_published'
            WHEN NOT EXISTS (
                SELECT 1
                FROM assessment_answer_versions answer
                JOIN assessment_item_versions answered_item
                  ON answered_item.id = answer.assessment_item_version_id
                WHERE answered_item.quiz_set_id = quiz.id
            ) AND EXISTS (
                SELECT 1
                FROM assessment_item_versions quiz_item
                WHERE quiz_item.quiz_set_id = quiz.id
                  AND NOT (quiz_item.payload_json::jsonb ? 'correct')
            ) THEN 'legacy_answer_missing'
            WHEN item.assessment_target_id <> assignment.assessment_target_id
                THEN 'item_target_mismatch'
            WHEN NOT EXISTS (
                SELECT 1
                FROM learning_contract_assessment_targets contract_target
                WHERE contract_target.contract_version_id = contract.id
                  AND contract_target.assessment_target_id
                        = assignment.assessment_target_id
            ) THEN 'target_not_in_contract'
            WHEN evidence.evidence_count = 0 THEN 'missing_evidence'
            WHEN evidence.same_content_count <> evidence.evidence_count
                THEN 'cross_content_evidence'
            WHEN evidence.teaches_count <> evidence.evidence_count
                THEN 'evidence_target_mismatch'
            WHEN evidence.payload_ids <> evidence.bound_ids
                THEN 'payload_binding_mismatch'
            ELSE 'compatible'
        END AS compatibility
    FROM review_assignments assignment
    JOIN review_selection_runs selection
      ON selection.id = assignment.selection_run_id
    LEFT JOIN LATERAL (
        SELECT candidate.*
        FROM assessment_observations candidate
        WHERE candidate.user_id = assignment.user_id
          AND candidate.learning_run_id = assignment.source_learning_run_id
          AND candidate.section_id = assignment.source_section_id
          AND candidate.quiz_set_id = assignment.prior_quiz_set_id
          AND candidate.content_version_id = assignment.content_version_id
          AND candidate.learning_contract_version_id
                = assignment.learning_contract_version_id
          AND candidate.assessment_target_id = assignment.assessment_target_id
          AND candidate.question_index IS NOT NULL
        ORDER BY candidate.sequence DESC
        LIMIT 1
    ) observation ON TRUE
    LEFT JOIN quiz_sets quiz ON quiz.id = assignment.prior_quiz_set_id
    LEFT JOIN content_versions content ON content.id = quiz.content_version_id
    LEFT JOIN learning_contract_versions contract
      ON contract.id = quiz.learning_contract_version_id
    LEFT JOIN LATERAL (
        SELECT candidate.*
        FROM assessment_item_versions candidate
        WHERE candidate.quiz_set_id = quiz.id
          AND candidate.position = observation.question_index
        ORDER BY candidate.id
        LIMIT 1
    ) item ON TRUE
    LEFT JOIN latest_governance governance ON governance.quiz_set_id = quiz.id
    LEFT JOIN LATERAL (
        SELECT
            count(*) AS evidence_count,
            count(*) FILTER (
                WHERE block.content_version_id = assignment.content_version_id
            ) AS same_content_count,
            count(*) FILTER (
                WHERE EXISTS (
                    SELECT 1
                    FROM content_block_assessment_targets taught
                    WHERE taught.content_block_version_id
                            = binding.content_block_version_id
                      AND taught.assessment_target_id
                            = assignment.assessment_target_id
                      AND taught.binding_role = 'teaches'
                )
            ) AS teaches_count,
            coalesce(
                array_agg(
                    DISTINCT binding.content_block_version_id::text
                    ORDER BY binding.content_block_version_id::text
                ),
                ARRAY[]::text[]
            ) AS bound_ids,
            coalesce((
                SELECT array_agg(DISTINCT value ORDER BY value)
                FROM jsonb_array_elements_text(
                    coalesce(
                        item.payload_json::jsonb -> 'evidenceBlockIds',
                        '[]'::jsonb
                    )
                ) value
            ), ARRAY[]::text[]) AS payload_ids
        FROM assessment_item_evidence_blocks binding
        LEFT JOIN content_block_versions block
          ON block.id = binding.content_block_version_id
        WHERE binding.assessment_item_version_id = item.id
    ) evidence ON TRUE
), scoped AS (
    SELECT 'all' AS scope, * FROM evaluated
    UNION ALL
    SELECT 'today', *
    FROM evaluated
    WHERE selection_date = (
        (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai')::date
    )::text
)
SELECT
    'assignments.' || scope || '.' || compatibility AS metric,
    count(*)::text AS value
FROM scoped
GROUP BY scope, compatibility
UNION ALL
SELECT
    'assignments.' || scope || '.source_has_published_answer' AS metric,
    count(*) FILTER (WHERE source_has_published_answer)::text AS value
FROM scoped
GROUP BY scope
ORDER BY metric;

ROLLBACK;
