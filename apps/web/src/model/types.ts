export type SectionSummary = { id:string; position:number; title:string; question:string; objectives:string[]; status:string; generated?:boolean; bestScore:number; totalScore:number; askMeUnlocked:boolean };
export type Attachment = { id:string; filename:string; mediaType:string; byteSize:number; sha256:string; createdAt:string };
export type Practice = { id:string; title:string; instructions:Record<string,unknown>; submission:Record<string,unknown>; attachments:Attachment[]; evidenceMode:'file_attachment'|'structured_only_legacy'; status:string };
export type Capstone = { id:string; title:string; brief:Record<string,unknown>; submission:Record<string,unknown>; attachments:Attachment[]; evidenceMode:'file_attachment'|'structured_only_legacy'; status:string };
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
  scope:'global'|'content_block';
  createdAt:string;
  regeneration:{
    status:'recorded_only'|'queued'|'blocked'|'not_applicable';
    reasonCode:string|null;
    task:LearningTask|null;
  };
};
export type Question = {
  prompt:string;
  options:string[];
  core:boolean;
  objective:string;
  assessmentTargetId?:string;
  evidenceBlockIds?:string[];
  selectionMode:'single'|'multiple';
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
  conceptRevisionId:string;
  label:string;
  rank:'unranked'|'bronze'|'silver'|'gold'|'platinum'|'diamond'|'master';
  rankOrder:number;
  rankLabel:string;
  meaning:string;
  capabilityScope:string;
  rankCeiling:string;
  rankCeilingLabel:string;
  atCeiling:boolean;
  stars:number;
  activation:'learning'|'active'|'due'|'reassessment';
  stabilityDays:number;
  nextDueAt:string|null;
  evidenceCount:number;
  independentEvidenceCount:number;
  targetCount:number;
  verifiedTargetCount:number;
  required:boolean;
  routeContexts:{seriesId:string;seriesTitle:string;bookId:string;bookTitle:string;chapterId:string;chapterTitle:string;sectionId:string;sectionTitle:string;required:boolean;contractVersionId:string}[];
  nextAction:{kind:'reinforce'|'wake'|'learn'|'maintain'|'advance';label:string};
};
export type KnowledgeMap = {
  schemaVersion:'personal_knowledge_map_v1';
  ruleVersion:string;
  rankRuleVersion:string;
  availability:'ready'|'partial'|'not_ready';
  scope:{seriesId:string|null;series:{id:string;title:string;shelfId:string;shelfName:string}[];definition:string};
  progress:{verifiedTargets:number;requiredTargets:number;coveragePpm:number;activeNodes:number;needsWakeNodes:number;reassessmentNodes:number;rankCounts:Record<string,number>;basis:string};
  learnerProfile:{nodeCount?:number;rankedNodeCount?:number;activeNodeCount?:number;needsAttentionNodeCount?:number;evidenceCount?:number;independentEvidenceCount?:number;profileRuleVersion?:string;sourceObservationWatermark?:number};
  nodes:KnowledgeMapNode[];
  edges:{id:string;from:string;to:string;type:string;label:string}[];
  excluded:{provisionalTargetCount:number;missingRubricNodeCount:number};
  message:string;
};
export type ReviewSession = {
  assignmentId:string;
  status:'started';
  assessmentTargetId:string;
  dueAt:string;
  expiresAt:string;
  quiz:{id:string;questions:Question[]};
  attemptId:string|null;
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
export type KnowledgeSettlement = {
  settlementId:string;
  ruleVersion:string;
  updates:{
    conceptRevisionId:string;
    label:string;
    before:KnowledgeNodeView;
    after:KnowledgeNodeView;
    change:'rank_up'|'star_up'|'needs_reinforcement'|'reactivated'|'confirmed';
    message:string;
  }[];
};
export type QuizResult = {
  attemptId:string;
  score:number;
  total:number;
  passed:boolean;
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
  lastThreadId:string|null;
  truncated:boolean;
  threads:QaHistoryThread[];
};
