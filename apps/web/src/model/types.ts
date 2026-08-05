export type SectionSummary = { id:string; position:number; title:string; question:string; objectives:string[]; status:string; generated?:boolean; bestScore:number; totalScore:number; askMeUnlocked:boolean };
export type Attachment = { id:string; filename:string; mediaType:string; byteSize:number; sha256:string; createdAt:string };
export type Practice = { id:string; title:string; instructions:Record<string,unknown>; submission:Record<string,unknown>; attachments:Attachment[]; evidenceMode:'file_attachment'|'structured_only_legacy'; status:string };
export type Capstone = { id:string; title:string; brief:Record<string,unknown>; submission:Record<string,unknown>; attachments:Attachment[]; evidenceMode:'file_attachment'|'structured_only_legacy'; status:string };
export type Chapter = { id:string; position:number; title:string; objective:string; status:string; generated:boolean; sections:SectionSummary[]; practice:null|Practice };
export type Book = { id:string; position:number; title:string; description:string; estimatedMinutes:number; status:string; progress:number; practiceProgress:number; chapters:Chapter[]; capstone:null|Capstone };
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
export type ShelfCreateInput = { name:string; domain:string; specialty:string; tags:string[] };
export type ResumePosition = {
  learningRunId:string;
  sectionId:string;
  blockId:string;
  updatedAt:string;
};
export type Bootstrap = {
  user:{id:string;name:string};
  shelves:Shelf[];
  profile:LearningProfile;
  resume:ResumePosition|null;
  milestoneDashboard:MilestoneDashboard;
};
export type LearningPreferences = {
  openingStyle:'auto'|'problem_first'|'example_first'|'concept_first';
  explanationDensity:'auto'|'concise'|'balanced'|'thorough';
  formatPreferences:('diagram'|'worked_example'|'code'|'table'|'analogy')[];
  interactionRhythm:'auto'|'low_interruption'|'balanced'|'frequent_checkins';
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
  onboarding:OnboardingState;
};
export type AuthConfig = {
  mode:'demo'|'local'|'password'|'oidc';
  providerName:string;
};
export type AiRuntime = {
  mode:'provider'|'demo'|'injected';
  configured:boolean;
  model:string;
  providerModel:string;
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
export type Block = { id:string; version:number; kind:string; role:string; heading:string; content:string; source_indexes:number[] };
export type FeedbackReceipt = { id:string; status:'received'; scope:'global'|'content_block'; createdAt:string };
export type Question = { prompt:string; options:string[]; core:boolean; objective:string; selectionMode:'single'|'multiple' };
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
export type Section = SectionSummary & { generation:null|Generation; content:null|{id:string;version:number;blocks:Block[];sources:Source[];sourceVerification:SourceVerification[];confidence:string}; quiz:null|{id:string;generation:number;questions:Question[]}; latestAttemptReview:QuizResult|null; remediations:Remediation[]; note:null|Note; workflowTasks:LearningTask[] };
export type LearningTask = {
  taskId:string;
  type:'initial_book_preload'|'note_generation'|'remediation_generation'|'next_section_preload';
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
  relation:'follow_up'|'new_question';
  answer:string;
};
export type QaCorrection = {
  threadId:string;
  targetThreadId:string;
  classification:'follow_up';
  corrected:boolean;
};
