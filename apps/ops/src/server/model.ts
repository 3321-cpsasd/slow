export type UserMetricRow = {
  account_ref: string;
  username: string;
  account_status: string;
  created_at: Date | string;
  last_login_at: Date | string | null;
  privacy_consent_current: boolean;
  privacy_accepted_at: Date | string | null;
  profile_completed: boolean;
  first_section_started: boolean;
  first_chapter_completed: boolean;
  first_book_completed: boolean;
  retained_concepts_7d: string | number;
  retained_claims: string | number;
  failed_tasks: string | number;
  feedback_count: string | number;
  product_events_7d: string | number;
  last_product_event_at: Date | string | null;
  ai_invocations: string | number;
  failed_ai_invocations: string | number;
  input_tokens: string | number;
  output_tokens: string | number;
  total_tokens: string | number;
  exit_status: string;
  exit_requested_at: Date | string | null;
  deletion_due_at: Date | string | null;
};

export type UserMetric = {
  accountRef: string;
  username: string;
  accountStatus: string;
  createdAt: string;
  lastLoginAt: string | null;
  privacyConsentCurrent: boolean;
  privacyAcceptedAt: string | null;
  profileCompleted: boolean;
  firstSectionStarted: boolean;
  firstChapterCompleted: boolean;
  firstBookCompleted: boolean;
  retainedConcepts7d: number;
  retainedClaims: number;
  failedTasks: number;
  feedbackCount: number;
  productEvents7d: number;
  lastProductEventAt: string | null;
  aiInvocations: number;
  failedAiInvocations: number;
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  exitStatus: string;
  exitRequestedAt: string | null;
  deletionDueAt: string | null;
};

function iso(value: Date | string | null): string | null {
  if (!value) return null;
  return new Date(value).toISOString();
}

function integer(value: string | number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

export function normalizeUserMetric(row: UserMetricRow): UserMetric {
  return {
    accountRef: row.account_ref,
    username: row.username,
    accountStatus: row.account_status,
    createdAt: iso(row.created_at) ?? "",
    lastLoginAt: iso(row.last_login_at),
    privacyConsentCurrent: row.privacy_consent_current,
    privacyAcceptedAt: iso(row.privacy_accepted_at),
    profileCompleted: row.profile_completed,
    firstSectionStarted: row.first_section_started,
    firstChapterCompleted: row.first_chapter_completed,
    firstBookCompleted: row.first_book_completed,
    retainedConcepts7d: integer(row.retained_concepts_7d),
    retainedClaims: integer(row.retained_claims),
    failedTasks: integer(row.failed_tasks),
    feedbackCount: integer(row.feedback_count),
    productEvents7d: integer(row.product_events_7d),
    lastProductEventAt: iso(row.last_product_event_at),
    aiInvocations: integer(row.ai_invocations),
    failedAiInvocations: integer(row.failed_ai_invocations),
    inputTokens: integer(row.input_tokens),
    outputTokens: integer(row.output_tokens),
    totalTokens: integer(row.total_tokens),
    exitStatus: row.exit_status,
    exitRequestedAt: iso(row.exit_requested_at),
    deletionDueAt: iso(row.deletion_due_at),
  };
}

export function summarize(users: UserMetric[]) {
  return {
    accounts: users.length,
    active: users.filter((user) => user.accountStatus === "active").length,
    consented: users.filter((user) => user.privacyConsentCurrent).length,
    active7d: users.filter((user) => user.productEvents7d > 0).length,
    firstSection: users.filter((user) => user.firstSectionStarted).length,
    firstChapter: users.filter((user) => user.firstChapterCompleted).length,
    firstBook: users.filter((user) => user.firstBookCompleted).length,
    retained7d: users.filter((user) => user.retainedConcepts7d > 0).length,
    failedTasks: users.reduce((sum, user) => sum + user.failedTasks, 0),
    failedAi: users.reduce((sum, user) => sum + user.failedAiInvocations, 0),
    feedback: users.reduce((sum, user) => sum + user.feedbackCount, 0),
    exits: users.filter((user) => user.exitStatus === "requested").length,
  };
}
