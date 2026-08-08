import assert from "node:assert/strict";
import test from "node:test";
import { normalizeUserMetric, summarize, type UserMetricRow } from "./model.ts";

const row: UserMetricRow = {
  account_ref: "U-EXAMPLE",
  username: "operator-test",
  account_status: "active",
  created_at: "2026-08-01T00:00:00Z",
  last_login_at: null,
  privacy_consent_current: true,
  privacy_accepted_at: "2026-08-02T00:00:00Z",
  profile_completed: true,
  first_section_started: true,
  first_chapter_completed: false,
  first_book_completed: false,
  retained_concepts_7d: "1",
  retained_claims: "2",
  failed_tasks: "3",
  feedback_count: "4",
  product_events_7d: "12",
  last_product_event_at: "2026-08-07T10:00:00Z",
  ai_invocations: "5",
  failed_ai_invocations: "1",
  input_tokens: "100",
  output_tokens: "200",
  total_tokens: "300",
  exit_status: "",
  exit_requested_at: null,
  deletion_due_at: null,
};

test("normalizes PostgreSQL bigint and timestamp values", () => {
  const user = normalizeUserMetric(row);
  assert.equal(user.retainedConcepts7d, 1);
  assert.equal(user.totalTokens, 300);
  assert.equal(user.createdAt, "2026-08-01T00:00:00.000Z");
});

test("summarizes operational attention signals", () => {
  const summary = summarize([normalizeUserMetric(row)]);
  assert.deepEqual(summary, {
    accounts: 1,
    active: 1,
    consented: 1,
    active7d: 1,
    firstSection: 1,
    firstChapter: 0,
    firstBook: 0,
    retained7d: 1,
    failedTasks: 3,
    failedAi: 1,
    feedback: 4,
    exits: 0,
  });
});
