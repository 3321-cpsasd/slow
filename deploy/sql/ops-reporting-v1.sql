\set ON_ERROR_STOP on

CREATE SCHEMA IF NOT EXISTS ops_reporting;
REVOKE ALL ON SCHEMA ops_reporting FROM PUBLIC;

CREATE OR REPLACE VIEW ops_reporting.user_metrics_v1
WITH (security_barrier = true) AS
WITH current_consent AS (
    SELECT DISTINCT ON (user_id)
        user_id,
        accepted_at
    FROM privacy_consents
    WHERE notice_version = '2026-08-08-r2'
      AND trial_terms_version = '2026-08-08'
      AND status = 'accepted'
    ORDER BY user_id, accepted_at DESC
),
latest_exit AS (
    SELECT DISTINCT ON (user_id)
        user_id,
        status,
        requested_at,
        deletion_due_at
    FROM account_exit_requests
    ORDER BY user_id, requested_at DESC
),
chapter_completion AS (
    SELECT user_id, count(*) FILTER (WHERE status = 'completed') AS completed_count
    FROM chapter_progress
    GROUP BY user_id
),
book_completion AS (
    SELECT user_id, count(*) FILTER (WHERE status = 'completed') AS completed_count
    FROM book_progress
    GROUP BY user_id
),
retained_projection AS (
    SELECT user_id, count(*) FILTER (WHERE claim_status = 'retained') AS retained_count
    FROM knowledge_state_projections
    GROUP BY user_id
),
failed_tasks AS (
    SELECT user_id, count(*) FILTER (WHERE status = 'failed') AS failed_count
    FROM learning_tasks
    GROUP BY user_id
),
feedback AS (
    SELECT user_id, count(*) AS feedback_count
    FROM user_feedback
    GROUP BY user_id
),
product_activity AS (
    SELECT
        user_id,
        count(*) FILTER (WHERE received_at >= now() - interval '7 days') AS event_count_7d,
        max(received_at) AS last_event_at
    FROM product_events
    GROUP BY user_id
),
ai AS (
    SELECT
        i.subject_user_id AS user_id,
        count(DISTINCT i.id) AS invocation_count,
        count(DISTINCT i.id) FILTER (WHERE i.status = 'failed') AS failed_count,
        coalesce(sum(m.input_tokens), 0) AS input_tokens,
        coalesce(sum(m.output_tokens), 0) AS output_tokens,
        coalesce(sum(m.total_tokens), 0) AS total_tokens
    FROM ai_invocations AS i
    LEFT JOIN ai_usage_measurements AS m ON m.invocation_id = i.id
    WHERE i.subject_user_id IS NOT NULL
    GROUP BY i.subject_user_id
),
retention_7d AS (
    SELECT
        ra.user_id,
        count(DISTINCT ra.assessment_target_id) AS retained_count
    FROM review_assignments AS ra
    JOIN quiz_attempts AS submitted
      ON submitted.id = ra.submitted_attempt_id
     AND submitted.passed IS TRUE
    JOIN LATERAL (
        SELECT min(prior.created_at) AS first_passed_at
        FROM quiz_attempts AS prior
        WHERE prior.user_id = ra.user_id
          AND prior.quiz_set_id = ra.prior_quiz_set_id
          AND prior.passed IS TRUE
    ) AS first_pass ON first_pass.first_passed_at IS NOT NULL
    WHERE ra.status = 'submitted'
      AND submitted.created_at - first_pass.first_passed_at >= interval '7 days'
    GROUP BY ra.user_id
)
SELECT
    'U-' || upper(substr(md5(u.id), 1, 10)) AS account_ref,
    coalesce(c.username, '') AS username,
    u.status AS account_status,
    u.created_at,
    c.last_login_at,
    (consent.user_id IS NOT NULL) AS privacy_consent_current,
    consent.accepted_at AS privacy_accepted_at,
    (profile.completed_at IS NOT NULL) AS profile_completed,
    (
        EXISTS (SELECT 1 FROM learning_resume_positions rp WHERE rp.user_id = u.id)
        OR EXISTS (SELECT 1 FROM quiz_attempts qa WHERE qa.user_id = u.id)
    ) AS first_section_started,
    (coalesce(chapter.completed_count, 0) > 0) AS first_chapter_completed,
    (coalesce(book.completed_count, 0) > 0) AS first_book_completed,
    coalesce(retention.retained_count, 0)::bigint AS retained_concepts_7d,
    coalesce(projection.retained_count, 0)::bigint AS retained_claims,
    coalesce(tasks.failed_count, 0)::bigint AS failed_tasks,
    coalesce(feedback.feedback_count, 0)::bigint AS feedback_count,
    coalesce(activity.event_count_7d, 0)::bigint AS product_events_7d,
    activity.last_event_at AS last_product_event_at,
    coalesce(ai.invocation_count, 0)::bigint AS ai_invocations,
    coalesce(ai.failed_count, 0)::bigint AS failed_ai_invocations,
    coalesce(ai.input_tokens, 0)::bigint AS input_tokens,
    coalesce(ai.output_tokens, 0)::bigint AS output_tokens,
    coalesce(ai.total_tokens, 0)::bigint AS total_tokens,
    coalesce(exit_request.status, '') AS exit_status,
    exit_request.requested_at AS exit_requested_at,
    exit_request.deletion_due_at
FROM users AS u
LEFT JOIN local_credentials AS c ON c.user_id = u.id
LEFT JOIN user_profiles AS profile ON profile.user_id = u.id
LEFT JOIN current_consent AS consent ON consent.user_id = u.id
LEFT JOIN latest_exit AS exit_request ON exit_request.user_id = u.id
LEFT JOIN chapter_completion AS chapter ON chapter.user_id = u.id
LEFT JOIN book_completion AS book ON book.user_id = u.id
LEFT JOIN retained_projection AS projection ON projection.user_id = u.id
LEFT JOIN failed_tasks AS tasks ON tasks.user_id = u.id
LEFT JOIN feedback ON feedback.user_id = u.id
LEFT JOIN product_activity AS activity ON activity.user_id = u.id
LEFT JOIN ai ON ai.user_id = u.id
LEFT JOIN retention_7d AS retention ON retention.user_id = u.id;

REVOKE ALL ON ops_reporting.user_metrics_v1 FROM PUBLIC;
