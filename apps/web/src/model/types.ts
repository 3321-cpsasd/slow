export type SectionSummary = { id:string; position:number; title:string; question:string; objectives:string[]; status:string; generated?:boolean; bestScore:number; totalScore:number; askMeUnlocked:boolean };
export type Attachment = { id:string; filename:string; mediaType:string; byteSize:number; sha256:string; createdAt:string };
export type Practice = { id:string; title:string; instructions:Record<string,unknown>; submission:Record<string,unknown>; attachments:Attachment[]; evidenceMode:'file_attachment'|'structured_only_legacy'; status:string };
export type Capstone = { id:string; title:string; brief:Record<string,unknown>; submission:Record<string,unknown>; attachments:Attachment[]; evidenceMode:'file_attachment'|'structured_only_legacy'; status:string };
export type BookSettlement = {
  bookId:string;
  bookTitle:string;
  status:'completed';
  chapterCount:number;
  completedChapterCount:number;
  sectionCount:number;
  completedSectionCount:number;
  verificationScore:number;
  verificationTotal:number;
  verificationRate:number|null;
  perfectSectionCount:number;
  reviewSectionCount:number;
  ruleVersion:string;
  settledAt:string|null;
};
export type ChapterWorkloadHint = { level:'anomalous'|'light'|'typical'|'extended'; sectionCount:number; typicalRange:[number,number]; technicalRange:[number,number]; message:string };
export type Chapter = { id:string; position:number; title:string; objective:string; status:string; generated:boolean; workloadHint:ChapterWorkloadHint|null; sections:SectionSummary[]; practice:null|Practice };
export type Book = { id:string; position:number; title:string; description:string; estimatedMinutes:number; outlineStatus:'draft'|'confirmed'; outlineVersion:number; outlineConfirmedAt:string|null; status:string; progress:number; practiceProgress:number; chapters:Chapter[]; capstone:null|Capstone };
export type BookReplanProposal = { proposalId:string; rationale:string; chapters:{title:string;objective:string}[]; requiresConfirmation:true };
export type Series = {
  id:string;
  title:string;
  rationale:string;
  progress:number;
  progressBasis?:string;
  books:Book[];
  initializationTask?:LearningTask|null;
};
export type Shelf = { id:string; name:string; domain:string; specialty:string; tags:string[]; series:Series[] };
export type ShelfCreateInput = { name:string };
export type ShelfRenameInput = { name:string };
export type SeriesRenameInput = { name:string };
export type LearningStartPreference =
  | 'practical_application'
  | 'understand_principles'
  | 'case_based'
  | 'practice_heavy';
export type LearningStartPreview = {
  schemaVersion:'learning_start_preview_v1';
  previewId:string;
  availability:'ready'|'not_ready';
  topic:string;
  title:string;
  nodes:{conceptRevisionId:string;label:string;meaning:string}[];
  edges:{id:string;from:string;to:string;type:string;label:string}[];
  message:string;
};
export type LearningGoalDimensionKey =
  | 'learning_object'
  | 'purpose'
  | 'success_marker'
  | 'starting_point'
  | 'daily_commitment'
  | 'completion_horizon'
  | 'scope';
export type LearningGoalInterviewAnswer = {
  questionId:string;
  dimension:LearningGoalDimensionKey;
  question:string;
  answer:string;
};
export type LearningGoalInterview = {
  schemaVersion:'learning_goal_interview_v1';
  status:'ask'|'ready';
  progressMessage:string;
  dimensions:{
    key:LearningGoalDimensionKey;
    status:'confirmed'|'inferred'|'missing'|'conflict'|'immaterial';
    summary:string;
    confidence:'high'|'medium'|'low';
  }[];
  question:null|{
    id:string;
    dimension:LearningGoalDimensionKey;
    prompt:string;
    helper:string;
    options:{id:string;label:string;description:string}[];
  };
  brief:null|{
    topic:string;
    purpose:string;
    successMarker:string;
    startingPoint:string;
    dailyCommitment:string;
    completionHorizon:string;
    scope:string;
    outOfScope:string;
    recommendedDepth:'overview'|'deep'|'mastery';
  };
  answerCount:number;
  generationMode:'ai'|'demo';
  ruleVersion:string;
};
export type ResumePosition = {
  learningRunId:string;
  sectionId:string;
  blockId:string;
  updatedAt:string;
};
export type DailyMode = 'fast'|'slow';
export type DailyModeDuration = '1h'|'3h'|'6h'|'today';
export type DailyModeSource = 'dialog'|'header_toggle'|'duration_adjustment';
export type DailyModeState = {
  active:boolean;
  dailyMode:DailyMode|null;
  lastDailyMode:DailyMode|null;
  duration:DailyModeDuration|null;
  timezone:string|null;
  activatedAt:string|null;
  expiresAt:string|null;
  version:number;
  serverNow:string;
};
export type StudyActivityKind = 'reading_thinking'|'verification_review'|'ask_ai';
export type StudyActivitySummary = {
  date:string;
  timezone:string;
  totalSeconds:number;
  categories:{activityKind:StudyActivityKind;seconds:number}[];
  episodes:{startedAt:string;endedAt:string;durationSeconds:number}[];
  measurementRuleVersion:string;
  estimated:true;
  serverNow:string;
};
export type Bootstrap = {
  user:{id:string;name:string};
  shelves:Shelf[];
  profile:LearningProfile;
  resume:ResumePosition|null;
  dailyMode:DailyModeState;
  milestoneDashboard:MilestoneDashboard;
};
export type LearningPreferences = {
  openingStyle:'auto'|'problem_first'|'example_first'|'concept_first';
  explanationDensity:'auto'|'concise'|'balanced'|'thorough';
  formatPreferences:('diagram'|'worked_example'|'code'|'table'|'analogy')[];
  interactionRhythm:'auto'|'low_interruption'|'balanced'|'frequent_checkins';
  dailyModePromptEnabled:boolean;
};
export type LearningProfile = {
  profession:string;
  stage:'exploring'|'beginner'|'foundation'|'practice'|'advanced'|'';
  purpose:string;
  domains:string[];
  experience:string;
  weeklyMinutes:number;
  targetDate:string;
  preferences:LearningPreferences;
  version:number;
  completedAt:string|null;
};
export type MilestoneCriterion = {
  key:string;
  statement:string;
  chapterId:string;
  bookId:string;
  bookTitle:string;
  evidenceRule:'all_section_quizzes_passed';
  completed:boolean;
  evidenceCount:number;
  expectedEvidenceCount:number;
};
export type Milestone = {
  key:string;
  title:string;
  outcome:string;
  criteria:MilestoneCriterion[];
  achieved:boolean;
};
export type MilestonePath = {
  id:string|null;
  seriesId:string;
  seriesTitle:string;
  status:'proposed'|'confirmed';
  version:number;
  rulesetVersion:string;
  goalAligned:boolean;
  currentIndex:number;
  progress:number;
  completedCriteria:number;
  totalCriteria:number;
  milestones:Milestone[];
};
export type TodayLearning = {
  seriesId:string;
  sectionId:string|null;
  bookTitle:string;
  chapterTitle:string;
  sectionTitle:string;
  question:string;
  estimatedMinutes:number;
  reason:string;
};
export type MilestoneDashboard = {
  goal:{
    statement:string;
    domains:string[];
    weeklyMinutes:number;
    targetDate:string;
    profileVersion:number;
  };
  path:MilestonePath|null;
  today:TodayLearning|null;
};
export type OnboardingStep = {
  id:'identity'|'direction'|'review';
  title:string;
  description:string;
};
export type OnboardingState = {
  flowId:string;
  flowVersion:number;
  required:boolean;
  status:'required'|'completed';
  currentStep:OnboardingStep['id'];
  steps:OnboardingStep[];
  profile:LearningProfile;
};
export type OnboardingCompletion = OnboardingState & {
  firstShelfId:string|null;
  firstShelfCreated:boolean;
};
export type AuthState = {
  authenticated:boolean;
  mode:'demo'|'local'|'password'|'oidc';
  user:{id:string;name:string};
  csrfToken:string;
  privacy:PrivacyState;
  onboarding:OnboardingState;
};
export type PrivacyNoticeItem = { title:string; body:string };
export type PrivacyNotice = {
  noticeVersion:string;
  trialTermsVersion:string;
  title:string;
  summary:string;
  items:PrivacyNoticeItem[];
};
export type PrivacyState = PrivacyNotice & {
  required:boolean;
  status:'accepted'|'required'|'not_required';
  acceptedAt:string|null;
};
export type AccountExitReceipt = {
  requestId:string;
  status:'requested'|'processing'|'completed';
  requestedAt:string;
  deletionDueAt:string;
  policyVersion:string;
};
export type AuthConfig = {
  mode:'demo'|'local'|'password'|'oidc';
  registrationMode:'closed'|'alpha'|'open';
  registrationCodeRequired:boolean;
  providerName:string;
  privacyNotice:PrivacyNotice;
};
export type RegistrationResult = AuthState & { recoveryCode:string };
export type RecoveryResetResult = { reset:true; recoveryCode:string };
export type AiRuntime = {
  mode:'provider'|'demo'|'injected';
  configured:boolean;
  model:string;
  providerModel:string;
  fallbackModels:string[];
  baseUrl:string;
  apiKeyStored:boolean;
  ephemeral:boolean;
  providerProtocol:'openai'|'anthropic';
  apiMode:'responses'|'chat_completions'|'messages';
  reasoningMode:'optional'|'required'|'disabled';
  structuredOutput:boolean;
  streaming:boolean;
};
export type Source = { title:string; url:string; kind:string; version:string };
export type Block = {
  id:string;
  version:number;
  kind:string;
  role:string;
  heading:string;
  content:string;
  source_indexes:number[];
  teachingMoves?:string[];
  caseKind?:string;
  relationToAnchor?:string;
  readerPriority?:'essential'|'highlight'|'normal';
  assessmentTargetIds?:string[];
  personalPresentation?:{
    id:string;
    content:string;
    source:'ask_ai';
    updatedAt:string;
  };
};
export type LearningPreferenceProjection = {
  recorded?:boolean;
  updatedAt:string;
  dimensions:{
    key:string;
    label:string;
    score:number;
    confidence:number;
    evidenceCount:number;
    positiveOutcomes:number;
    contextCount:number;
    active:boolean;
  }[];
  suggestedPreferences:Record<string,unknown>;
  confirmedPreferences:Record<string,unknown>;
  effectivePreferences:Record<string,unknown>;
};
export type FeedbackReceipt = {
  id:string;
  status:'received';
  scope:'global'|'content_block'|'quiz_question';
  createdAt:string;
  regeneration:{
    status:'stream_ready'|'recorded_only'|'queued'|'blocked'|'needs_review'|'not_applicable';
    reasonCode:string|null;
    task:LearningTask|null;
  };
};
export type FeedbackRepairResult = {
  feedbackId:string;
  contentVersionId:string;
  contentVersion:number;
  contentBlockId:string;
  replayed:boolean;
};
export type Question = {
  id?:string;
  prompt:string;
  options:string[];
  core:boolean;
  objective:string;
  assessmentTargetId?:string;
  evidenceBlockIds?:string[];
  selectionMode:'single'|'multiple';
};
export type ChapterChallenge = {
  schemaVersion:'chapter_challenge_view_v1';
  chapterId:string;
  chapterTitle:string;
  objective:string;
  status:'ready';
  questionCount:number;
  sections:{
    sectionId:string;
    position:number;
    title:string;
    quizSetId:string;
    questions:Question[];
  }[];
};
export type ChapterChallengeResult = {
  schemaVersion:'chapter_challenge_result_v1';
  attemptId:string;
  chapterId:string;
  passed:boolean;
  passedSectionCount:number;
  totalSectionCount:number;
  sectionResults:{
    sectionId:string;
    position:number;
    title:string;
    status:'passed'|'needs_learning';
    score:number;
    total:number;
  }[];
  nextChapterId?:string|null;
  nextBookId?:string|null;
};
export type ChapterRouteResult = {
  chapterId:string;
  status:'skipped'|'available';
  reason?:'not_focus'|'defer_unknown'|'challenge_exit';
  nextChapterId?:string|null;
  nextBookId?:string|null;
};
export type QuizGovernance = {
  decisionId:string;
  scope:string;
  requestedMode:'formal'|'experimental';
  mode:'formal'|'experimental'|'rejected';
  allowed:boolean;
  assessmentEligible:boolean;
  reasons:{code:string;message:string;severity:'blocking'|'warning';subjectIds:string[]}[];
  ruleVersion:string;
};
export type ReviewAssignmentItem = {
  assignmentId:string;
  assessmentTargetId:string;
  objective:string;
  status:'scheduled'|'presented'|'started'|'submitted'|'skipped'|'expired';
  dueAt:string;
  expiresAt:string;
  rank:number;
  basePriority:number;
  effectivePriority:number;
  quizSetId:string|null;
  reviewReason:string;
  capability:null|{
    label:string;
    currentStage:'unranked'|'bronze'|'silver'|'gold'|'diamond';
    activationState:string;
  };
  taskPlan:ReviewTaskPlan;
};
export type ReviewTaskPlan = {
  ruleVersion:string;
  reactivation:{
    purpose:'retention_reactivation';
    taskKind:'choice_reactivation'|'oral_reactivation'|'application_reactivation'|'transfer_reactivation';
    stage:'bronze'|'silver'|'gold'|'diamond';
    criterionIds:string[];
    verificationProtocols:string[];
    evidenceEffect:'activation_only';
  };
  strengthening:null|{
    purpose:'stage_strengthening';
    taskKind:'oral_strengthening'|'application_strengthening'|'transfer_strengthening';
    stage:'silver'|'gold'|'diamond';
    criterionIds:string[];
    verificationProtocols:string[];
    evidenceEffect:'may_advance_stage_after_qualified_evidence';
  };
};
export type DueReviews = {
  selectionRunId:string;
  asOf:string;
  dailyBudget:number;
  dueCount:number;
  selectedCount:number;
  ruleVersion:string;
  items:ReviewAssignmentItem[];
};
export type KnowledgeMapNode = {
  capabilityRevisionId:string;
  capabilityId:string;
  label:string;
  stage:'unranked'|'bronze'|'silver'|'gold'|'diamond';
  stageOrder:number;
  stageLabel:string;
  naturalStageCeiling:'bronze'|'silver'|'gold'|'diamond';
  naturalStageCeilingLabel:string;
  routeStageCeiling:'bronze'|'silver'|'gold'|'diamond';
  routeStageCeilingLabel:string;
  activationState:'learning'|'available'|'due_for_reactivation';
  stabilityDays:number;
  nextDueAt:string|null;
  evidenceCount:number;
  independentEvidenceCount:number;
  targetCount:number;
  knowledge:{conceptRevisionId:string;label:string;role:'anchor'|'required'|'supporting';required:boolean}[];
  relations:{knowledgeRelationRevisionId:string;fromConceptRevisionId:string;toConceptRevisionId:string;type:string;label:string;statement:string;required:boolean;minimumStage:'bronze'|'silver'|'gold'|'diamond'}[];
  routeContexts:{seriesId:string;seriesTitle:string;bookId:string;bookTitle:string;chapterId:string;chapterTitle:string;sectionId:string;sectionTitle:string;required:boolean;contractVersionId:string}[];
  nextStage:'bronze'|'silver'|'gold'|'diamond'|null;
  nextCriterion:string|null;
  nextAction:{kind:'wake'|'learn'|'maintain'|'advance';label:string};
};
export type KnowledgeMap = {
  schemaVersion:'personal_capability_map_v2';
  ruleVersion:string;
  projectionRuleVersion:string;
  availability:'ready'|'partial'|'not_ready';
  scope:{seriesId:string|null;series:{id:string;title:string;shelfId:string;shelfName:string}[];definition:string};
  progress:{stagedCapabilities:number;requiredCapabilities:number;coveragePpm:number;activeCapabilities:number;needsWakeCapabilities:number;learningCapabilities:number;stageCounts:Record<string,number>;basis:string};
  learnerProfile:{nodeCount?:number;rankedNodeCount?:number;activeNodeCount?:number;needsAttentionNodeCount?:number;evidenceCount?:number;independentEvidenceCount?:number;profileRuleVersion?:string;sourceObservationWatermark?:number};
  nodes:KnowledgeMapNode[];
  edges:{id:string;from:string;to:string;type:string;label:string}[];
  excluded:{targetWithoutCapabilityCount:number};
  message:string;
};
export type ReviewSession = {
  assignmentId:string;
  status:'started';
  assessmentTargetId:string;
  dueAt:string;
  expiresAt:string;
  quiz:{id:string;questions:Question[]}|null;
  taskPlan:ReviewTaskPlan;
  capabilityTask:CapabilityOpenTask|null;
  attemptId:string|null;
};
export type CapabilityOpenTask = {
  id:string;
  schemaVersion:string;
  taskKind:string;
  stage?:'silver'|'gold'|'diamond';
  prompt:string;
  taskContext:Record<string,unknown>;
  deliverables:string[];
  status?:'ready';
  evidenceEligible:boolean;
  isDemo:boolean;
};
export type ReviewResult = {
  assignmentId:string;
  status:'submitted';
  attemptId:string;
  score:number;
  total:number;
  passed:boolean;
  results:QuizResult['results'];
  retentionQualification:{status:string;ruleVersion:string;reasons:string[]};
  reinforcement:{available:boolean;reason:'wake_failed'|'not_needed'};
};
export type CapabilityReviewResult = {
  schemaVersion:'capability_review_result_v1';
  assignmentId:string;
  submissionId:string;
  status:'submitted';
  verdict:'pass'|'fail';
  evidenceSufficiency:string;
  reactivationQualified:boolean;
  stageChanged:false;
  feedback:string;
};
export type StrengtheningLaunch = {
  schemaVersion:'capability_strengthening_launch_v1';
  assignmentId:string;
  status:'ready'|'unavailable'|'already_achieved';
  reason?:string;
  stage?:'silver'|'gold'|'diamond';
  currentStage?:'unranked'|'bronze'|'silver'|'gold'|'diamond';
  taskKind?:'oral_strengthening'|'application_strengthening'|'transfer_strengthening';
  criterionIds?:string[];
  evidenceEffect?:'may_advance_stage_after_qualified_evidence';
  entry:null|{
    kind:'ask_me'|'standard_application'|'transfer_task';
    seriesId:string;
    sectionId:string;
    label?:string;
    task?:CapabilityOpenTask;
  };
};
export type StrengtheningResult = {
  schemaVersion:'standard_application_result_v1'|'transfer_task_result_v1';
  submissionId:string;
  verdict:'pass'|'fail';
  evidenceSufficiency:string;
  evidenceEligible:boolean;
  capabilityStage:'unranked'|'bronze'|'silver'|'gold'|'diamond';
  feedback:string;
};
export type ReinforcementActivity = {
  activityKey:'diagnose'|'repair'|'recompose'|'verify';
  type:'diagnose'|'repair'|'recompose'|'verify';
  evidenceRole:'diagnostic'|'instructional'|'run_only'|'formal_immediate';
  payload:{
    heading:string;
    prompt?:string;
    content?:string;
    case?:{heading:string;content:string;source:string}|null;
    round?:number;
    options?:{code:string;label:string}[];
    hypothesis?:{
      causeCode:string;
      label:string;
      status:'supported'|'tentative'|'abstained';
      confidence:number;
      evidenceCount:number;
      message:string;
    };
    question?:Question;
  };
};
export type ReinforcementRun = {
  runId:string;
  status:'preparing'|'active'|'completed'|'replan_required';
  state:'prepare'|'diagnose'|'repair'|'recompose'|'verify'|'complete'|'replan_required';
  objective:string;
  entryMode:'wake_failure'|'active_reinforcement';
  progress:{stage:number;totalStages:number;activityCount:number;maxActivities:number;repairRounds:number;maxRepairRounds:number};
  evidenceBoundary:string;
  currentActivity:ReinforcementActivity|null;
  feedback:{kind:string;message:string;correct?:boolean}|null;
  outcome:{kind:'recovered'|'needsReplan';message:string}|null;
};
export type Generation = {id:string;operation:string;attempt:number;status:string;model:string;trace:Record<string,unknown>;errorCode?:string;error?:string;startedAt:string;finishedAt?:string;durationMs:number};
export type SourceVerification = {url:string;reachable:boolean;statusCode:number;pinned:boolean;verificationStatus?:'verified'|'server_unverifiable'|'failed'};
export type Remediation = {id:string;attemptId:string;replacementQuizId:string;blocks:Block[];objectives:string[];strategy:string;sourceVerification:SourceVerification[];sourceLineage:{mode:"generation_trace"|"missing";generationRunId:string|null}};
export type NoteContent = {
  solved_question?:string;
  core_mechanism?:string[];
  personal_gaps?:string[];
  boundaries?:string[];
  practice_checks?:string[];
  sources?:string[];
  unresolved?:string[];
  [key:string]:unknown;
};
export type NoteVerificationAnnotation = {
  assessmentTargetId:string;
  objective:string;
  dimension:string;
  pKnown:number;
  uncertainty:number;
  claimStatus:string;
  retentionRounds:number;
  parameterSetVersion:string;
  projectionRuleVersion:string;
  sourceObservationWatermark:number;
};
export type Note = {
  id:string;
  aiContent:NoteContent;
  userContent:NoteContent;
  version:number;
  layers:{
    learningSummary:null|{
      version:number;
      content:NoteContent;
      sourceContentVersionId:string|null;
      sourceContractVersion:string;
      sourceObservationWatermark:number;
      generationRuleVersion:string;
      createdAt:string;
    };
    reviewSupplements:{
      id:string;
      reviewEpisodeId:string;
      content:NoteContent;
      authorKind:string;
      sourceObservationWatermark:number;
      createdAt:string;
    }[];
    userRevision:null|{
      version:number;
      content:NoteContent;
      basedOnSummaryVersion:number;
      source:string;
      createdAt:string;
    };
  };
  verificationAnnotations:NoteVerificationAnnotation[];
};
export type AskMe = {id:string;status:string;round:number;dimension:string;prompt:string|null;entries:{dimension:string;prompt:string;answer:string|null;evaluation:string;rationale:string}[]};
export type AskMeDiscussionFeedback = {
  evaluation:'strong'|'partial'|'weak';
  correctPoints:string[];
  issues:{
    kind:'factual_error'|'reasoning_gap'|'boundary_missed'|'evidence_insufficient'|'transfer_failure'|'off_topic';
    answerExcerpt:string;
    explanation:string;
  }[];
  suggestions:string[];
  followUpPrompt:string;
  followUpPurpose:string;
  topicSufficiency:'insufficient'|'sufficient';
};
export type AskMeDiscussion = {
  id:string;
  status:'active'|'paused'|'completed';
  revision:number;
  activeTopicId:string;
  pending:boolean;
  schemaVersion:string;
  topics:{
    id:string;
    position:number;
    title:string;
    purpose:string;
    dimension:'mechanism'|'boundary'|'transfer';
    assessmentTargetIds:string[];
    status:'pending'|'active'|'sufficient'|'closed';
    currentPrompt:string;
    turnCount:number;
    evidenceRecorded:boolean;
    finalAssessment:Record<string,unknown>;
  }[];
  turns:{
    id:string;
    topicId:string;
    turnIndex:number;
    prompt:string;
    answer:string;
    evaluation:'strong'|'partial'|'weak';
    feedback:AskMeDiscussionFeedback;
    createdAt:string;
  }[];
};
export type Section = SectionSummary & { dailyModeAtStart?:DailyMode;dailyModeStateVersion?:number;activityStartedAt?:string; generation:null|Generation; content:null|{id:string;version:number;blocks:Block[];sources:Source[];sourceVerification:SourceVerification[];confidence:string;publicationStatus:string;generationMode:'model_only'|'rights_grounded'|'demo';rightsStatus:string;factualStatus:string;aiGenerated:boolean;schemaVersion:string;promptVersion:string;boundaryValidation:{status:'passed'|'legacy'|'unverified';ruleVersion:string|null}}; quiz:null|{id:string;generation:number;publicationStatus:string;questions:Question[];governance:QuizGovernance|null}; latestAttemptReview:QuizResult|null; remediations:Remediation[]; note:null|Note; workflowTasks:LearningTask[] };
export type LearningTask = {
  taskId:string;
  type:'content_feedback_regeneration'|'initial_book_preload'|'note_generation'|'remediation_generation'|'next_section_preload'|'section_lookahead_preload';
  sectionId:string|null;
  triggerId?:string|null;
  status:'pending'|'running'|'succeeded'|'failed';
  attemptCount?:number;
  maxAttempts?:number;
  retryable:boolean;
  errorCode:string|null;
  errorMessage?:string|null;
  result?:Record<string,unknown>;
  createdAt?:string;
  updatedAt?:string;
};
export type KnowledgeRank =
  | 'unranked'
  | 'bronze'
  | 'silver'
  | 'gold'
  | 'platinum'
  | 'diamond'
  | 'master';
export type KnowledgeNodeView = {
  conceptRevisionId:string;
  label:string;
  rank:KnowledgeRank;
  rankOrder:number;
  rankLabel:string;
  meaning:string;
  capabilityScope?:string;
  rankPolicyVersion?:string;
  rankCeiling?:KnowledgeRank;
  rankCeilingLabel?:string;
  atCeiling?:boolean;
  stars:number;
  highestRank:KnowledgeRank;
  highestStars:number;
  activation:'learning'|'active'|'due'|'reassessment';
  stabilityDays:number;
  nextDueAt:string|null;
  evidenceCount:number;
  independentEvidenceCount:number;
  rankRuleVersion:string;
  sourceObservationWatermark:number;
};
export type CapabilityStateView = {
  capabilityRevisionId:string;
  capabilityId:string;
  label:string;
  stage:'unranked'|'bronze'|'silver'|'gold'|'diamond';
  stageOrder:number;
  stageLabel:string;
  highestStage:'unranked'|'bronze'|'silver'|'gold'|'diamond';
  naturalStageCeiling:'bronze'|'silver'|'gold'|'diamond';
  naturalStageCeilingLabel:string;
  activationState:'learning'|'available'|'due_for_reactivation';
  stabilityDays:number;
  nextDueAt:string|null;
  evidenceCount:number;
  independentEvidenceCount:number;
  satisfiedCriterionIds:string[];
  missingCriterionIds:string[];
  nextStage:'bronze'|'silver'|'gold'|'diamond'|null;
  nextCriterion:string|null;
  projectionRuleVersion:string;
  sourceObservationWatermark:number;
};
export type KnowledgeSettlement = {
  schemaVersion:'capability_settlement_v1';
  settlementId:string;
  ruleVersion:string;
  updates:{
    capabilityRevisionId:string;
    label:string;
    before:CapabilityStateView;
    after:CapabilityStateView;
    change:'stage_up'|'evidence_added'|'needs_reactivation'|'reactivated'|'confirmed';
    message:string;
  }[];
};
export type QuizResult = {
  attemptId:string;
  score:number;
  total:number;
  passed:boolean;
  reassessmentEligible:boolean;
  perfect:boolean;
  results:{
    correct:boolean;
    explanation:string;
    objective:string;
    selectedOptions?:number[];
    correctOptions?:number[];
    missedOptions?:number[];
    incorrectOptions?:number[];
  }[];
  knowledgeSettlement?:KnowledgeSettlement|null;
  questions?:Question[];
  remediation:Remediation|null;
  nextQuiz:Section['quiz'];
  workflowTasks:LearningTask[];
  noteGeneration:null|{
    status:string;
    retryable:boolean;
    errorCode:string|null;
    taskId:string;
  };
};
export type QaAnswer = {
  sessionId:string;
  threadId:string;
  answerMessageId:string;
  relation:'follow_up'|'new_question';
  answer:string;
};
export type QaCorrection = {
  threadId:string;
  targetThreadId:string;
  classification:'follow_up';
  corrected:boolean;
};
export type QaHistoryMessage = {
  id:string;
  blockId:string;
  role:'user'|'assistant';
  content:string;
  createdAt:string;
  preferenceRequestEventId?:string|null;
  explanationStyle?:'worked_example'|'diagram'|'analogy'|'derivation'|'precise'|'concise'|'custom'|null;
  explanationBlockKind?:Block['kind']|null;
  requestSource?:'ask_ai'|'explanation_preference';
};
export type QaHistoryThread = {
  threadId:string;
  summary:string;
  relation:'follow_up'|'new_question';
  corrected:boolean;
  createdAt:string;
  updatedAt:string;
  messages:QaHistoryMessage[];
};
export type QaHistory = {
  sectionId:string;
  contentVersionId:string|null;
  currentContentVersionId:string|null;
  readOnly:boolean;
  versions:{contentVersionId:string;contentVersion:number|null;isCurrent:boolean;createdAt:string}[];
  lastThreadId:string|null;
  truncated:boolean;
  threads:QaHistoryThread[];
};
export type ReadingAnnotationAnchor = {
  exact:string;
  prefix:string;
  suffix:string;
  startOffset:number;
  endOffset:number;
};
export type ReadingAnnotation = {
  id:string;
  sectionId:string;
  contentVersionId:string;
  contentVersion:number|null;
  blockId:string;
  displayBlockId:string|null;
  anchorStatus:'current'|'unchanged_in_current'|'old_version';
  kind:'highlight'|'comment';
  anchor:ReadingAnnotationAnchor;
  body:string;
  color:'amber';
  version:number;
  createdAt:string;
  updatedAt:string;
};
export type ReadingAnnotationList = {
  sectionId:string;
  currentContentVersionId:string;
  items:ReadingAnnotation[];
};
