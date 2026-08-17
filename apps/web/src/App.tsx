import {
  FormEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { MermaidDiagram } from './components/MermaidDiagram';
import { api, ApiError } from './api/client';
import { telemetry } from './telemetry';
import {
  ProfileOnboardingFlow,
  type OnboardingContinuation,
} from './ProfileOnboardingFlow';
import { DailyModeDialog, DailyModeHeader } from './DailyMode';
import {
  LessonReaderHeader,
  LessonReaderTabs,
  type ReaderTab,
} from './features/lesson/ReaderChrome';
import {
  type ExplanationStyle,
  type PresetExplanationStyle,
} from './features/lesson/LessonBlockTools';
import { LessonBlockBody, LessonContentBlock } from './features/lesson/LessonContentBlock';
import { PathDecisionBanner } from './features/learning/PathDecisionBanner';
import { useStudyActivity } from './features/study/useStudyActivity';
import { AppBusyStatus, AppStatusRegion } from './features/shell/AppStatusRegion';
import { useModalFocus } from './features/system/useModalFocus';
import type {
  AskMeDiscussion,
  AiRuntime,
  AuthConfig,
  AuthState,
  Block,
  Book,
  BookReplanProposal,
  BookSettlement,
  Bootstrap,
  AccountExitReceipt,
  PrivacyState,
  Question,
  RegistrationResult,
  Chapter,
  ChapterChallenge,
  ChapterChallengeResult,
  DueReviews,
  DailyMode,
  DailyModeDuration,
  DailyModeSource,
  LearningTask,
  LearningProfile,
  LearningPreferences,
  LearningGoalInterview,
  LearningGoalInterviewAnswer,
  LearningStartPreference,
  LearningStartPreview,
  KnowledgeSettlement,
  KnowledgeMap,
  KnowledgeMapNode,
  Note as NoteType,
  NoteContent,
  NoteVerificationAnnotation,
  QaHistory,
  ReadingAnnotation,
  ReadingAnnotationAnchor,
  QuizResult,
  CapabilityReviewResult,
  ReviewResult,
  ReviewSession,
  ReinforcementRun,
  StrengtheningLaunch,
  StrengtheningResult,
  Section,
  SectionSummary,
  Series,
  SeriesRenameInput,
  Shelf,
  ShelfCreateInput,
  ShelfRenameInput,
  StudyActivitySummary,
} from './model/types';

type View = 'home' | 'shelf' | 'series-create' | 'learn' | 'profile' | 'knowledge' | 'review';
type AppRoute =
  | { view: 'home' }
  | { view: 'profile'; section: 'profile' | 'account' }
  | { view: 'knowledge' }
  | { view: 'review' }
  | { view: 'shelf'; shelfId: string }
  | { view: 'series-create'; shelfId: string }
  | { view: 'learn'; seriesId: string; sectionId: string | null };
type TextQuote = { text: string; blockId: string };
type SelectionPopup = TextQuote & {
  contentVersionId: string;
  anchor: ReadingAnnotationAnchor;
  top: number;
  left: number;
  placement: 'above' | 'below';
  composerTop: number;
};
type AnnotationComposerDraft = {
  selection: SelectionPopup;
  body: string;
};
type BookReplanState = {
  book: Book;
  proposal: BookReplanProposal | null;
  status: 'preparing' | 'ready' | 'failed';
  feedback: string;
  previousProposalId?: string;
};
type FirstSeriesSeed = {
  shelfId: string;
  topic: string;
  experience: string;
};
type ExplanationRequest = {
  requestId: string;
  blockId: string;
  blockKind: Block['kind'];
  style: ExplanationStyle;
  label: string;
  question: string;
  displayQuestion: string;
  evidenceEventId?: string;
  preferenceStatus: 'saved' | 'unsaved' | 'saving';
  customInstruction?: string;
};
type FeedbackTarget =
  | { scope: 'global' }
  | {
      scope: 'content_block';
      sectionId: string;
      contentVersionId: string;
      block: Block;
    };
const AI_RUNTIME_SETTINGS_ENABLED = import.meta.env.VITE_INTERNAL_AI_SETTINGS === 'true';
type AuthPanel = 'login' | 'register' | 'recover';
type ProgressNotice = { title: string; message: string };

function generationProgressNotice(reason: ApiError): ProgressNotice {
  if (reason.message.includes('本章')) {
    return {
      title: '本章正在生成',
      message: '通常需要 1–3 分钟，请耐心等待。完成后会自动刷新目录。',
    };
  }
  return {
    title: '本节正在准备',
    message: '通常需要 1–2 分钟。完成后会自动更新，无需重复点击。',
  };
}

function domRangeFromTextOffsets(root: HTMLElement, start: number, end: number): Range | null {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let cursor = 0;
  let startNode: Text | null = null;
  let endNode: Text | null = null;
  let startInNode = 0;
  let endInNode = 0;
  while (walker.nextNode()) {
    const node = walker.currentNode as Text;
    const next = cursor + node.data.length;
    if (!startNode && start >= cursor && start <= next) {
      startNode = node;
      startInNode = Math.min(start - cursor, node.data.length);
    }
    if (endNode === null && end >= cursor && end <= next) {
      endNode = node;
      endInNode = Math.min(end - cursor, node.data.length);
      break;
    }
    cursor = next;
  }
  if (!startNode || !endNode) return null;
  const range = document.createRange();
  range.setStart(startNode, startInNode);
  range.setEnd(endNode, endInNode);
  return range.collapsed ? null : range;
}

function annotationRange(root: HTMLElement, anchor: ReadingAnnotationAnchor): Range | null {
  const text = root.textContent || '';
  if (text.slice(anchor.startOffset, anchor.endOffset) === anchor.exact) {
    return domRangeFromTextOffsets(root, anchor.startOffset, anchor.endOffset);
  }
  const candidates: number[] = [];
  let cursor = text.indexOf(anchor.exact);
  while (cursor >= 0) {
    const prefixMatches = !anchor.prefix
      || text.slice(Math.max(0, cursor - anchor.prefix.length), cursor) === anchor.prefix;
    const suffixStart = cursor + anchor.exact.length;
    const suffixMatches = !anchor.suffix
      || text.slice(suffixStart, suffixStart + anchor.suffix.length) === anchor.suffix;
    if (prefixMatches && suffixMatches) candidates.push(cursor);
    cursor = text.indexOf(anchor.exact, cursor + Math.max(anchor.exact.length, 1));
  }
  if (candidates.length !== 1) return null;
  return domRangeFromTextOffsets(
    root,
    candidates[0],
    candidates[0] + anchor.exact.length,
  );
}

function RecoveryCodePanel({
  code,
  renewed,
  onContinue,
}: {
  code:string;
  renewed:boolean;
  onContinue:()=>void;
}) {
  const [copied, setCopied] = useState(false);
  const copyCode = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };
  return (
    <div className="recovery-code-panel">
      <div className="recovery-key-mark" aria-hidden="true"><i /><i /><i /></div>
      <p className="eyebrow">只展示这一次</p>
      <h2>{renewed ? '新恢复码已生成' : '把备用钥匙收好'}</h2>
      <p>
        {renewed
          ? '旧恢复码已经失效。请保存这份新恢复码，再使用新密码登录。'
          : 'Slow 不绑定邮箱或手机。忘记密码时，这串恢复码是确认账号归属的唯一凭证。'}
      </p>
      <div className="recovery-code-value" aria-label="账号恢复码">{code}</div>
      <button type="button" className="recovery-copy-button" onClick={() => void copyCode()}>
        {copied ? '已复制恢复码' : '复制恢复码'}
      </button>
      <small>密码和恢复码同时遗失后，将无法自助恢复账号。</small>
      <button type="button" className="auth-submit" onClick={onContinue}>
        {renewed ? '返回登录' : '我已保存，继续'} <span>→</span>
      </button>
    </div>
  );
}

function PasswordVisibilityToggle({
  visible,
  onToggle,
}: {
  visible:boolean;
  onToggle:()=>void;
}) {
  return (
    <button
      type="button"
      aria-label={visible ? '隐藏密码' : '显示密码'}
      aria-pressed={visible}
      onClick={onToggle}
    >
      {visible ? (
        <svg aria-hidden="true" viewBox="0 0 24 24" fill="none">
          <path d="m3 3 18 18M10.6 10.7a2 2 0 0 0 2.7 2.7M9.9 4.3A10.7 10.7 0 0 1 12 4c5.5 0 9 5.5 9 5.5a16 16 0 0 1-2.2 2.7M6.6 6.6C4.3 8.1 3 9.5 3 9.5S6.5 15 12 15c1 0 1.9-.2 2.7-.5" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      ) : (
        <svg aria-hidden="true" viewBox="0 0 24 24" fill="none">
          <path d="M3 9.5S6.5 4 12 4s9 5.5 9 5.5S17.5 15 12 15 3 9.5 3 9.5Z" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round" />
          <circle cx="12" cy="9.5" r="2.5" stroke="currentColor" strokeWidth="1.7" />
        </svg>
      )}
    </button>
  );
}

function AppLoadingScreen({ message }: { message: string }) {
  return (
    <div className="app-shell app-loading-shell">
      <div className="app-loading-card" role="status" aria-live="polite" aria-atomic="true">
        <span className="brand" aria-hidden="true">
          <span className="brand-mark"><i /></span>
          <b>slow</b>
        </span>
        <span className="app-loading-progress" aria-hidden="true"><i /></span>
        <p>{message}</p>
      </div>
    </div>
  );
}

const routeFromLocation = (): AppRoute => {
  const parts = window.location.pathname.split('/').filter(Boolean).map((part) => {
    try {
      return decodeURIComponent(part);
    } catch {
      return part;
    }
  });
  if (parts[0] === 'profile' && parts.length === 1) {
    return {
      view: 'profile',
      section: new URLSearchParams(window.location.search).get('section') === 'account'
        ? 'account'
        : 'profile',
    };
  }
  if (parts[0] === 'knowledge' && parts.length === 1) return { view: 'knowledge' };
  if (parts[0] === 'review' && parts.length === 1) return { view: 'review' };
  if (parts[0] === 'shelves' && parts[1] && parts[2] === 'series' && parts[3] === 'new') {
    return { view: 'series-create', shelfId: parts[1] };
  }
  if (parts[0] === 'shelves' && parts[1] && parts.length === 2) {
    return { view: 'shelf', shelfId: parts[1] };
  }
  if (parts[0] === 'series' && parts[1]) {
    const sectionId = parts[2] === 'sections' && parts[3] ? parts[3] : null;
    return { view: 'learn', seriesId: parts[1], sectionId };
  }
  return { view: 'home' };
};

const shelfPath = (shelfId: string) => `/shelves/${encodeURIComponent(shelfId)}`;
const seriesCreatePath = (shelfId: string) => `${shelfPath(shelfId)}/series/new`;
const seriesPath = (seriesId: string, sectionId?: string | null) => (
  sectionId
    ? `/series/${encodeURIComponent(seriesId)}/sections/${encodeURIComponent(sectionId)}`
    : `/series/${encodeURIComponent(seriesId)}`
);

const updateBrowserLocation = (path: string, mode: 'push' | 'replace' | 'none') => {
  if (mode === 'none') return;
  const current = `${window.location.pathname}${window.location.search}`;
  if (current === path) return;
  window.history[mode === 'push' ? 'pushState' : 'replaceState']({}, '', path);
};

const GENERATION_STAGE_LABELS: Record<string, string> = {
  queued: '正在等待开始',
  teaching_blueprint: '正在准备学习内容',
  content_generation: '正在准备学习内容',
  combined_generation: '正在准备学习内容',
  source_verification: '正在检查内容',
  source_verification_degraded: '正在检查内容',
  source_repair: '正在检查内容',
  source_repair_rejected: '正在检查内容',
  quiz_generation: '正在准备验证题',
  semantic_alignment_review: '正在检查内容',
  semantic_alignment_rejected: '正在检查内容',
  semantic_claim_verification: '正在检查内容',
  persistence: '正在完成',
  persisted: '已经完成',
  failed: '准备失败',
};

type FailureWithCode = { errorCode?: string | null } | null | undefined;

const isAiGenerationFailure = (failure: FailureWithCode) => (
  failure?.errorCode?.startsWith('AI_') === true
);

const generationFailureMessage = (
  failure: FailureWithCode,
  subject = '本节内容',
) => (
  isAiGenerationFailure(failure)
    ? `AI 本次没有完成${subject}生成，未完成的内容不会发布。请稍后重新准备。`
    : `${subject}本次没有准备完成，未完成的内容不会发布。请稍后重新准备。`
);

const formatElapsed = (milliseconds: number) => {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000));
  if (seconds < 60) return `${seconds} 秒`;
  return `${Math.floor(seconds / 60)} 分 ${String(seconds % 60).padStart(2, '0')} 秒`;
};

type QaExchange = {
  id: string;
  threadId?: string;
  answerMessageId?: string;
  blockId?: string;
  question: string;
  answer: string;
  relation: string;
  status: 'streaming' | 'done' | 'error';
  explanationStyle?: ExplanationStyle;
  preferenceRequestEventId?: string;
  explanationBlockKind?: Block['kind'];
  preferenceStatus?: 'saved' | 'unsaved';
};

const EXPLANATION_STYLE_OPTIONS: Record<PresetExplanationStyle, {
  label: string;
  prompt: string;
}> = {
  worked_example: {
    label: '举个具体例子',
    prompt: '请用一个具体、可观察或可计算的例子重新解释这一段，并说明例子中的对象、过程和结论。',
  },
  diagram: {
    label: '画成关系图',
    prompt: '请把这一段整理成清晰的文本图解或关系图，并在图后用两三句话解释关键关系。',
  },
  analogy: {
    label: '打个贴切比方',
    prompt: '请先用一个贴切、容易形成直觉的类比重新解释这一段，再说明这个类比在哪些地方会失效。',
  },
  derivation: {
    label: '展开推导过程',
    prompt: '请把这一段涉及的推理或公式一步步展开，不跳步骤，并说明每一步为什么成立。',
  },
  precise: {
    label: '说得更严谨',
    prompt: '请用准确的定义、成立条件和失效边界重新解释这一段，避免模糊表述。',
  },
  concise: {
    label: '压缩成要点',
    prompt: '请用一句结论和不超过三个要点简洁解释这一段，同时保留必要的成立条件。',
  },
};

const explanationOptionsForBlock = (kind: Block['kind']) => {
  const preferred: PresetExplanationStyle[] = kind === 'formula'
    ? ['derivation', 'worked_example', 'precise', 'diagram', 'concise', 'analogy']
    : kind === 'diagram' || kind === 'table'
      ? ['worked_example', 'precise', 'concise', 'analogy', 'derivation', 'diagram']
      : kind === 'code'
        ? ['worked_example', 'derivation', 'precise', 'concise', 'diagram', 'analogy']
        : ['worked_example', 'diagram', 'analogy', 'precise', 'derivation', 'concise'];
  return preferred.map((style) => ({ style, ...EXPLANATION_STYLE_OPTIONS[style] }));
};

const qaQuestionForDisplay = (content: string) => {
  const quotedQuestion = content.match(
    /^请基于以下选中的正文回答。\n\n选中内容：[\s\S]*?\n\n问题：([\s\S]+)$/,
  );
  const question = quotedQuestion?.[1]?.trim() || content;
  const preset = Object.values(EXPLANATION_STYLE_OPTIONS).find((option) => option.prompt === question);
  if (preset) return preset.label;
  return question.replace(/^请按这个要求重新解释当前段落：/, '按这个讲：');
};

const qaHistoryExchanges = (history: QaHistory): QaExchange[] => {
  const exchanges: QaExchange[] = [];
  history.threads.forEach((thread) => {
    let activeExchange: QaExchange | undefined;
    thread.messages.forEach((message) => {
      if (message.role === 'user') {
        activeExchange = {
          id: message.id,
          threadId: thread.threadId,
          blockId: message.blockId,
          question: qaQuestionForDisplay(message.content),
          answer: '',
          relation: thread.relation,
          status: 'done',
        };
        exchanges.push(activeExchange);
        return;
      }
      if (activeExchange) {
        activeExchange.answer += message.content;
        activeExchange.answerMessageId = message.id;
        if (message.preferenceRequestEventId && message.explanationStyle && message.explanationBlockKind) {
          activeExchange.preferenceRequestEventId = message.preferenceRequestEventId;
          activeExchange.explanationStyle = message.explanationStyle;
          activeExchange.explanationBlockKind = message.explanationBlockKind;
        }
      }
    });
  });
  return exchanges.map((exchange) => exchange.answer
    ? exchange
    : { ...exchange, answer: '这次回答没有完整保存。', status: 'error' });
};

export default function App() {
  const [auth, setAuth] = useState<AuthState | null>(null);
  const [authConfig, setAuthConfig] = useState<AuthConfig | null>(null);
  const [authChecked, setAuthChecked] = useState(false);
  const [localUsername, setLocalUsername] = useState('');
  const [localPassword, setLocalPassword] = useState('');
  const [showLocalPassword, setShowLocalPassword] = useState(false);
  const [authPanel, setAuthPanel] = useState<AuthPanel>('login');
  const [registrationUsername, setRegistrationUsername] = useState('');
  const [registrationPassword, setRegistrationPassword] = useState('');
  const [registrationPasswordConfirm, setRegistrationPasswordConfirm] = useState('');
  const [showRegistrationPassword, setShowRegistrationPassword] = useState(false);
  const [showRegistrationPasswordConfirm, setShowRegistrationPasswordConfirm] = useState(false);
  const [alphaCode, setAlphaCode] = useState('');
  const [registrationResult, setRegistrationResult] = useState<RegistrationResult | null>(null);
  const [recoveryUsername, setRecoveryUsername] = useState('');
  const [recoveryCode, setRecoveryCode] = useState('');
  const [recoveryPassword, setRecoveryPassword] = useState('');
  const [recoveryPasswordConfirm, setRecoveryPasswordConfirm] = useState('');
  const [showRecoveryPassword, setShowRecoveryPassword] = useState(false);
  const [showRecoveryPasswordConfirm, setShowRecoveryPasswordConfirm] = useState(false);
  const [renewedRecoveryCode, setRenewedRecoveryCode] = useState('');
  const [data, setData] = useState<Bootstrap | null>(null);
  const [view, setView] = useState<View>(() => routeFromLocation().view);
  const [restoringInitialRoute, setRestoringInitialRoute] = useState(
    () => routeFromLocation().view !== 'home',
  );
  const [shelf, setShelf] = useState<Shelf | null>(null);
  const [series, setSeries] = useState<Series | null>(null);
  const [section, setSection] = useState<Section | null>(null);
  const [directoryBookId, setDirectoryBookId] = useState('');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [progressNotice, setProgressNotice] = useState<ProgressNotice | null>(null);
  const [showAiSettings, setShowAiSettings] = useState(false);
  const [feedbackTarget, setFeedbackTarget] = useState<FeedbackTarget | null>(null);
  const [bookReplan, setBookReplan] = useState<BookReplanState | null>(null);
  const [learningQaOpen, setLearningQaOpen] = useState(false);
  const [showUserMenu, setShowUserMenu] = useState(false);
  const [exitReceipt, setExitReceipt] = useState<AccountExitReceipt | null>(null);
  const [profileSection, setProfileSection] = useState<'profile' | 'account'>(() => (
    new URLSearchParams(window.location.search).get('section') === 'account' ? 'account' : 'profile'
  ));
  const [preparingInitialSection, setPreparingInitialSection] = useState(false);
  const [firstSeriesSeed, setFirstSeriesSeed] = useState<FirstSeriesSeed | null>(null);
  const [dailyModeDialogOpen, setDailyModeDialogOpen] = useState(false);
  const [dailyModeBusy, setDailyModeBusy] = useState(false);
  const [activityDailyMode, setActivityDailyMode] = useState<DailyMode | null>(null);
  const [dailyModeExpiredDuringActivity, setDailyModeExpiredDuringActivity] = useState(false);
  const [pendingSectionId, setPendingSectionId] = useState('');
  const [generatingChapterId, setGeneratingChapterId] = useState('');
  const [dismissedPathAlertSeriesId, setDismissedPathAlertSeriesId] = useState('');
  const chapterGenerationRequests = useRef(new Set<string>());
  const userMenuRef = useRef<HTMLDivElement | null>(null);
  const lastViewedSection = useRef('');
  const restoreLocationRef = useRef<() => Promise<void>>(async () => undefined);
  const routeRequestVersion = useRef(0);
  const routeInitializedForUser = useRef('');
  const initialSectionMonitorVersion = useRef(0);
  const bookReplanRequestVersion = useRef(0);

  const hasActiveDailyMode = () => Boolean(
    data?.dailyMode?.active
    && data.dailyMode.expiresAt
    && new Date(data.dailyMode.expiresAt).getTime() > Date.now(),
  );

  const dailyModePromptEnabled = data?.profile.preferences.dailyModePromptEnabled ?? false;

  const loadAuthenticatedState = async () => {
    const value = await api.authMe();
    setAuth(value);
    if (value.privacy.required) {
      setData(null);
      return;
    }
    if (value.onboarding.required) {
      setData(null);
      return;
    }
    setData(await api.bootstrap());
  };

  const initializeAuth = async () => {
    setAuthChecked(false);
    setError('');
    try {
      const config = await api.authConfig();
      setAuthConfig(config);
      const demoEntered = sessionStorage.getItem('slow_demo_entered') === 'true';
      if (config.mode === 'oidc' || config.mode === 'local' || config.mode === 'password' || demoEntered) {
        await loadAuthenticatedState();
      } else {
        setAuth(null);
        setData(null);
      }
    } catch (reason) {
      if ((reason as { status?: number })?.status !== 401) {
        setError(reason instanceof Error ? reason.message : '无法连接登录服务');
      }
      setAuth(null);
    } finally {
      setAuthChecked(true);
    }
  };

  useEffect(() => {
    if (!notice) return undefined;
    const timer = window.setTimeout(() => setNotice(''), 4800);
    return () => window.clearTimeout(timer);
  }, [notice]);

  useEffect(() => {
    const clearUserState = () => {
      routeInitializedForUser.current = '';
      setAuth(null);
      setData(null);
      setShelf(null);
      setSeries(null);
      setSection(null);
      setFirstSeriesSeed(null);
      setView('home');
      setRestoringInitialRoute(false);
      setAuthPanel('login');
      setShowUserMenu(false);
      window.history.replaceState({}, '', '/');
      setAuthChecked(true);
    };
    api.setUnauthorizedHandler(clearUserState);
    telemetry.start();
    void initializeAuth();
    const handleHistory = () => { void restoreLocationRef.current(); };
    window.addEventListener('popstate', handleHistory);
    return () => window.removeEventListener('popstate', handleHistory);
  }, []);

  useEffect(() => {
    const enabled = auth?.privacy.status === 'accepted';
    telemetry.setEnabled(enabled);
    if (!enabled || !data) return;
    if (view === 'home') {
      telemetry.track('home_viewed', { view: 'home' });
    } else if (view === 'shelf' && shelf) {
      telemetry.track('shelf_viewed', { view: 'shelf', entityType: 'shelf', entityId: shelf.id });
    } else if (view === 'series-create' && shelf) {
      telemetry.track('series_creation_viewed', { view: 'series-create', entityType: 'shelf', entityId: shelf.id });
    } else if (view === 'learn' && series) {
      telemetry.track('learning_viewed', { view: 'learn', entityType: 'series', entityId: series.id });
    } else if (view === 'profile') {
      telemetry.track('profile_viewed', { view: 'profile' });
    } else if (view === 'review') {
      telemetry.track('review_center_viewed', { view: 'review' });
    }
  }, [auth?.privacy.status, data, view, shelf?.id, series?.id]);

  useEffect(() => {
    if (view !== 'learn' || !section?.content || auth?.privacy.status !== 'accepted') {
      if (view !== 'learn') lastViewedSection.current = '';
      return;
    }
    if (lastViewedSection.current === section.id) return;
    lastViewedSection.current = section.id;
    telemetry.track('section_viewed', {
      view: 'learn',
      entityType: 'section',
      entityId: section.id,
    });
  }, [auth?.privacy.status, view, section?.id, section?.content?.id]);

  useEffect(() => {
    if (view !== 'learn' || !section?.content || auth?.privacy.status !== 'accepted') return;
    let activeSeconds = 0;
    let lastInteractionAt = Date.now();
    const markInteraction = () => { lastInteractionAt = Date.now(); };
    const timer = window.setInterval(() => {
      const active = document.visibilityState === 'visible'
        && document.hasFocus()
        && Date.now() - lastInteractionAt <= 30_000;
      if (!active) return;
      activeSeconds += 1;
      if (activeSeconds % 60 === 0) {
        telemetry.track('active_reading_60s', {
          view: 'learn',
          entityType: 'section',
          entityId: section.id,
          properties: { seconds: 60 },
        });
      }
    }, 1_000);
    document.addEventListener('pointerdown', markInteraction, true);
    document.addEventListener('keydown', markInteraction, true);
    document.addEventListener('scroll', markInteraction, true);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('pointerdown', markInteraction, true);
      document.removeEventListener('keydown', markInteraction, true);
      document.removeEventListener('scroll', markInteraction, true);
    };
  }, [auth?.privacy.status, view, section?.id, section?.content?.id]);

  useEffect(() => {
    if (!feedbackTarget || auth?.privacy.status !== 'accepted') return;
    telemetry.track('feedback_opened', {
      view,
      entityType: feedbackTarget.scope === 'content_block' ? 'section' : '',
      entityId: feedbackTarget.scope === 'content_block' ? feedbackTarget.sectionId : '',
      properties: { scope: feedbackTarget.scope },
    });
  }, [auth?.privacy.status, feedbackTarget, view]);

  useEffect(() => {
    if (!showUserMenu) return;
    const closeMenu = (event: MouseEvent) => {
      if (!userMenuRef.current?.contains(event.target as Node)) setShowUserMenu(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setShowUserMenu(false);
    };
    document.addEventListener('mousedown', closeMenu);
    document.addEventListener('keydown', closeOnEscape);
    return () => {
      document.removeEventListener('mousedown', closeMenu);
      document.removeEventListener('keydown', closeOnEscape);
    };
  }, [showUserMenu]);

  useEffect(() => {
    const state = data?.dailyMode;
    if (!state) return;
    if (!state.active || !state.expiresAt) {
      return;
    }
    setDailyModeDialogOpen(false);
    const expire = () => {
      setData((current) => current ? {
        ...current,
        dailyMode: {
          ...current.dailyMode,
          active: false,
          dailyMode: null,
          duration: null,
          activatedAt: null,
          expiresAt: null,
          serverNow: new Date().toISOString(),
        },
      } : current);
      if (view === 'learn' && section && activityDailyMode) {
        setDailyModeExpiredDuringActivity(true);
      }
    };
    const remaining = new Date(state.expiresAt).getTime() - Date.now();
    if (remaining <= 0) {
      expire();
      return;
    }
    const timer = window.setTimeout(expire, Math.min(remaining, 2_147_000_000));
    return () => window.clearTimeout(timer);
  }, [data?.dailyMode?.version, data?.dailyMode?.active, data?.dailyMode?.expiresAt, view, section?.id, activityDailyMode]);

  const run = async <T,>(label: string, action: () => Promise<T>) => {
    setBusy(label);
    setError('');
    setProgressNotice(null);
    try {
      return await action();
    } catch (reason) {
      if (reason instanceof ApiError && reason.code === 'GENERATION_IN_PROGRESS') {
        setProgressNotice(generationProgressNotice(reason));
      } else {
        setError(reason instanceof Error ? reason.message : '操作失败');
      }
      throw reason;
    } finally {
      setBusy('');
    }
  };

  const loginWithLocalAccount = async (event: FormEvent) => {
    event.preventDefault();
    setBusy('正在登录…');
    setError('');
    try {
      const mode = authConfig?.mode === 'password' ? 'password' : 'local';
      const state = await api.credentialsLogin(mode, localUsername, localPassword);
      setAuth(state);
      setData(state.privacy.required || state.onboarding.required ? null : await api.bootstrap());
      setLocalPassword('');
      setShowLocalPassword(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '登录失败');
    } finally {
      setBusy('');
    }
  };

  const switchAuthPanel = (panel: AuthPanel) => {
    setAuthPanel(panel);
    setError('');
    setRenewedRecoveryCode('');
  };

  const registerAlphaAccount = async (event: FormEvent) => {
    event.preventDefault();
    setBusy('正在创建账号…');
    setError('');
    try {
      const state = await api.registerAccount({
        username: registrationUsername,
        password: registrationPassword,
        passwordConfirm: registrationPasswordConfirm,
        alphaCode,
      });
      setRegistrationResult(state);
      setRegistrationPassword('');
      setRegistrationPasswordConfirm('');
      setShowRegistrationPassword(false);
      setShowRegistrationPasswordConfirm(false);
      setAlphaCode('');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '账号创建失败');
    } finally {
      setBusy('');
    }
  };

  const resetPasswordWithRecoveryCode = async (event: FormEvent) => {
    event.preventDefault();
    setBusy('正在重置密码…');
    setError('');
    try {
      const result = await api.resetPasswordWithRecovery({
        username: recoveryUsername,
        recoveryCode,
        newPassword: recoveryPassword,
        newPasswordConfirm: recoveryPasswordConfirm,
      });
      setRenewedRecoveryCode(result.recoveryCode);
      setLocalUsername(recoveryUsername);
      setRecoveryCode('');
      setRecoveryPassword('');
      setRecoveryPasswordConfirm('');
      setShowRecoveryPassword(false);
      setShowRecoveryPasswordConfirm(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '密码重置失败');
    } finally {
      setBusy('');
    }
  };

  const openShelf = (value: Shelf, historyMode: 'push' | 'replace' | 'none' = 'push') => {
    if (historyMode !== 'none') routeRequestVersion.current += 1;
    updateBrowserLocation(shelfPath(value.id), historyMode);
    setShelf(value);
    setSeries(null);
    setSection(null);
    setFirstSeriesSeed(null);
    setView('shelf');
  };

  const openSeriesCreation = (
    value: Shelf,
    historyMode: 'push' | 'replace' | 'none' = 'push',
    seed?: FirstSeriesSeed,
  ) => {
    if (historyMode !== 'none') routeRequestVersion.current += 1;
    updateBrowserLocation(seriesCreatePath(value.id), historyMode);
    if (seed) setFirstSeriesSeed(seed);
    else if (historyMode !== 'none') setFirstSeriesSeed(null);
    setShelf(value);
    setSeries(null);
    setSection(null);
    setView('series-create');
  };

  const logout = async () => {
    try {
      await api.logout();
    } finally {
      sessionStorage.removeItem('slow_demo_entered');
      setAuth(null);
      setData(null);
      setShelf(null);
      setSeries(null);
      setSection(null);
      setFirstSeriesSeed(null);
      setView('home');
      setAuthPanel('login');
      setShowUserMenu(false);
      window.history.replaceState({}, '', '/');
    }
  };

  const acceptPrivacy = async () => {
    const privacy = await api.acceptPrivacy({ privacyAccepted: true, trialAccepted: true });
    setAuth((current) => current ? { ...current, privacy } : current);
    await loadAuthenticatedState();
  };

  const continueFromOnboarding = async (continuation: OnboardingContinuation) => {
    const nextAuth = await api.authMe();
    const nextData = await api.bootstrap();
    const firstShelf = nextData.shelves.find((item) => (
      item.id === continuation.firstShelfId
    )) || null;
    setAuth(nextAuth);
    setData(nextData);
    if (firstShelf) {
      openSeriesCreation(firstShelf, 'replace', {
        shelfId: firstShelf.id,
        topic: continuation.topic,
        experience: continuation.experience,
      });
      return;
    }
    routeRequestVersion.current += 1;
    updateBrowserLocation('/', 'replace');
    setShelf(null);
    setSeries(null);
    setSection(null);
    setFirstSeriesSeed(null);
    setView('home');
  };

  const requestAccountExit = async (confirmation: string, reason: string) => {
    const receipt = await api.requestAccountExit({ confirmation, reason });
    setExitReceipt(receipt);
    setAuth(null);
    setData(null);
    setShelf(null);
    setSeries(null);
    setSection(null);
    setShowUserMenu(false);
    window.history.replaceState({}, '', '/');
  };

  const showHome = (historyMode: 'push' | 'replace' | 'none') => {
    if (historyMode !== 'none') routeRequestVersion.current += 1;
    updateBrowserLocation('/', historyMode);
    setShowUserMenu(false);
    setView('home');
    setShelf(null);
    setSeries(null);
    setSection(null);
    setActivityDailyMode(null);
    setDailyModeExpiredDuringActivity(false);
    setDailyModeDialogOpen(false);
    void api.bootstrap()
      .then(setData)
      .catch((reason) => setError(reason instanceof Error ? reason.message : '主页刷新失败'));
  };

  const goHome = () => showHome('push');

  const returnToShelf = async () => {
    const shelfId = shelf?.id;
    if (!shelfId) {
      goHome();
      return;
    }
    try {
      const refreshed = await run('正在返回书架…', () => api.bootstrap());
      const refreshedShelf = refreshed.shelves.find((item) => item.id === shelfId) || null;
      setData(refreshed);
      setShelf(refreshedShelf);
      routeRequestVersion.current += 1;
      updateBrowserLocation(refreshedShelf ? shelfPath(shelfId) : '/', 'push');
      setShowUserMenu(false);
      setSeries(null);
      setSection(null);
      setActivityDailyMode(null);
      setDailyModeExpiredDuringActivity(false);
      setView(refreshedShelf ? 'shelf' : 'home');
    } catch {
      // Stay in the current learning view so stale shelf data is never presented.
    }
  };

  const openProfileCenter = (nextSection: 'profile' | 'account' = 'profile') => {
    routeRequestVersion.current += 1;
    const nextUrl = nextSection === 'account' ? '/profile?section=account' : '/profile';
    if (`${window.location.pathname}${window.location.search}` !== nextUrl) window.history.pushState({}, '', nextUrl);
    setProfileSection(nextSection);
    setShowUserMenu(false);
    setShelf(null);
    setSeries(null);
    setSection(null);
    setView('profile');
  };

  const openKnowledgeMap = () => {
    routeRequestVersion.current += 1;
    updateBrowserLocation('/knowledge', 'push');
    setShowUserMenu(false);
    setShelf(null);
    setSeries(null);
    setSection(null);
    setView('knowledge');
  };

  const openReviewCenter = () => {
    routeRequestVersion.current += 1;
    updateBrowserLocation('/review', 'push');
    setShowUserMenu(false);
    setShelf(null);
    setSeries(null);
    setSection(null);
    setView('review');
  };

  const changeProfileSection = (nextSection: 'profile' | 'account') => {
    const nextUrl = nextSection === 'account' ? '/profile?section=account' : '/profile';
    window.history.replaceState({}, '', nextUrl);
    setProfileSection(nextSection);
  };

  const applyOpenedSection = (value: Section, sectionId: string) => {
    setActivityDailyMode(
      value.dailyModeAtStart
      || data?.dailyMode?.dailyMode
      || data?.dailyMode?.lastDailyMode
      || 'slow',
    );
    setDailyModeExpiredDuringActivity(false);
    void api.updateResume(sectionId).catch(() => undefined);
  };

  const openAndTrackSection = async (sectionId: string) => {
    try {
      const value = await api.openSection(sectionId);
      applyOpenedSection(value, sectionId);
      return value;
    } catch (reason) {
      if (
        reason instanceof ApiError
        && reason.code === 'SECTION_CANDIDATE_INCOMPLETE'
      ) {
        // Opening is intentionally fail-closed until a complete content/quiz
        // pair can be frozen. The read-only section view is still safe to show
        // and contains the audited generation failure plus its retry action.
        return api.section(sectionId);
      }
      throw reason;
    }
  };

  const loadSection = async (
    sectionId: string,
    historyMode: 'push' | 'replace' | 'none' = 'push',
    promptForDailyMode = true,
  ) => {
    if (series) updateBrowserLocation(seriesPath(series.id, sectionId), historyMode);
    if (promptForDailyMode && dailyModePromptEnabled && !hasActiveDailyMode()) {
      setPendingSectionId(sectionId);
      setDailyModeDialogOpen(true);
      if (section) return section;
      return api.section(sectionId);
    }
    const value = await run(
      '正在读取小节…',
      () => openAndTrackSection(sectionId),
    );
    const targetBook = series?.books.find((book) => bookContainsSection(book, sectionId));
    if (targetBook) setDirectoryBookId(targetBook.id);
    setSection(value);
    return value;
  };

  const activateDailyMode = async (
    mode: DailyMode,
    duration: DailyModeDuration,
    source: DailyModeSource,
    promptEnabled?: boolean,
  ) => {
    setDailyModeBusy(true);
    setError('');
    try {
      const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
      const updated = await api.updateDailyMode(
        { dailyMode: mode, duration, timezone, source },
        `daily-mode-${crypto.randomUUID()}`,
      );
      let updatedProfile: LearningProfile | null = null;
      let promptPreferenceFailed = false;
      if (
        source === 'dialog'
        && typeof promptEnabled === 'boolean'
        && data
        && data.profile.preferences.dailyModePromptEnabled !== promptEnabled
      ) {
        const profile = data.profile;
        try {
          updatedProfile = await api.updateProfile({
            profession: profile.profession,
            stage: profile.stage,
            purpose: profile.purpose,
            domains: profile.domains,
            experience: profile.experience,
            weeklyMinutes: profile.weeklyMinutes,
            targetDate: profile.targetDate,
            preferences: {
              ...profile.preferences,
              dailyModePromptEnabled: promptEnabled,
            },
          });
        } catch {
          promptPreferenceFailed = true;
        }
      }
      setData((current) => current ? {
        ...current,
        dailyMode: updated,
        profile: updatedProfile || current.profile,
      } : current);
      if (source === 'header_toggle') {
        if (section) setActivityDailyMode(mode);
        setNotice(mode === 'fast'
          ? '已切换为快速阅读：当前只展示关键段落和 30 秒自检。'
          : '已切换为完整阅读：当前展示本节全部正文。');
      }
      setDailyModeExpiredDuringActivity(false);
      setDailyModeDialogOpen(false);
      if (promptPreferenceFailed) {
        setNotice('学习模式已开始；“不再自动弹出”未能保存，可稍后在学习画像中关闭。');
      }
      if (pendingSectionId) {
        const target = pendingSectionId;
        setPendingSectionId('');
        const value = await openAndTrackSection(target);
        setSection(value);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '学习模式同步失败');
    } finally {
      setDailyModeBusy(false);
    }
  };

  const firstUsableSection = (value: Series) => {
    let completedFallback: string | null = null;
    for (const book of value.books) {
      for (const chapter of book.chapters) {
        const match = chapter.sections.find(
          (item) => !['locked', 'completed', 'skipped'].includes(item.status),
        );
        if (match) return match.id;
        completedFallback ||= chapter.sections.find(
          (item) => item.status === 'completed',
        )?.id || null;
      }
    }
    return completedFallback;
  };

  const waitForInitialSectionReady = async (
    value: Series,
    onProgress?: (stage: SeriesCreationStage) => void,
  ) => {
    let task = value.initializationTask;
    if (!task) {
      throw new Error('学习路线没有返回第一节准备任务，请重新创建。');
    }
    for (let poll = 0; poll < 360; poll += 1) {
      if (!['succeeded', 'failed'].includes(task.status)) {
        task = await api.learningTask(task.taskId);
      }
      if (task.status === 'failed') {
        throw new Error(generationFailureMessage(task, '第一节内容'));
      }
      if (task.status === 'succeeded') {
        onProgress?.('opening');
        const refreshed = await api.series(value.id);
        const targetSectionId = typeof task.result?.targetSectionId === 'string'
          ? task.result.targetSectionId
          : null;
        const targetBook = targetSectionId
          ? refreshed.books.find((book) => bookContainsSection(book, targetSectionId))
          : null;
        if (!targetSectionId || !targetBook) {
          throw new Error('第一节已经准备完成，但学习路线尚未同步，请重新进入书架。');
        }
        const readySection = await openAndTrackSection(targetSectionId);
        if (
          !readySection.content
          || readySection.content.publicationStatus !== 'published'
          || !readySection.quiz
          || readySection.quiz.publicationStatus !== 'published'
        ) {
          throw new Error('第一节正文和验证题尚未完整发布，请稍后重试。');
        }
        return { series: refreshed, section: readySection, bookId: targetBook.id };
      }
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
    }
    throw new Error('第一节仍在准备，可以稍后从书架重新进入本系列。');
  };

  const monitorInitialSection = async (value: Series) => {
    const initialTask = value.initializationTask;
    if (!initialTask || initialTask.status === 'failed') return false;
    const monitorVersion = ++initialSectionMonitorVersion.current;
    const navigationVersion = routeRequestVersion.current;
    const busyLabel = '准备中…';
    const isCurrent = () => {
      const route = routeFromLocation();
      return monitorVersion === initialSectionMonitorVersion.current
        && navigationVersion === routeRequestVersion.current
        && route.view === 'learn'
        && route.seriesId === value.id;
    };
    setPreparingInitialSection(true);
    setBusy(busyLabel);
    setError('');
    try {
      let task = initialTask;
      for (let poll = 0; poll < 360; poll += 1) {
        if (!isCurrent()) return false;
        if (!['succeeded', 'failed'].includes(task.status)) {
          task = await api.learningTask(task.taskId);
          if (!isCurrent()) return false;
        }
        if (task.status === 'succeeded') {
          const refreshed = await api.series(value.id);
          if (!isCurrent()) return false;
          setSeries(refreshed);
          const targetSectionId = typeof task.result?.targetSectionId === 'string'
            ? task.result.targetSectionId
            : null;
          const targetBook = targetSectionId
            ? refreshed.books.find((book) => bookContainsSection(book, targetSectionId))
            : null;
          if (targetSectionId && targetBook) {
            setDirectoryBookId(targetBook.id);
            updateBrowserLocation(
              seriesPath(refreshed.id, targetSectionId),
              'replace',
            );
            const readySection = await run(
              '正在打开第一节…',
              () => openAndTrackSection(targetSectionId),
            );
            if (!isCurrent()) return false;
            setSection(readySection);
          } else {
            setSection(null);
            updateBrowserLocation(seriesPath(refreshed.id), 'replace');
          }
          return true;
        }
        if (task.status === 'failed') {
          const refreshed = await api.series(value.id);
          if (!isCurrent()) return false;
          setSeries(refreshed);
          setSection(null);
          updateBrowserLocation(seriesPath(refreshed.id), 'replace');
          setError(generationFailureMessage(task, '第一节内容'));
          return true;
        }
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
      }
      if (isCurrent()) setError('第一节仍在准备，可以稍后重新进入本书查看。');
      return true;
    } catch (reason) {
      if (isCurrent()) {
        setError(reason instanceof Error ? reason.message : '无法读取第一节准备状态。');
      }
      return true;
    } finally {
      if (monitorVersion === initialSectionMonitorVersion.current) {
        setPreparingInitialSection(false);
        setBusy((current) => current === busyLabel ? '' : current);
      }
    }
  };

  const openSeries = async (
    seriesId: string,
    requestedSectionId: string | null = null,
    historyMode: 'push' | 'replace' | 'none' = 'push',
    requestedBookId: string | null = null,
    focusAskMe = false,
  ) => {
    const shouldFocusAskMe = focusAskMe;
    const requestVersion = historyMode === 'none'
      ? routeRequestVersion.current
      : ++routeRequestVersion.current;
    const value = await run('正在进入学习空间…', () => api.series(seriesId));
    if (requestVersion !== routeRequestVersion.current) return;
    const targetShelf = data?.shelves.find((item) => (
      item.series.some((candidate) => candidate.id === seriesId)
    )) || null;
    if (
      value.initializationTask
      && (
        value.initializationTask.status !== 'succeeded'
        || (!requestedSectionId && value.progress === 0)
      )
    ) {
      let ready: Awaited<ReturnType<typeof waitForInitialSectionReady>>;
      try {
        ready = await run(
          '正在准备目录和第一节…',
          () => waitForInitialSectionReady(value),
        );
      } catch {
        // run() has already translated the failure for the learner. Keep the
        // current shelf/home mounted and never leak a rejected click promise.
        return;
      }
      if (requestVersion !== routeRequestVersion.current) return;
      setShelf(targetShelf);
      setDirectoryBookId(ready.bookId);
      setSeries(ready.series);
      setSection(ready.section);
      setView('learn');
      updateBrowserLocation(
        `${seriesPath(ready.series.id, ready.section.id)}${shouldFocusAskMe ? '#ask-me' : ''}`,
        historyMode,
      );
      return;
    }
    updateBrowserLocation(
      `${seriesPath(seriesId, requestedSectionId)}${shouldFocusAskMe ? '#ask-me' : ''}`,
      historyMode,
    );
    setShelf(targetShelf);
    setSeries(value);
    setView('learn');
    const requestedBook = requestedBookId
      ? value.books.find((book) => book.id === requestedBookId) || null
      : null;
    if (requestedBook) setDirectoryBookId(requestedBook.id);
    const resumeSection = data?.resume?.sectionId;
    const resumeBelongsToSeries = resumeSection
      ? value.books.some((book) => book.chapters.some(
        (chapter) => chapter.sections.some(
          (item) => item.id === resumeSection && !['locked', 'skipped'].includes(item.status),
        ),
      ))
      : false;
    const requestedBelongsToSeries = requestedSectionId
      ? value.books.some((book) => book.chapters.some(
        (chapter) => chapter.sections.some(
          (item) => item.id === requestedSectionId && !['locked', 'skipped'].includes(item.status),
        ),
      ))
      : false;
    const initial = requestedBelongsToSeries
      ? requestedSectionId!
      : !requestedBook && resumeBelongsToSeries && value.progress > 0
        ? resumeSection!
        : null;
    const initialBook = initial
      ? value.books.find((book) => bookContainsSection(book, initial)) || null
      : requestedBook;
    setDirectoryBookId(
      initialBook?.id
      || value.books.find((book) => book.status !== 'locked')?.id
      || value.books[0]?.id
      || '',
    );
    if (initial) {
      updateBrowserLocation(
        `${seriesPath(value.id, initial)}${shouldFocusAskMe ? '#ask-me' : ''}`,
        historyMode === 'none' ? 'none' : 'replace',
      );
      await loadSection(initial, 'none', false);
      if (shouldFocusAskMe) {
        updateBrowserLocation(`${seriesPath(value.id, initial)}#ask-me`, 'none');
      }
    } else setSection(null);
  };

  const refreshSeries = async () => {
    if (!series) return;
    const value = await api.series(series.id);
    setSeries(value);
  };

  const retryInitialSection = async () => {
    const task = series?.initializationTask;
    if (!series || !task?.retryable) return;
    setPreparingInitialSection(true);
    try {
      const retried = await api.retryLearningTask(task.taskId);
      const updated = { ...series, initializationTask: retried };
      setSeries(updated);
      void monitorInitialSection(updated);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '第一节重试失败。');
      setPreparingInitialSection(false);
    }
  };

  const openChapter = async (chapter: Chapter) => {
    if (
      preparingInitialSection ||
      chapterGenerationRequests.current.has(chapter.id)
    ) return;
    chapterGenerationRequests.current.add(chapter.id);
    setGeneratingChapterId(chapter.id);
    try {
      const updated = await run('正在规划本章小节…', () => api.chapter(chapter.id));
      await refreshSeries();
      const first = updated.sections.find(
        (item) => !['locked', 'completed', 'skipped'].includes(item.status),
      ) || updated.sections.find((item) => item.status === 'completed');
      if (first) await loadSection(first.id);
    } catch (reason) {
      if (!(reason instanceof ApiError) || reason.code !== 'GENERATION_IN_PROGRESS' || !series) {
        throw reason;
      }
      setError('');
      setProgressNotice(generationProgressNotice(reason));
      for (let poll = 0; poll < 120; poll += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 1500));
        try {
          const refreshed = await api.series(series.id);
          const refreshedChapter = refreshed.books
            .flatMap((book) => book.chapters)
            .find((item) => item.id === chapter.id);
          setSeries(refreshed);
          if (refreshedChapter?.sections.length) {
            setProgressNotice(null);
            setNotice('本章已准备好，可以从目录进入第一节。');
            return;
          }
        } catch {
          // A transient refresh failure should not turn active generation red.
        }
      }
      setProgressNotice({
        title: '本章仍在生成',
        message: '这次比平时稍久，请耐心等待。稍后重新打开本章即可继续。',
      });
    } finally {
      chapterGenerationRequests.current.delete(chapter.id);
      setGeneratingChapterId('');
    }
  };

  const startNextBook = async (bookId: string) => {
    if (!series) return;
    const nextBook = series.books.find((book) => book.id === bookId);
    const firstChapter = nextBook?.chapters[0];
    if (!firstChapter) return;
    setDirectoryBookId(nextBook.id);
    setSection(null);
    if (
      series.initializationTask
      && !['failed', 'succeeded'].includes(series.initializationTask.status)
    ) {
      updateBrowserLocation(seriesPath(series.id), 'replace');
      await monitorInitialSection(series);
      return;
    }
    const availableSection = firstChapter.sections.find(
      (item) => item.status !== 'locked' && item.status !== 'completed',
    );
    if (availableSection) await loadSection(availableSection.id);
    else await openChapter(firstChapter);
  };

  const activateBook = async (
    book: Book,
    feedback = '',
    previousProposalId?: string,
  ) => {
    const requestVersion = ++bookReplanRequestVersion.current;
    setBookReplan({ book, proposal: null, status: 'preparing', feedback, previousProposalId });
    try {
      const proposal = await api.replanBook(
        book.id,
        feedback ? { feedback, previousProposalId } : undefined,
      );
      if (requestVersion !== bookReplanRequestVersion.current) return;
      setBookReplan({ book, proposal, status: 'ready', feedback: '', previousProposalId: proposal.proposalId });
    } catch {
      if (requestVersion !== bookReplanRequestVersion.current) return;
      setBookReplan({ book, proposal: null, status: 'failed', feedback, previousProposalId });
    }
  };

  const generateSection = async (sectionId: string) => {
    try {
      const value = await run('正在准备并检查本节内容…', () => api.prepareSection(sectionId));
      setSection(value);
      await refreshSeries();
    } catch (reason) {
      if (!(reason instanceof ApiError) || !reason.retryable) throw reason;
      try {
        const failed = await api.section(sectionId);
        setSection(failed);
        setError('');
        await refreshSeries();
      } catch {
        setError(generationFailureMessage(null));
      }
    }
  };

  const regenerateSection = async (sectionId: string) => {
    let polling = true;
    let timer: number | undefined;
    const pollGeneration = async () => {
      try {
        const current = await api.section(sectionId);
        setSection(current);
      } catch {
        // The foreground request owns error reporting. Polling is best-effort.
      } finally {
        if (polling) timer = window.setTimeout(pollGeneration, 1000);
      }
    };
    timer = window.setTimeout(pollGeneration, 250);
    try {
      const value = await run('正在重新准备并检查本节内容…', () => api.regenerateSection(sectionId));
      setSection(value);
      await refreshSeries();
    } finally {
      polling = false;
      if (timer !== undefined) window.clearTimeout(timer);
      try {
        setSection(await api.section(sectionId));
      } catch {
        // Keep the last successfully polled state when the final refresh fails.
      }
    }
  };

  const restoreLocation = async () => {
    if (!data) return;
    const requestVersion = ++routeRequestVersion.current;
    const route = routeFromLocation();
    setShowUserMenu(false);
    setError('');
    try {
      if (route.view === 'home') {
        setView('home');
        setShelf(null);
        setSeries(null);
        setSection(null);
        return;
      }
      if (route.view === 'profile') {
        setProfileSection(route.section);
        setView('profile');
        setShelf(null);
        setSeries(null);
        setSection(null);
        return;
      }
      if (route.view === 'knowledge') {
        setView('knowledge');
        setShelf(null);
        setSeries(null);
        setSection(null);
        return;
      }
      if (route.view === 'review') {
        setView('review');
        setShelf(null);
        setSeries(null);
        setSection(null);
        return;
      }
      if (route.view === 'shelf' || route.view === 'series-create') {
        const targetShelf = data.shelves.find((item) => item.id === route.shelfId);
        if (!targetShelf) {
          updateBrowserLocation('/', 'replace');
          setView('home');
          setShelf(null);
          setSeries(null);
          setSection(null);
          setError('这个书架不存在，或当前账号无权访问。');
          return;
        }
        if (route.view === 'series-create') openSeriesCreation(targetShelf, 'none');
        else openShelf(targetShelf, 'none');
        return;
      }

      setBusy('正在恢复上次浏览位置…');
      let restoredSeries = await api.series(route.seriesId);
      if (requestVersion !== routeRequestVersion.current) return;
      const restoredShelf = data.shelves.find((item) => (
        item.series.some((candidate) => candidate.id === restoredSeries.id)
      )) || null;
      if (
        restoredSeries.initializationTask
        && (
          restoredSeries.initializationTask.status !== 'succeeded'
          || (!route.sectionId && restoredSeries.progress === 0)
        )
      ) {
        const ready = await waitForInitialSectionReady(restoredSeries);
        if (requestVersion !== routeRequestVersion.current) return;
        restoredSeries = ready.series;
        if (!route.sectionId) {
          updateBrowserLocation(
            seriesPath(ready.series.id, ready.section.id),
            'replace',
          );
          setShelf(restoredShelf);
          setDirectoryBookId(ready.bookId);
          setSeries(ready.series);
          setSection(ready.section);
          setView('learn');
          return;
        }
      }
      if (!route.sectionId) {
        setShelf(restoredShelf);
        setSeries(restoredSeries);
        setSection(null);
        setView('learn');
        return;
      }
      const sectionCanOpen = restoredSeries.books.some((book) => book.chapters.some(
        (chapter) => chapter.sections.some(
          (item) => item.id === route.sectionId && item.status !== 'locked',
        ),
      ));
      if (!sectionCanOpen) {
        updateBrowserLocation(seriesPath(restoredSeries.id), 'replace');
        setShelf(restoredShelf);
        setSeries(restoredSeries);
        setSection(null);
        setView('learn');
        setError('这个小节尚未解锁，已返回当前系列目录。');
        return;
      }
      const restoredSection = await openAndTrackSection(route.sectionId);
      if (requestVersion !== routeRequestVersion.current) return;
      // Commit the complete destination together so a hard refresh does not
      // visibly walk through the series directory before showing the section.
      setShelf(restoredShelf);
      setSeries(restoredSeries);
      setSection(restoredSection);
      setView('learn');
    } catch (reason) {
      if (requestVersion !== routeRequestVersion.current) return;
      updateBrowserLocation('/', 'replace');
      setView('home');
      setShelf(null);
      setSeries(null);
      setSection(null);
      setError(reason instanceof Error ? reason.message : '无法恢复这个浏览位置。');
    } finally {
      if (requestVersion === routeRequestVersion.current) {
        setBusy('');
        setRestoringInitialRoute(false);
      }
    }
  };

  restoreLocationRef.current = restoreLocation;

  useEffect(() => {
    if (!auth || !data) {
      if (!auth) routeInitializedForUser.current = '';
      return;
    }
    if (routeInitializedForUser.current === auth.user.id) return;
    routeInitializedForUser.current = auth.user.id;
    void restoreLocationRef.current();
  }, [auth?.user.id, data]);

  if (exitReceipt) {
    return <AccountExitReceiptPage receipt={exitReceipt} onClose={() => setExitReceipt(null)} />;
  }

  if (!authChecked) {
    return <AppLoadingScreen message="正在打开你的书架…" />;
  }

  if(!auth) {
    const isDemo = authConfig?.mode === 'demo';
    const isLocal = authConfig?.mode === 'local';
    const isPassword = authConfig?.mode === 'password';
    const usesCredentials = isLocal || isPassword;
    const registrationOpen = Boolean(
      isPassword && authConfig?.registrationMode !== 'closed',
    );
    const providerName = authConfig?.providerName || '统一身份账户';
    return (
      <div className="app-shell auth-shell">
        <header className="auth-header">
          <div className="auth-header-left">
            <span className="brand"><span className="brand-mark"><i /></span><b>slow</b></span>
            <a className="docs-entry-link" href="/docs">使用指南</a>
            <a
              className="github-repo-link"
              href="https://github.com/3321-cpsasd/slow"
              target="_blank"
              rel="noreferrer"
              aria-label="在新标签页打开 Slow GitHub 仓库"
            >
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path d="M12 2C6.48 2 2 6.59 2 12.25c0 4.53 2.87 8.37 6.84 9.73.5.1.68-.22.68-.49 0-.24-.01-1.05-.02-1.9-2.78.62-3.37-1.21-3.37-1.21-.45-1.18-1.11-1.49-1.11-1.49-.91-.64.07-.62.07-.62 1 .07 1.53 1.05 1.53 1.05.89 1.57 2.34 1.11 2.91.85.09-.66.35-1.11.64-1.37-2.22-.26-4.56-1.14-4.56-5.07 0-1.12.39-2.04 1.03-2.76-.1-.26-.45-1.31.1-2.72 0 0 .84-.28 2.75 1.05A9.31 9.31 0 0 1 12 6.11a9.3 9.3 0 0 1 2.5.35c1.91-1.33 2.75-1.05 2.75-1.05.55 1.41.2 2.46.1 2.72.64.72 1.03 1.64 1.03 2.76 0 3.94-2.34 4.8-4.57 5.06.36.32.68.94.68 1.89 0 1.37-.01 2.47-.01 2.81 0 .27.18.59.69.49A10.27 10.27 0 0 0 22 12.25C22 6.59 17.52 2 12 2Z" />
              </svg>
              <span className="github-repo-identity">
                <span>3321-cpsasd</span>
                <i aria-hidden="true">/</i>
                <strong>slow</strong>
              </span>
              <span className="github-repo-mobile-label">GitHub</span>
              <span className="github-repo-external" aria-hidden="true">↗</span>
            </a>
          </div>
          <span className="auth-header-tagline">AI 原生个人学习系统</span>
        </header>
        <main className="auth-main">
          <section className="auth-story">
            <p className="eyebrow">YOUR PERSONAL LEARNING LIBRARY</p>
            <h1>把想学的，<br />变成真正学会的。</h1>
            <div className="auth-journey" aria-label="Slow 学习闭环">
              <article><span>01</span><div><b>生成你的书</b></div></article>
              <article><span>02</span><div><b>逐节学习验证</b></div></article>
              <article><span>03</span><div><b>持续学习</b></div></article>
            </div>
          </section>

          <section className={`auth-card auth-panel-${authPanel}`} aria-busy={Boolean(busy) || !authChecked}>
            {registrationResult ? (
              <RecoveryCodePanel
                code={registrationResult.recoveryCode}
                renewed={false}
                onContinue={() => {
                  setAuth(registrationResult);
                  setData(null);
                  setRegistrationResult(null);
                }}
              />
            ) : renewedRecoveryCode ? (
              <RecoveryCodePanel
                code={renewedRecoveryCode}
                renewed
                onContinue={() => switchAuthPanel('login')}
              />
            ) : (
              <>
                <div className={`auth-mode-badge ${isDemo || isLocal ? 'demo' : ''}`}>
                  <i />{isDemo
                    ? '固定体验环境'
                    : isLocal
                      ? '本地多账号环境'
                      : registrationOpen
                        ? 'ALPHA 开放注册'
                        : '受邀用户空间'}
                </div>

                {registrationOpen && authPanel !== 'recover' && (
                  <div className="auth-card-tabs" role="tablist" aria-label="账号入口">
                    <button
                      type="button"
                      role="tab"
                      aria-selected={authPanel === 'login'}
                      onClick={() => switchAuthPanel('login')}
                    >登录</button>
                    <button
                      type="button"
                      role="tab"
                      aria-selected={authPanel === 'register'}
                      onClick={() => switchAuthPanel('register')}
                    >创建账号</button>
                  </div>
                )}

                <h2>{isDemo
                  ? '进入体验书架'
                  : authPanel === 'register'
                    ? '领取你的学习账号'
                    : authPanel === 'recover'
                      ? '用恢复码重置密码'
                      : usesCredentials
                        ? '登录学习账号'
                        : '欢迎回来'}</h2>
                <p>{isDemo
                  ? '无需配置第三方账号，使用本机固定体验身份查看完整学习闭环。'
                  : authPanel === 'register'
                    ? '无需邮箱或手机号。创建后请保存仅展示一次的账号恢复码。'
                    : authPanel === 'recover'
                      ? '输入注册时保存的恢复码。完成后，旧密码和旧恢复码都会失效。'
                      : usesCredentials
                        ? '输入账号和密码，回到你的书架、学习记录与复习安排。'
                        : '登录后继续你的书架、学习记录与复习安排。'}</p>

                {!authConfig && error ? (
                  <button className="auth-submit" onClick={() => void initializeAuth()}>
                    重新连接服务 <span>→</span>
                  </button>
                ) : isDemo ? (
                  <button
                    className="auth-submit"
                    disabled={!authChecked}
                    onClick={async () => {
                      sessionStorage.setItem('slow_demo_entered', 'true');
                      await initializeAuth();
                    }}
                  >
                    进入本地体验 <span>→</span>
                  </button>
                ) : usesCredentials && authPanel === 'register' ? (
                  <form className="local-auth-form" onSubmit={(event) => void registerAlphaAccount(event)}>
                    <label>
                      学习账号
                      <input
                        autoComplete="username"
                        value={registrationUsername}
                        onChange={(event) => setRegistrationUsername(event.target.value)}
                        placeholder="3–80 个文字、字母或数字"
                        minLength={3}
                        maxLength={80}
                        required
                      />
                    </label>
                    {authConfig?.registrationCodeRequired && (
                      <label>
                        Alpha 访问码
                        <input
                          type="password"
                          autoComplete="off"
                          value={alphaCode}
                          onChange={(event) => setAlphaCode(event.target.value)}
                          placeholder="输入内测访问码"
                          required
                        />
                      </label>
                    )}
                    <label>
                      设置密码
                      <span className="password-input">
                        <input
                          type={showRegistrationPassword ? 'text' : 'password'}
                          autoComplete="new-password"
                          value={registrationPassword}
                          onChange={(event) => setRegistrationPassword(event.target.value)}
                          placeholder="至少 12 个字符"
                          minLength={12}
                          required
                        />
                        <PasswordVisibilityToggle
                          visible={showRegistrationPassword}
                          onToggle={() => setShowRegistrationPassword((visible) => !visible)}
                        />
                      </span>
                    </label>
                    <label>
                      再输入一次
                      <span className="password-input">
                        <input
                          type={showRegistrationPasswordConfirm ? 'text' : 'password'}
                          autoComplete="new-password"
                          value={registrationPasswordConfirm}
                          onChange={(event) => setRegistrationPasswordConfirm(event.target.value)}
                          placeholder="确认你的密码"
                          minLength={12}
                          required
                        />
                        <PasswordVisibilityToggle
                          visible={showRegistrationPasswordConfirm}
                          onToggle={() => setShowRegistrationPasswordConfirm((visible) => !visible)}
                        />
                      </span>
                    </label>
                    <button className="auth-submit" type="submit" disabled={Boolean(busy)}>
                      创建并进入书架 <span>→</span>
                    </button>
                  </form>
                ) : usesCredentials && authPanel === 'recover' ? (
                  <form className="local-auth-form" onSubmit={(event) => void resetPasswordWithRecoveryCode(event)}>
                    <label>
                      学习账号
                      <input
                        autoComplete="username"
                        value={recoveryUsername}
                        onChange={(event) => setRecoveryUsername(event.target.value)}
                        placeholder="输入你的账号"
                        required
                      />
                    </label>
                    <label>
                      恢复码
                      <input
                        autoComplete="off"
                        value={recoveryCode}
                        onChange={(event) => setRecoveryCode(event.target.value)}
                        placeholder="SLOW-XXXX-XXXX-…"
                        minLength={20}
                        required
                      />
                    </label>
                    <label>
                      新密码
                      <span className="password-input">
                        <input
                          type={showRecoveryPassword ? 'text' : 'password'}
                          autoComplete="new-password"
                          value={recoveryPassword}
                          onChange={(event) => setRecoveryPassword(event.target.value)}
                          placeholder="至少 12 个字符"
                          minLength={12}
                          required
                        />
                        <PasswordVisibilityToggle
                          visible={showRecoveryPassword}
                          onToggle={() => setShowRecoveryPassword((visible) => !visible)}
                        />
                      </span>
                    </label>
                    <label>
                      确认新密码
                      <span className="password-input">
                        <input
                          type={showRecoveryPasswordConfirm ? 'text' : 'password'}
                          autoComplete="new-password"
                          value={recoveryPasswordConfirm}
                          onChange={(event) => setRecoveryPasswordConfirm(event.target.value)}
                          placeholder="再次输入新密码"
                          minLength={12}
                          required
                        />
                        <PasswordVisibilityToggle
                          visible={showRecoveryPasswordConfirm}
                          onToggle={() => setShowRecoveryPasswordConfirm((visible) => !visible)}
                        />
                      </span>
                    </label>
                    <button className="auth-submit" type="submit" disabled={Boolean(busy)}>
                      重置密码 <span>→</span>
                    </button>
                    <button type="button" className="auth-text-button" onClick={() => switchAuthPanel('login')}>
                      ← 返回登录
                    </button>
                  </form>
                ) : usesCredentials ? (
                  <form className="local-auth-form" onSubmit={(event) => void loginWithLocalAccount(event)}>
                    <label>
                      账号
                      <input
                        autoComplete="username"
                        value={localUsername}
                        onChange={(event) => setLocalUsername(event.target.value)}
                        placeholder={isPassword ? '输入你的学习账号' : '输入分配给你的账号'}
                        required
                      />
                    </label>
                    <label>
                      密码
                      <span className="password-input">
                        <input
                          type={showLocalPassword ? 'text' : 'password'}
                          autoComplete="current-password"
                          value={localPassword}
                          onChange={(event) => setLocalPassword(event.target.value)}
                          placeholder={isPassword ? '输入你的密码' : '输入本地体验密码'}
                          minLength={8}
                          required
                        />
                        <PasswordVisibilityToggle
                          visible={showLocalPassword}
                          onToggle={() => setShowLocalPassword((visible) => !visible)}
                        />
                      </span>
                    </label>
                    <button className="auth-submit" type="submit" disabled={Boolean(busy)}>
                      登录独立书架 <span>→</span>
                    </button>
                    {isPassword && (
                      <button type="button" className="auth-text-button" onClick={() => switchAuthPanel('recover')}>
                        我有恢复码，需要重置密码
                      </button>
                    )}
                  </form>
                ) : (
                  <button
                    className="auth-submit"
                    disabled={!authChecked}
                    onClick={() => api.login(`${window.location.pathname}${window.location.search}`)}
                  >
                    使用{providerName}继续 <span>→</span>
                  </button>
                )}

                {!authChecked && <div className="auth-inline-status">正在确认登录状态…</div>}
                {error && <div className="auth-inline-error">{error}</div>}

                {(isDemo || isLocal || isPassword) && (
                  <small className="auth-disclaimer">
                    {isDemo
                      ? '体验内容会明确标记，并与正式账号数据分开。'
                      : isLocal
                        ? '本地账号仅用于开发和场景验证，生产环境会拒绝启用。'
                        : registrationOpen
                          ? 'Alpha 账号不绑定邮箱或手机号；请自行保存恢复码。'
                          : '账号仅由内测管理员创建，当前不开放注册。'}
                  </small>
                )}
                {authConfig?.privacyNotice && (
                  <details className="auth-privacy-brief">
                    <summary>内测隐私与数据说明</summary>
                    <p>{authConfig.privacyNotice.summary}</p>
                    <small>登录后、开始填写学习画像前，需要确认隐私告知与自愿参加内测。</small>
                  </details>
                )}
              </>
            )}
          </section>
        </main>
      </div>
    );
  }

  if (auth.privacy.required) {
    return (
      <PrivacyConsentGate
        userName={auth.user.name}
        privacy={auth.privacy}
        onAccept={acceptPrivacy}
        onLogout={logout}
      />
    );
  }

  if (auth.onboarding.required) {
    return (
      <ProfileOnboardingFlow
        initial={auth.onboarding}
        userName={auth.user.name}
        onComplete={continueFromOnboarding}
        onLogout={logout}
      />
    );
  }

  if (restoringInitialRoute) {
    return <AppLoadingScreen message="正在回到你刚才阅读的位置…" />;
  }

  const showDailyModeDialog = Boolean(
    data?.dailyMode
    && dailyModePromptEnabled
    && dailyModeDialogOpen
    && !hasActiveDailyMode(),
  );
  const currentMilestonePath = data?.milestoneDashboard.path || null;
  const activeMilestonePath = currentMilestonePath?.seriesId === series?.id
    ? currentMilestonePath
    : null;
  const pathDecisionKey = activeMilestonePath
    ? `${activeMilestonePath.seriesId}:${activeMilestonePath.version}:${data?.milestoneDashboard.goal.profileVersion}:${activeMilestonePath.goalAligned}`
    : '';
  const pathNeedsDecision = Boolean(
    activeMilestonePath
    && (activeMilestonePath.status === 'proposed' || !activeMilestonePath.goalAligned)
    && dismissedPathAlertSeriesId !== pathDecisionKey,
  );

  return (
    <div className={`app-shell view-${view}`}>
      <header className="app-header">
        <button
          className="brand"
          aria-label="返回 Slow 首页"
          onClick={goHome}
        >
          <span className="brand-mark"><i /></span>
          <b>slow</b>
        </button>
        {view === 'learn' && series ? (
          <div className="header-context">
            <span>{series.title}</span>
            <i>·</i>
            <span>{series.progress}%</span>
          </div>
        ) : view === 'profile' ? (
          <small>个人中心</small>
        ) : view === 'knowledge' ? (
          <small>我的知识版图</small>
        ) : view === 'review' ? (
          <small>复习与补强</small>
        ) : (
          <small>一步一步，学成自己的书</small>
        )}
        <div className="header-actions">
          <a className="quiet-button docs-header-link" href="/docs">使用指南</a>
          {data?.dailyMode && (
            <DailyModeHeader
              state={data.dailyMode}
              effectiveMode={
                activityDailyMode
                || data.dailyMode.dailyMode
                || data.dailyMode.lastDailyMode
                || 'slow'
              }
              expiredInActivity={dailyModeExpiredDuringActivity}
              busy={dailyModeBusy}
              onActivate={activateDailyMode}
            />
          )}
          <AppBusyStatus message={busy} />
          {AI_RUNTIME_SETTINGS_ENABLED && (
            <button className="quiet-button ai-settings-trigger" onClick={() => setShowAiSettings(true)}>
              <span aria-hidden="true" />
              AI 设置
            </button>
          )}
          {view === 'learn' && (
            <button
              className="quiet-button learn-back-button"
              aria-label={shelf ? `返回${shelf.name}书架` : '返回全部书架'}
              onClick={returnToShelf}
            >
              返回当前书架
            </button>
          )}
          <div className="user-menu-shell" ref={userMenuRef}>
            <button
              className={`user-avatar-trigger ${view === 'profile' ? 'is-active' : ''}`}
              aria-label={`${auth.user.name}的账号菜单`}
              aria-haspopup="menu"
              aria-expanded={showUserMenu}
              onClick={() => setShowUserMenu((visible) => !visible)}
            >
              <span aria-hidden="true">{auth.user.name.trim().slice(0, 1).toUpperCase() || '我'}</span>
            </button>
            {showUserMenu && (
              <div className="user-account-menu" role="menu" aria-label="账号菜单">
                <header>
                  <span aria-hidden="true">{auth.user.name.trim().slice(0, 1).toUpperCase() || '我'}</span>
                  <div>
                    <b>{auth.user.name}</b>
                    <small>{{
                      demo: '固定体验账号',
                      local: '本地独立账号',
                      password: '受邀学习账号',
                      oidc: '统一身份账号',
                    }[auth.mode]}</small>
                  </div>
                </header>
                <button role="menuitem" onClick={() => openProfileCenter('profile')}>
                  <span><b>个人中心</b><small>学习画像与学习节奏</small></span><i aria-hidden="true">→</i>
                </button>
                <button role="menuitem" onClick={openKnowledgeMap}>
                  <span><b>知识版图</b><small>能力段位、保持状态与目标覆盖</small></span><i aria-hidden="true">→</i>
                </button>
                <button role="menuitem" onClick={openReviewCenter}>
                  <span><b>复习与补强</b><small>快速唤醒知识，修复薄弱连接</small></span><i aria-hidden="true">→</i>
                </button>
                <button role="menuitem" onClick={() => openProfileCenter('account')}>
                  <span><b>账号与数据</b><small>身份、数据归属与安全</small></span><i aria-hidden="true">→</i>
                </button>
                <button className="user-menu-logout" role="menuitem" onClick={() => { setShowUserMenu(false); void logout(); }}>
                  退出登录
                </button>
              </div>
            )}
          </div>
        </div>
      </header>

      <AppStatusRegion
        error={error}
        notice={notice}
        progress={progressNotice}
        onDismissError={() => setError('')}
        onDismissNotice={() => setNotice('')}
        onDismissProgress={() => setProgressNotice(null)}
      />
      <main className={view === 'learn' ? 'learn-main' : view === 'profile' ? 'profile-main' : view === 'knowledge' ? 'knowledge-main' : view === 'review' ? 'review-main' : view === 'series-create' ? 'series-create-main' : 'marketing-main'}>
        {view === 'home' && (
          <Home
            data={data}
            dailyMode={data?.dailyMode?.dailyMode || data?.dailyMode?.lastDailyMode || 'slow'}
            onOpen={openShelf}
            onContinue={(seriesId, sectionId, bookId) => (
              openSeries(seriesId, sectionId, 'push', bookId)
            )}
            onActivateBook={activateBook}
            onOpenReview={openReviewCenter}
            onCreate={async (body) => {
              const value = await run('正在创建书架…', () => api.createShelf(body));
              setData((current) => current
                ? { ...current, shelves: [...current.shelves, value] }
                : current);
              openShelf(value);
            }}
          />
        )}
        {view === 'shelf' && shelf && (
          <ShelfPage
            shelf={shelf}
            onBack={goHome}
            onStartCreate={() => openSeriesCreation(shelf)}
            onRename={async (body) => {
              const value = await run('正在重命名书架…', () => api.renameShelf(shelf.id, body));
              setData((current) => current
                ? {
                    ...current,
                    shelves: current.shelves.map((item) => item.id === value.id ? value : item),
                  }
                : current);
              setShelf(value);
              setNotice('书架已重命名');
            }}
            onDeleteShelf={async () => {
              await run('正在删除书架…', () => api.deleteShelf(shelf.id));
              const refreshed = await api.bootstrap();
              setData(refreshed);
              setShelf(null);
              setSeries(null);
              setSection(null);
              updateBrowserLocation('/', 'replace');
              setView('home');
              setNotice('书架及其中的学习系列已删除');
            }}
            onOpen={(seriesId, sectionId, bookId) => {
              void openSeries(seriesId, sectionId, 'push', bookId);
            }}
            onActivateBook={activateBook}
            onRenameSeries={async (seriesId, body) => {
              const value = await run('正在重命名系列…', () => api.renameSeries(seriesId, body));
              setData((current) => current
                ? {
                    ...current,
                    shelves: current.shelves.map((item) => item.id === shelf.id
                      ? {
                          ...item,
                          series: item.series.map((candidate) => candidate.id === value.id ? value : candidate),
                        }
                      : item),
                  }
                : current);
              setShelf((current) => current
                ? {
                    ...current,
                    series: current.series.map((candidate) => candidate.id === value.id ? value : candidate),
                  }
                : current);
              if (series?.id === value.id) setSeries(value);
              setNotice('系列已重命名');
            }}
            onDelete={async (seriesId) => {
              await run('正在从书架移除系列…', async () => {
                await api.deleteSeries(seriesId);
                const refreshed = await api.bootstrap();
                const refreshedShelf = refreshed.shelves.find((item) => item.id === shelf.id) || null;
                setData(refreshed);
                setShelf(refreshedShelf);
                if (!refreshedShelf) {
                  updateBrowserLocation('/', 'replace');
                  setView('home');
                }
              });
            }}
          />
        )}
        {view === 'series-create' && shelf && data && (
          <SeriesCreationPage
            shelf={shelf}
            profile={data.profile}
            onboardingSeed={firstSeriesSeed?.shelfId === shelf.id ? firstSeriesSeed : null}
            onCancel={() => openShelf(shelf)}
            onCreate={async (body, idempotencyKey, onProgress) => {
              const navigationVersion = routeRequestVersion.current;
              const ready = await run('正在准备目录和第一节…', async () => {
                const value = await api.createPlan(
                  { ...body, shelfId: shelf.id },
                  idempotencyKey,
                );
                onProgress('first-section');
                return waitForInitialSectionReady(value, onProgress);
              });
              // Keep the completed transition legible when a local/demo task resolves instantly.
              await new Promise((resolve) => window.setTimeout(resolve, 650));
              if (navigationVersion !== routeRequestVersion.current) return;
              setDirectoryBookId(ready.bookId);
              setSeries(ready.series);
              setSection(ready.section);
              setView('learn');
              updateBrowserLocation(
                seriesPath(ready.series.id, ready.section.id),
                'push',
              );
            }}
          />
        )}
        {view === 'profile' && data && (
          <ProfileCenterPage
            user={auth.user}
            mode={auth.mode}
            profile={data.profile}
            stats={{
              shelves: data.shelves.length,
              series: data.shelves.reduce((total, item) => total + item.series.length, 0),
            }}
            section={profileSection}
            onSectionChange={changeProfileSection}
            onBack={goHome}
            onSave={async (body) => {
              await run('正在更新学习画像…', () => api.updateProfile(body));
              setData(await api.bootstrap());
            }}
            onLogout={logout}
            onRotateRecoveryCode={async (currentPassword) => (await api.rotateRecoveryCode(currentPassword)).recoveryCode}
            privacy={auth.privacy}
            onRequestExit={requestAccountExit}
          />
        )}
        {view === 'knowledge' && data && (
          <KnowledgeMapPage
            series={data.shelves.flatMap((item) => item.series.map((entry) => ({
              id: entry.id,
              title: entry.title,
              shelfName: item.name,
            })))}
            onBack={goHome}
            onOpenReview={openReviewCenter}
          />
        )}
        {view === 'review' && (
          <ReviewCenterPage
            onBack={goHome}
            onOpenLearning={(seriesId, sectionId) => (
              openSeries(seriesId, sectionId, 'push', null, true)
            )}
          />
        )}
        {view === 'learn' && series && (
          <>
            {(pathNeedsDecision
              || (series.initializationTask && series.initializationTask.status !== 'succeeded')) && (
              <div className="workspace-alert-stack">
                {pathNeedsDecision && activeMilestonePath && (
                  <PathDecisionBanner
                    path={activeMilestonePath}
                    goalStatement={data?.milestoneDashboard.goal.statement || ''}
                    busy={busy === '正在确认学习路径…'}
                    onDismiss={() => setDismissedPathAlertSeriesId(pathDecisionKey)}
                    onReviewGoal={() => openProfileCenter('profile')}
                    onConfirm={async () => {
                      await run('正在确认学习路径…', async () => {
                        await api.confirmMilestonePath(series.id);
                        setData(await api.bootstrap());
                      });
                    }}
                  />
                )}
                {series.initializationTask
                  && series.initializationTask.status !== 'succeeded' && (
                <div
                  className={`initial-task-alert ${series.initializationTask.status}`}
                role={series.initializationTask.status === 'failed' ? 'alert' : 'status'}
              >
                <span>
                  第一节准备{{
                    pending: '等待中',
                    running: '进行中',
                    failed: '失败',
                    succeeded: '已完成',
                  }[series.initializationTask.status]}：
                  {series.initializationTask.status === 'failed'
                    ? generationFailureMessage(series.initializationTask, '第一节内容')
                    : '准备中'}
                </span>
                {series.initializationTask.retryable && (
                  <button
                    className="secondary-button"
                    disabled={preparingInitialSection}
                    onClick={retryInitialSection}
                  >
                    {preparingInitialSection ? '正在重试…' : '重新准备第一节'}
                  </button>
                )}
              </div>
                )}
              </div>
            )}
            <LearningWorkspace
              userId={auth.user.id}
              series={series}
              section={section}
              directoryBookId={directoryBookId}
              dailyMode={activityDailyMode || data?.dailyMode?.dailyMode || 'slow'}
              onSelectSection={loadSection}
              onGenerateSection={generateSection}
              onRegenerateSection={regenerateSection}
              onGenerateChapter={openChapter}
              onActivateBook={activateBook}
              onStartNextBook={startNextBook}
              chapterGenerationDisabled={preparingInitialSection}
              generatingChapterId={generatingChapterId}
              onSectionChange={setSection}
              onRefreshSeries={refreshSeries}
              onDeleteBook={async (bookId) => {
              const deletingLastBook = series.books.length === 1;
              const deletingCurrentBook = series.books.some(
                (book) => book.id === bookId && book.chapters.some(
                  (chapter) => chapter.sections.some((item) => item.id === section?.id),
                ),
              );
              await run('正在从书架移除书籍…', async () => {
                await api.deleteBook(bookId);
                const refreshed = await api.bootstrap();
                const refreshedShelf = shelf
                  ? refreshed.shelves.find((item) => item.id === shelf.id) || null
                  : null;
                setData(refreshed);
                setShelf(refreshedShelf);
                if (deletingLastBook) {
                  setSeries(null);
                  setSection(null);
                  updateBrowserLocation(
                    refreshedShelf ? shelfPath(refreshedShelf.id) : '/',
                    'replace',
                  );
                  setView(refreshedShelf ? 'shelf' : 'home');
                  return;
                }
                const updated = await api.series(series.id);
                setSeries(updated);
                if (deletingCurrentBook) {
                  const initial = firstUsableSection(updated);
                  if (initial) await loadSection(initial, 'push', false);
                  else setSection(null);
                }
              });
              }}
              onFeedbackBlock={(block) => {
                if (!section?.content) return;
                setFeedbackTarget({
                  scope: 'content_block',
                  sectionId: section.id,
                  contentVersionId: section.content.id,
                  block,
                });
              }}
              onGlobalFeedback={() => setFeedbackTarget({ scope: 'global' })}
              onReturnHome={goHome}
              onQaVisibilityChange={setLearningQaOpen}
            />
          </>
        )}
      </main>
      {!['learn', 'series-create'].includes(view) && (
        <nav className="mobile-primary-nav" aria-label="移动端主导航">
          <button
            type="button"
            className={view === 'home' || view === 'shelf' ? 'is-active' : ''}
            aria-current={view === 'home' || view === 'shelf' ? 'page' : undefined}
            onClick={goHome}
          >
            <span aria-hidden="true">书</span><b>书架</b>
          </button>
          <button
            type="button"
            className={view === 'review' ? 'is-active' : ''}
            aria-current={view === 'review' ? 'page' : undefined}
            onClick={openReviewCenter}
          >
            <span aria-hidden="true">复</span><b>复习</b>
          </button>
          <button
            type="button"
            className={view === 'knowledge' ? 'is-active' : ''}
            aria-current={view === 'knowledge' ? 'page' : undefined}
            onClick={openKnowledgeMap}
          >
            <span aria-hidden="true">图</span><b>版图</b>
          </button>
          <button
            type="button"
            className={view === 'profile' ? 'is-active' : ''}
            aria-current={view === 'profile' ? 'page' : undefined}
            onClick={() => openProfileCenter('profile')}
          >
            <span aria-hidden="true">我</span><b>我的</b>
          </button>
        </nav>
      )}
      {bookReplan && (
        <BookReplanDialog
          book={bookReplan.book}
          proposal={bookReplan.proposal}
          status={bookReplan.status}
          onClose={() => {
            bookReplanRequestVersion.current += 1;
            setBookReplan(null);
          }}
          onRetry={() => activateBook(
            bookReplan.book,
            bookReplan.feedback,
            bookReplan.previousProposalId,
          )}
          onRevise={(feedback) => activateBook(
            bookReplan.book,
            feedback,
            bookReplan.proposal?.proposalId,
          )}
          onConfirm={async () => {
            const proposal = bookReplan.proposal;
            if (!proposal) return;
            await run(
              '正在确认新章节目录…',
              () => api.confirmBookReplan(bookReplan.book.id, proposal.proposalId),
            );
            await refreshSeries();
            const refreshed = await api.bootstrap();
            setData(refreshed);
            if (shelf) {
              setShelf(refreshed.shelves.find((item) => item.id === shelf.id) || null);
            }
            setNotice('新目录已采用，这本书现在可以开始学习。');
          }}
        />
      )}
      {showDailyModeDialog && data?.dailyMode && (
        <DailyModeDialog
          state={data.dailyMode}
          busy={dailyModeBusy}
          onActivate={activateDailyMode}
        />
      )}
      {AI_RUNTIME_SETTINGS_ENABLED && showAiSettings && (
        <AiSettingsDialog onClose={() => setShowAiSettings(false)} />
      )}
      {!(view === 'learn' && section) && !learningQaOpen && (
        <button
          className="global-feedback-tab"
          aria-label="反馈产品问题或建议"
          onClick={() => setFeedbackTarget({ scope: 'global' })}
        >
          <span aria-hidden="true">✦</span> 反馈
        </button>
      )}
      {feedbackTarget && (
        <FeedbackDialog
          target={feedbackTarget}
          view={view}
          onSectionChange={(updated) => {
            setSection((current) => current?.id === updated.id ? updated : current);
          }}
          onRefreshSeries={refreshSeries}
          onRepairBackgrounded={() => setNotice('反馈已收到，正文正在后台更新，你可以继续学习。')}
          onRepairSettled={(updated) => setNotice(updated
            ? '正文已按你的反馈完成更新。'
            : '反馈已收到，但正文这次没有更新；原内容保持不变，可稍后重试。')}
          onClose={() => setFeedbackTarget(null)}
        />
      )}
    </div>
  );
}

function BookReplanDialog({
  book,
  proposal,
  status,
  onClose,
  onRetry,
  onRevise,
  onConfirm,
}: {
  book: Book;
  proposal: BookReplanProposal | null;
  status: BookReplanState['status'];
  onClose: () => void;
  onRetry: () => Promise<void>;
  onRevise: (feedback: string) => Promise<void>;
  onConfirm: () => Promise<void>;
}) {
  const [confirming, setConfirming] = useState(false);
  const [feedback, setFeedback] = useState('');
  const dialogRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    dialogRef.current?.focus();
    const handleKeys = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !confirming) {
        onClose();
        return;
      }
      if (event.key !== 'Tab' || !dialogRef.current) return;
      const focusable = Array.from(dialogRef.current.querySelectorAll<HTMLElement>('button:not(:disabled)'));
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && (document.activeElement === first || document.activeElement === dialogRef.current)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', handleKeys);
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener('keydown', handleKeys);
    };
  }, [confirming, onClose]);

  return (
    <div
      className="confirm-backdrop book-replan-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !confirming) onClose();
      }}
    >
      <section
        ref={dialogRef}
        className="book-replan-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="book-replan-title"
        tabIndex={-1}
      >
        <header className="book-replan-heading">
          <div>
            <p className="eyebrow">下一本书 · {status === 'ready' ? '目录预览' : '准备目录'}</p>
            <h2 id="book-replan-title">
              {status === 'ready'
                ? `开始《${book.title}》前，先看一眼新目录`
                : `正在准备《${book.title}》的新目录`}
            </h2>
          </div>
          <button className="dialog-close" type="button" aria-label="关闭目录预览" disabled={confirming} onClick={onClose}>×</button>
        </header>

        {status === 'preparing' ? (
          <div className="book-replan-preparing" role="status" aria-live="polite">
            <span className="book-replan-progress-mark" aria-hidden="true"><i /><i /><i /></span>
            <div>
              <b>正在结合最近的学习表现调整章节</b>
              <p>准备完成后，新的章节顺序和学习重点会直接出现在这里；只调整本书尚未开始的章节，不会改变书单或系列。</p>
            </div>
          </div>
        ) : status === 'failed' ? (
          <div className="book-replan-failure" role="alert">
            <b>这次没有准备好新目录</b>
            <p>当前章节没有变化。请检查网络后重新准备，或先关闭稍后再试。</p>
          </div>
        ) : proposal ? (
          <>
            <ol className="book-replan-outline">
              {proposal.chapters.map((chapter, index) => (
                <li key={`${chapter.title}-${index}`}>
                  <span>{String(index + 1).padStart(2, '0')}</span>
                  <div>
                    <b>{chapter.title}</b>
                    <p>{chapter.objective}</p>
                  </div>
                </li>
              ))}
            </ol>
            <section className="book-replan-feedback" aria-labelledby="book-replan-feedback-title">
              <div>
                <span>和这版目录继续讨论</span>
                <b id="book-replan-feedback-title">哪里不对，直接指出来</b>
                <p>可以点名某一章，要求加深、删减、换顺序或补上遗漏；系统会返回下一版，不会直接采用。</p>
              </div>
              <textarea
                value={feedback}
                maxLength={3000}
                rows={4}
                placeholder="例如：第 2 章太泛。不要罗列共享方案，改成从训练任务的隔离目标出发，对比 time-slicing、MIG 和 vGPU 的机制与边界。"
                aria-label="对这版目录的修改意见"
                onChange={(event) => setFeedback(event.target.value)}
                onKeyDown={(event) => {
                  if ((event.metaKey || event.ctrlKey) && event.key === 'Enter' && feedback.trim()) {
                    event.preventDefault();
                    const instruction = feedback.trim();
                    setFeedback('');
                    void onRevise(instruction);
                  }
                }}
              />
              <footer>
                <small>{feedback.length}/3000 · {navigator.platform.includes('Mac') ? '⌘' : 'Ctrl'} + Enter</small>
                <button
                  className="secondary-button"
                  type="button"
                  disabled={!feedback.trim()}
                  onClick={() => {
                    const instruction = feedback.trim();
                    if (!instruction) return;
                    setFeedback('');
                    void onRevise(instruction);
                  }}
                >
                  按我的意见重做
                </button>
              </footer>
            </section>
          </>
        ) : null}

        <footer className="dialog-actions">
          <button className="quiet-button" type="button" disabled={confirming} onClick={onClose}>
            {status === 'ready' ? '稍后再说' : '先关闭'}
          </button>
          {status === 'failed' && (
            <button className="primary-button" type="button" onClick={() => void onRetry()}>重新准备</button>
          )}
          {status === 'ready' && (
            <button
              className="primary-button"
              type="button"
              disabled={confirming}
              onClick={async () => {
                setConfirming(true);
                let confirmed = false;
                try {
                  await onConfirm();
                  confirmed = true;
                } finally {
                  setConfirming(false);
                }
                if (confirmed) onClose();
              }}
            >
              {confirming ? '正在采用…' : '采用这份目录'}
            </button>
          )}
        </footer>
      </section>
    </div>
  );
}

function FeedbackTypeDropdown({
  options,
  value,
  disabled,
  onChange,
}: {
  options: string[][];
  value: string;
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const selectedIndex = Math.max(0, options.findIndex(([optionValue]) => optionValue === value));
  const selectedLabel = options[selectedIndex]?.[1] || '';

  useEffect(() => {
    if (!open) return;
    const closeOutside = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      setOpen(false);
      triggerRef.current?.focus();
    };
    document.addEventListener('mousedown', closeOutside);
    document.addEventListener('keydown', closeOnEscape);
    const focusFrame = window.requestAnimationFrame(() => optionRefs.current[selectedIndex]?.focus());
    return () => {
      window.cancelAnimationFrame(focusFrame);
      document.removeEventListener('mousedown', closeOutside);
      document.removeEventListener('keydown', closeOnEscape);
    };
  }, [open, selectedIndex]);

  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);

  const moveFocus = (index: number) => {
    const nextIndex = (index + options.length) % options.length;
    optionRefs.current[nextIndex]?.focus();
  };

  const choose = (optionValue: string) => {
    onChange(optionValue);
    setOpen(false);
    window.requestAnimationFrame(() => triggerRef.current?.focus());
  };

  return (
    <div className={`feedback-select-control ${open ? 'is-open' : ''}`} ref={rootRef}>
      <button
        ref={triggerRef}
        className="feedback-select-trigger"
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={`反馈类型：${selectedLabel}`}
        disabled={disabled}
        onClick={() => setOpen((current) => !current)}
        onKeyDown={(event) => {
          if (!['ArrowDown', 'ArrowUp'].includes(event.key)) return;
          event.preventDefault();
          setOpen(true);
        }}
      >
        <span>{selectedLabel}</span>
        <i aria-hidden="true" />
      </button>
      {open && (
        <div className="feedback-select-menu" role="listbox" aria-label="反馈类型">
          {options.map(([optionValue, label], index) => {
            const selected = optionValue === value;
            return (
              <button
                ref={(node) => { optionRefs.current[index] = node; }}
                className={`feedback-select-option ${selected ? 'selected' : ''}`}
                type="button"
                role="option"
                aria-selected={selected}
                tabIndex={-1}
                key={optionValue}
                onClick={() => choose(optionValue)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    choose(optionValue);
                  } else if (event.key === 'ArrowDown') {
                    event.preventDefault();
                    moveFocus(index + 1);
                  } else if (event.key === 'ArrowUp') {
                    event.preventDefault();
                    moveFocus(index - 1);
                  } else if (event.key === 'Home') {
                    event.preventDefault();
                    moveFocus(0);
                  } else if (event.key === 'End') {
                    event.preventDefault();
                    moveFocus(options.length - 1);
                  } else if (event.key === 'Tab') {
                    setOpen(false);
                  }
                }}
              >
                <span className="feedback-select-indicator" aria-hidden="true">{selected ? '✓' : ''}</span>
                <span>{label}</span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

function FeedbackDialog({
  target,
  view,
  onSectionChange,
  onRefreshSeries,
  onRepairBackgrounded,
  onRepairSettled,
  onClose,
}: {
  target: FeedbackTarget;
  view: View;
  onSectionChange: (section: Section) => void;
  onRefreshSeries: () => Promise<void>;
  onRepairBackgrounded: () => void;
  onRepairSettled: (updated: boolean) => void;
  onClose: () => void;
}) {
  const options = target.scope === 'content_block'
    ? [
        ['inaccurate', '内容不准确'],
        ['unclear', '没有讲清楚'],
        ['poor_example', '例子不合适'],
        ['typo', '错别字'],
        ['layout', '排版有问题'],
        ['other', '其他'],
      ]
    : [
        ['bug', '遇到问题'],
        ['feature', '功能建议'],
        ['experience', '体验感受'],
        ['other', '其他'],
      ];
  const [feedbackType, setFeedbackType] = useState(options[0][0]);
  const [message, setMessage] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [repairing, setRepairing] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [repairText, setRepairText] = useState('');
  const [repairFeedbackId, setRepairFeedbackId] = useState('');
  const [repairFailed, setRepairFailed] = useState(false);
  const [status, setStatus] = useState('');
  const updatesLesson = target.scope === 'content_block'
    && ['unclear', 'poor_example', 'typo', 'layout'].includes(feedbackType);
  const dialogRef = useRef<HTMLElement | null>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const submittingRef = useRef(submitting);
  const repairingRef = useRef(repairing);
  const onCloseRef = useRef(onClose);
  const onRepairBackgroundedRef = useRef(onRepairBackgrounded);
  const onRepairSettledRef = useRef(onRepairSettled);
  const backgroundedRef = useRef(false);
  const closeTimerRef = useRef<number | undefined>(undefined);
  const submissionRef = useRef({ payload: '', key: '' });
  submittingRef.current = submitting;
  repairingRef.current = repairing;
  onCloseRef.current = onClose;
  onRepairBackgroundedRef.current = onRepairBackgrounded;
  onRepairSettledRef.current = onRepairSettled;

  const closeDialog = () => {
    if (repairingRef.current) {
      backgroundedRef.current = true;
      onRepairBackgroundedRef.current();
    }
    onCloseRef.current();
  };

  useEffect(() => {
    const dialog = dialogRef.current;
    const activeElement = document.activeElement;
    returnFocusRef.current = activeElement instanceof HTMLElement ? activeElement : null;
    const initialFocus = target.scope === 'global'
      ? dialog?.querySelector<HTMLElement>('textarea')
      : dialog?.querySelector<HTMLElement>('.feedback-select-trigger');
    (initialFocus || dialog)?.focus();

    const handleDialogKeys = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        if (!submittingRef.current) closeDialog();
        return;
      }
      if (event.key !== 'Tab' || !dialog) return;
      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(
        'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [href], [tabindex]:not([tabindex="-1"])',
      )).filter((element) => !element.hasAttribute('hidden'));
      if (focusable.length === 0) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      } else if (!dialog.contains(document.activeElement)) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', handleDialogKeys);
    return () => {
      document.removeEventListener('keydown', handleDialogKeys);
      if (closeTimerRef.current !== undefined) window.clearTimeout(closeTimerRef.current);
      if (returnFocusRef.current?.isConnected) returnFocusRef.current.focus();
    };
  }, [target.scope]);

  const streamRepair = async (feedbackId: string) => {
    setRepairing(true);
    setSubmitted(true);
    setRepairFailed(false);
    setRepairText('');
    setStatus('反馈已收到，正在准备本节新版本。你可以关闭窗口继续学习。');
    try {
      await api.streamFeedbackRepair(
        feedbackId,
        (delta) => setRepairText((current) => current + delta),
      );
      if (target.scope === 'content_block') {
        const updated = await api.section(target.sectionId);
        onSectionChange(updated);
        await onRefreshSeries();
        setStatus('本节新版本已完成更新。');
        if (backgroundedRef.current) onRepairSettledRef.current(true);
      }
    } catch (reason) {
      setRepairFailed(true);
      setStatus(reason instanceof Error
        ? `反馈已收到，但这次更新没有完成：${reason.message}。原正文保持不变。`
        : '反馈已收到，但这次更新没有完成。原正文保持不变，请稍后重试。');
      if (backgroundedRef.current) onRepairSettledRef.current(false);
    } finally {
      setRepairing(false);
    }
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setSubmitting(true);
    setStatus('');
    try {
      const payload = {
        scope: target.scope,
        feedbackType,
        message,
        pagePath: window.location.pathname,
        view,
        ...(target.scope === 'content_block' ? {
          sectionId: target.sectionId,
          contentVersionId: target.contentVersionId,
          blockId: target.block.id,
        } : {}),
      };
      const serializedPayload = JSON.stringify(payload);
      if (submissionRef.current.payload !== serializedPayload) {
        submissionRef.current = {
          payload: serializedPayload,
          key: crypto.randomUUID(),
        };
      }
      const receipt = await api.submitFeedback(payload, submissionRef.current.key);
      setSubmitted(true);
      setSubmitting(false);
      if (target.scope === 'global') {
        setStatus('已收到。');
        closeTimerRef.current = window.setTimeout(() => onCloseRef.current(), 900);
        return;
      }
      if (receipt.regeneration.status === 'stream_ready') {
        setRepairFeedbackId(receipt.id);
        await streamRepair(receipt.id);
        return;
      }
      const blockedMessages: Record<string, string> = {
        FEEDBACK_CONTENT_VERSION_STALE: '当前正文已经更新。请刷新页面后，在最新正文上重新反馈。',
        SECTION_CONTENT_MISSING: '这段正文已不可用，请刷新页面后重试。',
        FEEDBACK_ACCURACY_REVIEW_REQUIRED: '已记录。为避免未经核实地改写，原正文保持不变。',
        FEEDBACK_CLASSIFICATION_REQUIRED: '已记录。需先确认问题类型，因此原正文保持不变。',
        FEEDBACK_ASSESSED_VERSION_FROZEN: '已记录。你已经在这份正文上完成过验证，当前学习记录不会被静默替换。',
      };
      setStatus(
        blockedMessages[receipt.regeneration.reasonCode || '']
        || '反馈已记录，但当前版本暂时不能自动更新。',
      );
    } catch (reason) {
      setStatus(reason instanceof Error ? reason.message : '反馈没有提交成功，请稍后重试。');
      setSubmitting(false);
    }
  };

  return (
    <div
      className="confirm-backdrop feedback-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !submitting) closeDialog();
      }}
    >
      <section
        className="feedback-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="feedback-title"
        ref={dialogRef}
        tabIndex={-1}
      >
        <header>
          <div>
            <p className="eyebrow">{target.scope === 'content_block' ? '内容反馈' : '告诉我们你的感受'}</p>
            <h2 id="feedback-title">{target.scope === 'content_block' ? '报告这一段的问题' : '全局反馈'}</h2>
          </div>
          <button className="dialog-close" type="button" aria-label="关闭反馈" disabled={submitting} onClick={closeDialog}>×</button>
        </header>
        {target.scope === 'content_block' && (
          <div className="feedback-block-preview">
            <span>{target.block.heading}</span>
            <p>{target.block.content.replace(/[#*_`>|]/g, '').slice(0, 150)}</p>
          </div>
        )}
        <form onSubmit={submit}>
          {!submitted && <div className="feedback-type-select">
            <span>这次想反馈什么？</span>
            <FeedbackTypeDropdown
              options={options}
              value={feedbackType}
              disabled={submitting}
              onChange={setFeedbackType}
            />
          </div>}
          {!submitted && <label className="feedback-message-label">
            {target.scope === 'content_block' ? '补充说明（可选）' : '具体说说'}
            <textarea
              value={message}
              maxLength={4000}
              required={target.scope === 'global' || feedbackType === 'other'}
              placeholder={target.scope === 'content_block' ? '哪里不对，或者怎样会更容易理解？' : '遇到了什么，或者希望我们改进什么？'}
              onChange={(event) => setMessage(event.target.value)}
              disabled={submitting}
            />
            <small>请勿填写密码、API Key 或其他敏感信息 · {message.length}/4000</small>
          </label>}
          {!submitted && updatesLesson && (
            <p className="feedback-update-impact">发送后会生成并切换到本节新版本，正文与验证题会一起更新；已有标注仍保留在原来的正文版本。</p>
          )}
          {submitted && target.scope === 'content_block' && (
            <div className={`feedback-repair-answer ${repairFailed ? 'failed' : ''}`} aria-live="polite">
              {repairText ? (
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{repairText}</ReactMarkdown>
              ) : repairing ? (
                <span className="feedback-repair-listening">正在准备本节新版本<span aria-hidden="true">…</span></span>
              ) : null}
              {repairing && repairText && <i className="stream-caret" aria-hidden="true" />}
            </div>
          )}
          {status && <p className="feedback-status" role="status">{status}</p>}
          <div className="dialog-actions">
            <button type="button" className="quiet-button" disabled={submitting} onClick={closeDialog}>
              {repairing ? '继续学习' : submitted ? '关闭' : '取消'}
            </button>
            {repairFailed && repairFeedbackId ? (
              <button type="button" className="primary-button" disabled={repairing} onClick={() => void streamRepair(repairFeedbackId)}>
                {repairing ? '正在更新…' : '重试更新'}
              </button>
            ) : !submitted ? (
              <button className="primary-button" disabled={submitting || ((target.scope === 'global' || feedbackType === 'other') && message.trim().length < 2)}>
                {submitting ? '正在送出…' : updatesLesson ? '发送并更新本节' : '发送反馈'}
              </button>
            ) : null}
          </div>
        </form>
      </section>
    </div>
  );
}

function AiSettingsDialog({ onClose }: { onClose: () => void }) {
  const [runtime, setRuntime] = useState<AiRuntime | null>(null);
  const [mode, setMode] = useState<'provider' | 'demo'>('provider');
  const [apiKey, setApiKey] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [model, setModel] = useState('gpt-5');
  const [providerProtocol, setProviderProtocol] = useState<'openai' | 'anthropic'>('openai');
  const [apiMode, setApiMode] = useState<'responses' | 'chat_completions'>('responses');
  const [reasoningMode, setReasoningMode] = useState<'optional' | 'required' | 'disabled'>('optional');
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState('');

  useEffect(() => {
    api.aiRuntime()
      .then((value) => {
        setRuntime(value);
        setMode(value.mode === 'demo' ? 'demo' : 'provider');
        setBaseUrl(value.baseUrl);
        setModel(value.providerModel);
        setProviderProtocol(value.providerProtocol);
        if (value.apiMode !== 'messages') setApiMode(value.apiMode);
        setReasoningMode(value.reasoningMode);
      })
      .catch((reason) => setMessage(reason instanceof Error ? reason.message : '读取 AI 设置失败'));
  }, []);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !saving) onClose();
    };
    document.addEventListener('keydown', closeOnEscape);
    return () => document.removeEventListener('keydown', closeOnEscape);
  }, [onClose, saving]);

  const save = async (event: FormEvent) => {
    event.preventDefault();
    setSaving(true);
    setMessage(mode === 'provider' ? '正在验证新配置…' : '正在切换…');
    try {
      const value = await api.updateAiRuntime({
        mode,
        apiKey: apiKey.trim() || undefined,
        baseUrl: mode === 'provider' ? baseUrl.trim() : '',
        model: mode === 'provider' ? model.trim() : 'local-demo-v1',
        providerProtocol,
        apiMode,
        reasoningMode,
      });
      setRuntime(value);
      setApiKey('');
      setMessage(
        value.mode === 'demo'
          ? '已保存并切换到本地演示模式。'
          : `已切换到 ${value.model}。`,
      );
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : '切换失败');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      className="confirm-backdrop ai-settings-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !saving) onClose();
      }}
    >
      <section className="ai-settings-dialog" role="dialog" aria-modal="true" aria-labelledby="ai-settings-title">
        <div className="ai-settings-heading">
          <div>
            <p className="eyebrow">仅限本机</p>
            <h2 id="ai-settings-title">运行时 AI 设置</h2>
          </div>
          <button className="dialog-close" aria-label="关闭 AI 设置" disabled={saving} onClick={onClose}>×</button>
        </div>

        <div className={`runtime-status ${runtime?.configured ? 'online' : 'demo'}`}>
          <span />
          <div>
            <b>{runtime ? (runtime.mode === 'demo' ? '本地演示模式' : runtime.model) : '正在读取当前配置…'}</b>
            <small>{runtime?.configured ? (runtime.baseUrl || 'OpenAI 官方接口') : '不产生真实 AI 内容'}</small>
          </div>
        </div>

        <form className="ai-settings-form" onSubmit={save}>
          <fieldset disabled={saving}>
            <legend>运行模式</legend>
            <label className={mode === 'provider' ? 'selected' : ''}>
              <input type="radio" checked={mode === 'provider'} onChange={() => setMode('provider')} />
              <span><b>真实模型</b><small>使用服务端 API Key 调用模型</small></span>
            </label>
            <label className={mode === 'demo' ? 'selected' : ''}>
              <input type="radio" checked={mode === 'demo'} onChange={() => setMode('demo')} />
              <span><b>本地演示</b><small>明确标记为 Demo，不调用外部模型</small></span>
            </label>
          </fieldset>

          {mode === 'provider' && (
            <div className="provider-fields">
              <label>
                API Key
                <input
                  type="password"
                  value={apiKey}
                  autoComplete="new-password"
                  placeholder={runtime?.apiKeyStored ? '留空则沿用当前 Key' : '输入新的 API Key'}
                  onChange={(event) => setApiKey(event.target.value)}
                />
                <small>Key 只提交给本机 API，不会由服务端返回。</small>
              </label>
              <label>
                Base URL
                <input
                  value={baseUrl}
                  placeholder={providerProtocol === 'openai' ? '留空使用 OpenAI 官方接口' : '留空使用 Anthropic 官方接口'}
                  onChange={(event) => setBaseUrl(event.target.value)}
                />
              </label>
              <label>
                模型
                <input value={model} required onChange={(event) => setModel(event.target.value)} />
              </label>
              <label>
                供应商协议
                <select
                  value={providerProtocol}
                  onChange={(event) => {
                    const next = event.target.value as typeof providerProtocol;
                    setProviderProtocol(next);
                    setBaseUrl((current) => {
                      if (next === 'anthropic' && current.endsWith('/compatible-mode/v1')) {
                        return current.replace(/\/compatible-mode\/v1$/, '/apps/anthropic');
                      }
                      if (next === 'openai' && current.endsWith('/apps/anthropic')) {
                        return current.replace(/\/apps\/anthropic$/, '/compatible-mode/v1');
                      }
                      return current;
                    });
                  }}
                >
                  <option value="openai">OpenAI 兼容 API</option>
                  <option value="anthropic">Anthropic 兼容 API</option>
                </select>
              </label>
              {providerProtocol === 'openai' ? (
                <label>
                  OpenAI 接口形态
                  <select value={apiMode} onChange={(event) => setApiMode(event.target.value as typeof apiMode)}>
                    <option value="responses">Responses API</option>
                    <option value="chat_completions">Chat Completions</option>
                  </select>
                </label>
              ) : (
                <p className="runtime-warning">Anthropic 兼容协议固定使用 Messages API；Base URL 不要包含尾部 `/v1`。</p>
              )}
              <label>
                推理模式
                <select value={reasoningMode} onChange={(event) => setReasoningMode(event.target.value as typeof reasoningMode)}>
                  <option value="optional">可选</option>
                  <option value="required">必须启用</option>
                  <option value="disabled">禁用</option>
                </select>
              </label>
            </div>
          )}

          <p className="runtime-warning">
            {runtime?.ephemeral
              ? '当前环境不提供持久化存储；重启后会恢复服务器环境变量中的配置。'
              : '配置仅保存在本机服务端，浏览器无法读取 API Key。'}
            保存前会先验证连接，失败时继续使用旧配置。
          </p>
          {message && <p className="runtime-message" role="status">{message}</p>}
          <div className="dialog-actions">
            <button type="button" className="quiet-button" disabled={saving} onClick={onClose}>取消</button>
            <button className="primary-button" disabled={saving || !runtime}>
              {saving ? '正在验证…' : '验证并切换'}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}

function shelfDescriptor(shelf: Pick<Shelf, 'tags'>) {
  return shelf.tags.slice(0, 2).join(' · ');
}

function bookProgressDetails(book: Book) {
  const completedChapters = book.chapters.filter((chapter) => chapter.status === 'completed').length;
  const sections = book.chapters.flatMap((chapter) => chapter.sections);
  const completedSections = sections.filter((section) => section.status === 'completed').length;
  return {
    completedChapters,
    totalChapters: book.chapters.length,
    completedSections,
    totalSections: sections.length,
  };
}

function bookProgressCopy(
  book: Book,
  currentSectionId?: string | null,
  showCurrentPosition = book.progress > 0,
) {
  const details = bookProgressDetails(book);
  if (details.totalChapters === 0) return '章节待确认';
  if (book.status === 'completed') {
    return `${details.completedChapters}/${details.totalChapters} 章 · ${details.completedSections}/${details.totalSections} 节`;
  }

  const currentChapter = book.chapters.find((chapter) => (
    currentSectionId && chapter.sections.some((section) => section.id === currentSectionId)
  ));
  const currentSection = currentChapter?.sections.find((section) => section.id === currentSectionId);
  if (currentChapter && currentSection && showCurrentPosition) {
    return `读到第 ${currentChapter.position}/${details.totalChapters} 章 · 本章第 ${currentSection.position}/${currentChapter.sections.length} 节`;
  }

  return `共 ${details.totalChapters} 章`;
}

function bookProgressLabel(book: Book, isCurrent: boolean) {
  if (book.status === 'completed') return '已完成';
  if (book.status === 'locked') return '未解锁';
  if (book.outlineStatus === 'draft') return '待确认';
  if (isCurrent || book.progress > 0) return '学习中';
  return '待开始';
}

function bookReadyForOutlineReview(series: Series, bookIndex: number) {
  const book = series.books[bookIndex];
  if (!book || book.outlineStatus !== 'draft') return false;
  return bookIndex === 0 || series.books[bookIndex - 1]?.status === 'completed';
}

function nextBookSection(book: Book) {
  for (const chapter of book.chapters) {
    const section = chapter.sections.find((item) => item.status !== 'locked' && item.status !== 'completed');
    if (section) return { chapter, section };
  }
  return null;
}

function bookContainsSection(book: Book, sectionId: string | null | undefined) {
  return Boolean(
    sectionId
    && book.chapters.some((chapter) => (
      chapter.sections.some((section) => section.id === sectionId)
    )),
  );
}

const studyActivityLabels = {
  reading_thinking: '阅读与思考',
  verification_review: '验证与复习',
  ask_ai: 'Ask AI',
} as const;

function studyMinutes(seconds: number) {
  if (seconds <= 0) return '0';
  return String(Math.max(1, Math.round(seconds / 60)));
}

function StudyTimeSummary({
  summary,
  loading,
}: {
  summary: StudyActivitySummary | null;
  loading: boolean;
}) {
  const [view, setView] = useState<'activity' | 'timeline'>('activity');
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  const [pinned, setPinned] = useState(false);
  const shellRef = useRef<HTMLDivElement>(null);
  const open = hovered || focused || pinned;

  useEffect(() => {
    if (!pinned) return undefined;
    const close = (event: MouseEvent) => {
      if (!shellRef.current?.contains(event.target as Node)) setPinned(false);
    };
    const closeOnEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') setPinned(false);
    };
    document.addEventListener('mousedown', close);
    document.addEventListener('keydown', closeOnEscape);
    return () => {
      document.removeEventListener('mousedown', close);
      document.removeEventListener('keydown', closeOnEscape);
    };
  }, [pinned]);

  const totalSeconds = summary?.totalSeconds || 0;
  const categories = summary?.categories.filter((item) => item.seconds > 0) || [];
  const episodes = summary?.episodes || [];
  const localTime = (value: string) => new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(new Date(value));

  return (
    <div
      className="study-time-shell"
      ref={shellRef}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      onFocus={() => setFocused(true)}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setFocused(false);
      }}
    >
      <button
        type="button"
        className="study-time-trigger"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls="study-time-popover"
        onClick={() => setPinned((value) => !value)}
      >
        <span>今天已投入</span>
        <strong>{loading && !summary ? '—' : studyMinutes(totalSeconds)}</strong>
        <em>分钟</em>
      </button>
      {open && (
        <section
          className="study-time-popover"
          id="study-time-popover"
          role="dialog"
          aria-label="今天的学习投入"
        >
          <h2>今天的学习投入</h2>
          <div className="study-time-view-switch" role="tablist" aria-label="学习投入查看方式">
            <button
              type="button"
              role="tab"
              aria-selected={view === 'activity'}
              className={view === 'activity' ? 'active' : ''}
              onClick={() => setView('activity')}
            >按活动</button>
            <button
              type="button"
              role="tab"
              aria-selected={view === 'timeline'}
              className={view === 'timeline' ? 'active' : ''}
              onClick={() => setView('timeline')}
            >时间线</button>
          </div>
          {view === 'activity' ? (
            categories.length ? (
              <ul className="study-time-rows">
                {categories.map((item) => (
                  <li key={item.activityKind}>
                    <span>{studyActivityLabels[item.activityKind]}</span>
                    <strong>{studyMinutes(item.seconds)} 分钟</strong>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="study-time-empty">今天还没有学习记录</p>
            )
          ) : episodes.length ? (
            <ol className="study-time-timeline">
              {episodes.map((episode) => (
                <li key={`${episode.startedAt}-${episode.endedAt}`}>
                  <time dateTime={episode.startedAt}>{localTime(episode.startedAt)}</time>
                  <span>开始</span>
                  <strong>学了 {studyMinutes(episode.durationSeconds)} 分钟</strong>
                </li>
              ))}
            </ol>
          ) : (
            <p className="study-time-empty">今天还没有学习记录</p>
          )}
        </section>
      )}
    </div>
  );
}

function useReviewWorkspace() {
  const [dueReviews, setDueReviews] = useState<DueReviews | null>(null);
  const [reviewSession, setReviewSession] = useState<ReviewSession | null>(null);
  const [reviewResult, setReviewResult] = useState<ReviewResult | CapabilityReviewResult | null>(null);
  const [reviewResponseText, setReviewResponseText] = useState('');
  const [strengtheningLaunch, setStrengtheningLaunch] = useState<StrengtheningLaunch | null>(null);
  const [strengtheningResponse, setStrengtheningResponse] = useState('');
  const [strengtheningResult, setStrengtheningResult] = useState<StrengtheningResult | null>(null);
  const [reinforcement, setReinforcement] = useState<ReinforcementRun | null>(null);
  const [reinforcementAnswer, setReinforcementAnswer] = useState<number[]>([]);
  const [reinforcementText, setReinforcementText] = useState('');
  const [reviewAnswers, setReviewAnswers] = useState<number[][]>([]);
  const [reviewBusy, setReviewBusy] = useState('正在读取到期复习…');
  const [reviewError, setReviewError] = useState('');
  const pendingReviews = dueReviews?.items.filter(
    (item) => item.status === 'presented' || item.status === 'started',
  ) || [];
  const activeReview = reviewSession
    ? dueReviews?.items.find((item) => item.assignmentId === reviewSession.assignmentId) || null
    : null;
  const currentReview = activeReview || pendingReviews[0] || null;

  const loadDueReviews = async () => {
    setReviewBusy('正在读取到期复习…');
    setReviewError('');
    try {
      setDueReviews(await api.dueReviews());
    } catch (reason) {
      setReviewError(reason instanceof Error ? reason.message : '无法读取到期复习。');
    } finally {
      setReviewBusy('');
    }
  };

  useEffect(() => {
    void loadDueReviews();
    void api.activeReinforcement().then((value) => {
      if (value) setReinforcement(value);
    }).catch(() => undefined);
  }, []);

  const startDueReview = async () => {
    if (!currentReview) return;
    setReviewBusy('正在准备一道新的复习题…');
    setReviewError('');
    try {
      const value = await api.startReview(currentReview.assignmentId);
      setReviewSession(value);
      setReviewAnswers(value.quiz?.questions.map(() => []) || []);
      setReviewResponseText('');
      setReviewResult(null);
      setReinforcement(null);
      setDueReviews((current) => current ? {
        ...current,
        items: current.items.map((item) => item.assignmentId === value.assignmentId
          ? {...item, status: 'started', quizSetId: value.quiz?.id || null}
          : item),
      } : current);
    } catch (reason) {
      setReviewError(reason instanceof Error ? reason.message : '复习题准备失败。');
    } finally {
      setReviewBusy('');
    }
  };

  const chooseReviewAnswer = (questionIndex: number, optionIndex: number, mode: 'single' | 'multiple') => {
    setReviewAnswers((current) => current.map((answer, index) => {
      if (index !== questionIndex) return answer;
      if (mode === 'single') return [optionIndex];
      return answer.includes(optionIndex)
        ? answer.filter((item) => item !== optionIndex)
        : [...answer, optionIndex].sort((left, right) => left - right);
    }));
  };

  const submitDueReview = async () => {
    if (!reviewSession) return;
    if (reviewSession.quiz && reviewAnswers.some((answer) => answer.length === 0)) return;
    if (reviewSession.capabilityTask && reviewResponseText.trim().length < 12) return;
    setReviewBusy('正在保存复习结果…');
    setReviewError('');
    try {
      const value = reviewSession.quiz
        ? await api.submitReview(
          reviewSession.assignmentId,
          reviewAnswers,
          `review-${reviewSession.assignmentId}`,
        )
        : await api.respondCapabilityReview(
          reviewSession.assignmentId,
          {answer: reviewResponseText.trim()},
          `capability-review-${reviewSession.assignmentId}`,
        );
      setReviewResult(value);
      setDueReviews((current) => current ? {
        ...current,
        items: current.items.map((item) => item.assignmentId === value.assignmentId
          ? {...item, status: 'submitted'}
          : item),
      } : current);
    } catch (reason) {
      setReviewError(reason instanceof Error ? reason.message : '复习结果保存失败。');
    } finally {
      setReviewBusy('');
    }
  };

  const skipDueReview = async () => {
    if (!currentReview) return;
    setReviewBusy('正在移出今日复习…');
    setReviewError('');
    try {
      await api.skipReview(currentReview.assignmentId);
      setDueReviews((current) => current ? {
        ...current,
        items: current.items.map((item) => item.assignmentId === currentReview.assignmentId
          ? {...item, status: 'skipped'}
          : item),
      } : current);
    } catch (reason) {
      setReviewError(reason instanceof Error ? reason.message : '无法跳过这项复习。');
    } finally {
      setReviewBusy('');
    }
  };

  const continueReviewQueue = () => {
    setReviewSession(null);
    setReviewResult(null);
    setReviewAnswers([]);
    setReviewResponseText('');
    setReinforcement(null);
    setReinforcementAnswer([]);
    setReinforcementText('');
    setStrengtheningLaunch(null);
    setStrengtheningResponse('');
    setStrengtheningResult(null);
  };

  const startStrengthening = async () => {
    if (!reviewResult) return null;
    setReviewBusy('正在打开下一阶正式任务…');
    setReviewError('');
    try {
      const value = await api.reviewStrengthening(reviewResult.assignmentId);
      setStrengtheningLaunch(value);
      setStrengtheningResponse('');
      setStrengtheningResult(null);
      return value;
    } catch (reason) {
      setReviewError(reason instanceof Error ? reason.message : '下一阶任务暂时无法打开。');
      return null;
    } finally {
      setReviewBusy('');
    }
  };

  const submitStrengthening = async () => {
    const entry = strengtheningLaunch?.entry;
    const task = entry?.task;
    if (!entry || !task || strengtheningResponse.trim().length < 12) return;
    setReviewBusy('正在按阶段标准评定…');
    setReviewError('');
    try {
      const response = {answer: strengtheningResponse.trim()};
      const key = `strengthening-${task.id}`;
      const value = entry.kind === 'transfer_task'
        ? await api.submitTransferTask(task.id, response, key)
        : await api.submitApplicationTask(task.id, response, key);
      setStrengtheningResult(value);
    } catch (reason) {
      setReviewError(reason instanceof Error ? reason.message : '强化任务评定失败。');
    } finally {
      setReviewBusy('');
    }
  };

  const startReinforcement = async () => {
    if (!reviewResult) return;
    setReviewBusy('正在为这个断点准备一条短路径…');
    setReviewError('');
    try {
      const value = await api.startReviewReinforcement(reviewResult.assignmentId);
      setReinforcement(value);
      setReinforcementAnswer([]);
      setReinforcementText('');
    } catch (reason) {
      setReviewError(reason instanceof Error ? reason.message : '补强路径准备失败。');
    } finally {
      setReviewBusy('');
    }
  };

  const submitReinforcementStep = async () => {
    const activity = reinforcement?.currentActivity;
    if (!reinforcement || !activity) return;
    setReviewBusy('正在衔接下一步…');
    setReviewError('');
    try {
      const value = await api.respondReinforcement(
        reinforcement.runId,
        {
          activityKey: activity.activityKey,
          selectedOptions: reinforcementAnswer,
          responseText: reinforcementText,
          acknowledged: activity.type === 'diagnose',
        },
        `reinforcement-${reinforcement.runId}-${reinforcement.progress.activityCount + 1}`,
      );
      setReinforcement(value);
      setReinforcementAnswer([]);
      setReinforcementText('');
    } catch (reason) {
      setReviewError(reason instanceof Error ? reason.message : '这一步暂时无法保存。');
    } finally {
      setReviewBusy('');
    }
  };

  return {
    dueReviews, pendingReviews, currentReview, reviewSession, reviewResult, reinforcement,
    reinforcementAnswer, reinforcementText, reviewAnswers, reviewResponseText,
    strengtheningLaunch, strengtheningResponse, strengtheningResult,
    reviewBusy, reviewError,
    setReinforcementAnswer, setReinforcementText, setReviewResponseText,
    setStrengtheningResponse, loadDueReviews, startDueReview,
    chooseReviewAnswer, submitDueReview, skipDueReview, continueReviewQueue,
    startReinforcement, submitReinforcementStep, startStrengthening,
    submitStrengthening,
  };
}

const capabilityStageCopy = {
  bronze: '青铜 · 说得出',
  silver: '白银 · 讲得清',
  gold: '黄金 · 做得到',
  diamond: '钻石 · 能迁移',
} as const;

function ReviewCenterPage({
  onBack,
  onOpenLearning,
}: {
  onBack: () => void;
  onOpenLearning: (seriesId:string, sectionId:string) => Promise<void>;
}) {
  const {
    pendingReviews, currentReview, reviewSession, reviewResult, reinforcement,
    reinforcementAnswer, reinforcementText, reviewAnswers, reviewResponseText,
    strengtheningLaunch, strengtheningResponse, strengtheningResult,
    reviewBusy, reviewError,
    setReinforcementAnswer, setReinforcementText, setReviewResponseText,
    setStrengtheningResponse, loadDueReviews, startDueReview,
    chooseReviewAnswer, submitDueReview, skipDueReview, continueReviewQueue,
    startReinforcement, submitReinforcementStep, startStrengthening,
    submitStrengthening,
  } = useReviewWorkspace();
  const activity = reinforcement?.currentActivity;
  const stage = strengtheningLaunch || strengtheningResult
    ? 5
    : reinforcement?.outcome
    ? 5
    : activity?.type === 'verify'
      ? 5
      : activity?.type === 'recompose'
        ? 4
        : activity?.type === 'repair'
          ? 3
          : activity?.type === 'diagnose'
            ? 2
            : reviewSession
              ? 1
              : 0;
  const reviewPassed = reviewResult
    ? ('passed' in reviewResult ? reviewResult.passed : reviewResult.reactivationQualified)
    : false;
  const nextStage = currentReview?.taskPlan.strengthening?.stage;
  const openStrengthening = async () => {
    const launch = await startStrengthening();
    if (launch?.entry?.kind === 'ask_me') {
      await onOpenLearning(launch.entry.seriesId, launch.entry.sectionId);
    }
  };

  return (
    <section className="review-center-page" aria-labelledby="review-center-title">
      <header className="review-center-hero">
        <button type="button" className="review-center-back" onClick={onBack}>← 返回书架</button>
        <div className="review-center-title-row">
          <div>
            <p className="eyebrow">RECALL STUDIO · 只处理已经学过的知识</p>
            <h1 id="review-center-title">快速找回，<br /><em>只在断点处停留。</em></h1>
            <p>按当前能力阶段完成一次独立调用检查；答不稳时只修复断点，答稳后可主动进入下一阶正式任务。</p>
          </div>
          <div className="review-center-contract">
            <span>一次 5–10 分钟</span>
            <b>唤醒不是重学，补强不是刷题。</b>
            <small>再激活只更新可调用状态；独立晋级任务才改变能力阶段。</small>
          </div>
        </div>
        <ol className="review-trace" aria-label={`当前位于第 ${stage || 1} 阶段`}>
          {['快速唤醒', '定位断点', '案例补强', '重组推理', '独立验证'].map((label, index) => (
            <li key={label} className={stage >= index + 1 ? 'is-reached' : ''}><i /><span>{label}</span></li>
          ))}
        </ol>
      </header>

      <div className="review-center-layout">
        <aside className="review-center-queue">
          <header><div><span>今日队列</span><b>{pendingReviews.length} 项</b></div><small>跨书架按到期程度排序</small></header>
          {reinforcement && (
            <button type="button" className="review-queue-item is-active">
              <i>补</i><span><b>{reinforcement.objective}</b><small>补强进行中 · 继续当前一步</small></span>
            </button>
          )}
          {!reinforcement && pendingReviews.map((item, index) => (
            <button type="button" className={`review-queue-item ${index === 0 ? 'is-active' : ''}`} key={item.assignmentId}>
              <i>{String(index + 1).padStart(2, '0')}</i><span><b>{item.capability?.label || item.objective}</b><small>{item.capability ? `${capabilityStageCopy[item.capability.currentStage as keyof typeof capabilityStageCopy] || '尚未定段'} · ${item.status === 'started' ? '已开始' : '等待唤醒'}` : item.status === 'started' ? '已开始' : '等待唤醒'}</small></span>
            </button>
          ))}
          {!reviewBusy && !reinforcement && pendingReviews.length === 0 && (
            <div className="review-queue-clear"><i>✓</i><b>今日队列已清空</b><small>下一次到期时，这里会自动出现。</small></div>
          )}
          <div className="review-method-note"><span>本次方法</span><ol><li><b>01</b> 快速过关键连接</li><li><b>02</b> 只补薄弱案例</li><li><b>03</b> 换题独立校验</li></ol></div>
        </aside>

        <main className="review-workbench" aria-live="polite">
          {reviewBusy ? (
            <div className="review-workbench-status"><i /><span>正在衔接</span><h2>{reviewBusy}</h2></div>
          ) : reviewError ? (
            <div className="review-workbench-status is-error" role="alert"><span>暂时中断</span><h2>这一步没有保存</h2><p>{reviewError}</p><button onClick={() => void loadDueReviews()}>重新读取</button></div>
          ) : strengtheningResult ? (
            <div className="review-workbench-outcome">
              <span>{strengtheningResult.evidenceEligible ? '晋级证据成立' : '本次尚未晋级'}</span>
              <h2>{strengtheningResult.evidenceEligible
                ? `已达到 ${capabilityStageCopy[strengtheningResult.capabilityStage as keyof typeof capabilityStageCopy] || strengtheningResult.capabilityStage}`
                : '能力阶段保持不变'}</h2>
              <p>{strengtheningResult.feedback}</p>
              <button onClick={continueReviewQueue}>继续今日复习 <i aria-hidden="true">→</i></button>
            </div>
          ) : strengtheningLaunch?.status === 'already_achieved' ? (
            <div className="review-workbench-outcome">
              <span>下一阶已经取得</span>
              <h2>这项能力不需要重复晋级。</h2>
              <p>画像已经包含这一级正式证据，本次复习只负责保持可调用状态。</p>
              <button onClick={continueReviewQueue}>继续今日复习 <i aria-hidden="true">→</i></button>
            </div>
          ) : strengtheningLaunch?.status === 'unavailable' ? (
            <div className="review-workbench-outcome">
              <span>本路线暂时到这里</span>
              <h2>没有虚构下一阶任务。</h2>
              <p>当前课程没有发布更高阶的正式验证机会，能力保持在已经取得的阶段。</p>
              <button onClick={continueReviewQueue}>继续今日复习 <i aria-hidden="true">→</i></button>
            </div>
          ) : strengtheningLaunch?.entry?.task ? (
            <div className="review-workbench-capability-task is-strengthening">
              <header>
                <span>主动强化 · {strengtheningLaunch.stage ? capabilityStageCopy[strengtheningLaunch.stage] : '下一阶'}</span>
                <small>独立作答后才可能晋级</small>
              </header>
              <p className="review-workbench-objective">正式任务 · <b>{strengtheningLaunch.entry.kind === 'transfer_task' ? '陌生情境迁移' : '未见过的标准应用'}</b></p>
              <h2>{strengtheningLaunch.entry.task.prompt}</h2>
              {typeof strengtheningLaunch.entry.task.taskContext.scenario === 'string' && (
                <div className="review-capability-context"><span>任务情境</span><p>{strengtheningLaunch.entry.task.taskContext.scenario}</p></div>
              )}
              {strengtheningLaunch.entry.task.deliverables.length > 0 && (
                <ul className="review-capability-deliverables">
                  {strengtheningLaunch.entry.task.deliverables.map((item) => <li key={item}>{item}</li>)}
                </ul>
              )}
              <label className="review-capability-response">
                <span>写下你的判断、过程和依据</span>
                <textarea rows={7} value={strengtheningResponse} onChange={(event) => setStrengtheningResponse(event.target.value)} placeholder="完整说明你会怎么做，以及为什么…" />
              </label>
              <button className="review-workbench-next" disabled={strengtheningResponse.trim().length < 12} onClick={() => void submitStrengthening()}>提交正式验证 <i aria-hidden="true">→</i></button>
              <p className="review-evidence-boundary"><i aria-hidden="true">◇</i>本次再激活证据不会被复用；只有这份独立提交满足下一阶全部标准时才会晋级。</p>
            </div>
          ) : reinforcement?.outcome ? (
            <div className={`review-workbench-outcome is-${reinforcement.outcome.kind}`}>
              <span>{reinforcement.outcome.kind === 'recovered' ? '连接已恢复' : '本轮已停止'}</span>
              <h2>{reinforcement.outcome.kind === 'recovered' ? '这次不是记住答案，是真正重新会用了。' : '不继续堆题，下一步拆回前置能力。'}</h2>
              <p>{reinforcement.outcome.message}</p>
              <button onClick={continueReviewQueue}>处理下一项 <i aria-hidden="true">→</i></button>
            </div>
          ) : reinforcement && activity ? (
            <div className={`review-workbench-activity is-${activity.type}`}>
              <header><span>{activity.type === 'diagnose' ? '定位断点' : activity.type === 'repair' ? '案例补强' : activity.type === 'recompose' ? '重组推理' : '独立验证'}</span><small>{reinforcement.progress.activityCount} / {reinforcement.progress.maxActivities} 步已用</small></header>
              <p className="review-workbench-objective">当前知识点 · <b>{reinforcement.objective}</b></p>
              <h2>{activity.payload.heading}</h2>
              {reinforcement.feedback && <div className={`reinforcement-feedback is-${reinforcement.feedback.kind}`}>{reinforcement.feedback.message}</div>}
              {activity.type === 'diagnose' && (
                <div className="review-agent-diagnosis">
                  <div className={`review-diagnosis-signal is-${activity.payload.hypothesis?.status || 'abstained'}`}>
                    <span>{activity.payload.hypothesis?.status === 'supported' ? '多条证据一致' : activity.payload.hypothesis?.status === 'tentative' ? '一条待验证线索' : 'Agent 暂不判断'}</span>
                    <small>不会直接写入画像</small>
                  </div>
                  <h3>{activity.payload.hypothesis?.label}</h3>
                  <p>{activity.payload.hypothesis?.message}</p>
                  <div className="review-diagnosis-next"><i aria-hidden="true">→</i><span>{activity.payload.prompt}</span></div>
                </div>
              )}
              {activity.type === 'repair' && (
                <div className="review-repair-layout">
                  <div className="review-quick-pass">
                    <span>30 秒快速过</span>
                    <div className="review-markdown review-quick-pass-content">
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>{activity.payload.content || ''}</ReactMarkdown>
                    </div>
                  </div>
                  {activity.payload.case && (
                    <div className="review-targeted-case">
                      <header><span>针对刚才断点的案例</span><small>{activity.payload.case.source}</small></header>
                      <h3>{activity.payload.case.heading}</h3>
                      <div className="review-markdown review-targeted-case-content">
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>{activity.payload.case.content}</ReactMarkdown>
                      </div>
                    </div>
                  )}
                  <label className="reinforcement-recall"><span>{activity.payload.prompt}</span><textarea value={reinforcementText} onChange={(event) => setReinforcementText(event.target.value)} placeholder="用你自己的话写一句…" rows={3} /></label>
                </div>
              )}
              {(activity.type === 'recompose' || activity.type === 'verify') && activity.payload.question && (
                <fieldset><legend>{activity.payload.question.prompt}</legend>{activity.payload.question.options.map((option, index) => (
                  <label key={index}><input type={activity.payload.question?.selectionMode === 'multiple' ? 'checkbox' : 'radio'} name={`reinforcement-${reinforcement.runId}-${activity.activityKey}`} checked={reinforcementAnswer.includes(index)} onChange={() => setReinforcementAnswer((current) => activity.payload.question?.selectionMode === 'multiple' ? current.includes(index) ? current.filter((item) => item !== index) : [...current, index].sort() : [index])} /><span>{option}</span></label>
                ))}</fieldset>
              )}
              <button className="review-workbench-next" disabled={activity.type === 'repair' ? reinforcementText.trim().length < 6 : activity.type === 'diagnose' ? false : reinforcementAnswer.length === 0} onClick={() => void submitReinforcementStep()}>{activity.type === 'diagnose' ? '查看针对性案例' : activity.type === 'verify' ? '提交独立验证' : '进入下一步'} <i aria-hidden="true">→</i></button>
              <p className="review-evidence-boundary"><i aria-hidden="true">◇</i>{reinforcement.evidenceBoundary}</p>
            </div>
          ) : reviewResult ? (
            <div className="review-workbench-result">
              <span>{reviewPassed ? '当前能力已重新唤醒' : '发现一个具体断点'}</span>
              <h2>{'score' in reviewResult ? `${reviewResult.score} / ${reviewResult.total}` : reviewPassed ? '可调用' : '待补强'}</h2>
              {!reviewPassed && 'reinforcement' in reviewResult && reviewResult.reinforcement.available ? <><p>不重复刚才那道题。接下来只补这项能力缺失的连接与案例，再换题独立验证。</p><button onClick={() => void startReinforcement()}>开始针对性补强 <i aria-hidden="true">→</i></button></>
                : reviewPassed && nextStage ? <><p>再激活已经完成，不会因此升段。你可以现在进入一项独立的下一阶任务，也可以留到课程路线中的正式机会。</p><button onClick={() => void openStrengthening()}>主动提升到 {capabilityStageCopy[nextStage]} <i aria-hidden="true">→</i></button><button className="review-secondary-action" onClick={continueReviewQueue}>暂时保持当前阶段</button></>
                  : <><p>{'feedback' in reviewResult ? reviewResult.feedback : '当前路线没有发布新的晋级任务，本次只更新保持状态。'}</p><button onClick={continueReviewQueue}>{pendingReviews.length ? '处理下一项' : '完成今日复习'} <i aria-hidden="true">→</i></button></>}
            </div>
          ) : reviewSession?.quiz ? (
            <div className="review-workbench-quiz">
              <header><span>快速唤醒</span><small>先凭记忆作答，不翻正文</small></header>
              <p className="review-workbench-objective">正在检查 · <b>{currentReview?.objective || reviewSession.quiz.questions[0]?.objective}</b></p>
              {reviewSession.quiz.questions.map((question, questionIndex) => (
                <fieldset key={`${reviewSession.quiz!.id}-${questionIndex}`}><legend>{question.prompt}</legend>{question.options.map((option, optionIndex) => (
                  <label key={optionIndex}><input type={question.selectionMode === 'multiple' ? 'checkbox' : 'radio'} name={`review-${reviewSession.quiz!.id}-${questionIndex}`} checked={reviewAnswers[questionIndex]?.includes(optionIndex) || false} onChange={() => chooseReviewAnswer(questionIndex, optionIndex, question.selectionMode)} /><span>{option}</span></label>
                ))}</fieldset>
              ))}
              <button className="review-workbench-next" disabled={reviewAnswers.some((answer) => answer.length === 0)} onClick={() => void submitDueReview()}>检查能否调用 <i aria-hidden="true">→</i></button>
            </div>
          ) : reviewSession?.capabilityTask ? (
            <div className="review-workbench-capability-task">
              <header><span>{reviewSession.capabilityTask.stage ? capabilityStageCopy[reviewSession.capabilityTask.stage] : '能力再激活'}</span><small>开放作答 · 不翻正文</small></header>
              <p className="review-workbench-objective">正在重新确认 · <b>{currentReview?.capability?.label || currentReview?.objective}</b></p>
              <h2>{reviewSession.capabilityTask.prompt}</h2>
              {typeof reviewSession.capabilityTask.taskContext.scenario === 'string' && (
                <div className="review-capability-context"><span>新的情境</span><p>{reviewSession.capabilityTask.taskContext.scenario}</p></div>
              )}
              {reviewSession.capabilityTask.deliverables.length > 0 && (
                <ul className="review-capability-deliverables">
                  {reviewSession.capabilityTask.deliverables.map((item) => <li key={item}>{item}</li>)}
                </ul>
              )}
              <label className="review-capability-response"><span>用自己的话完整回答</span><textarea rows={7} value={reviewResponseText} onChange={(event) => setReviewResponseText(event.target.value)} placeholder="说明你的判断、关键关系和适用边界…" /></label>
              <button className="review-workbench-next" disabled={reviewResponseText.trim().length < 12} onClick={() => void submitDueReview()}>提交再激活验证 <i aria-hidden="true">→</i></button>
              <p className="review-evidence-boundary"><i aria-hidden="true">◇</i>这次只重新确认当前阶段是否还能调用，不会直接制造晋级。</p>
            </div>
          ) : currentReview ? (
            <div className="review-workbench-ready"><span>下一项 · {currentReview.capability ? capabilityStageCopy[currentReview.capability.currentStage as keyof typeof capabilityStageCopy] || '能力再激活' : '预计 3 分钟'}</span><h2>{currentReview.capability?.label || currentReview.objective}</h2><p>{currentReview.reviewReason}。系统会按你已经取得的阶段安排对应任务；答对只恢复可调用状态，不会自动升段。</p><div><button onClick={() => void startDueReview()}>{currentReview.status === 'started' ? '继续能力再激活' : '开始能力再激活'} <i aria-hidden="true">→</i></button>{currentReview.status !== 'started' && <button onClick={() => void skipDueReview()}>今天跳过</button>}</div></div>
          ) : (
            <div className="review-workbench-status is-clear"><span>今日完成</span><h2>需要找回的知识都处理好了。</h2><p>这里不会为了维持连续感制造练习；等新的复习证据到期再回来。</p><button onClick={onBack}>返回书架</button></div>
          )}
        </main>
      </div>
    </section>
  );
}

function Home({
  data,
  dailyMode,
  onOpen,
  onContinue,
  onActivateBook,
  onOpenReview,
  onCreate,
}: {
  data: Bootstrap | null;
  dailyMode: DailyMode;
  onOpen: (shelf: Shelf) => void;
  onContinue: (seriesId: string, sectionId?: string | null, bookId?: string | null) => Promise<void>;
  onActivateBook: (book: Book) => Promise<void>;
  onOpenReview: () => void;
  onCreate: (body: ShelfCreateInput) => Promise<void>;
}) {
  const [showCreate, setShowCreate] = useState(false);
  const dashboard = data?.milestoneDashboard;
  const shelfCount = data?.shelves.length || 0;
  const seriesCount = data?.shelves.reduce((total, item) => total + item.series.length, 0) || 0;
  const bookCount = data?.shelves.reduce(
    (total, item) => total + item.series.reduce((count, itemSeries) => count + itemSeries.books.length, 0),
    0,
  ) || 0;
  const [studyToday, setStudyToday] = useState<StudyActivitySummary | null>(null);
  const [studyTodayLoading, setStudyTodayLoading] = useState(true);
  const {
    pendingReviews, currentReview, reinforcement, reviewBusy, reviewError, loadDueReviews,
  } = useReviewWorkspace();

  useEffect(() => {
    let current = true;
    const loadStudyToday = async () => {
      try {
        const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
        const value = await api.studyActivityToday(timezone);
        if (current) setStudyToday(value);
      } catch {
        // The dashboard stays usable if the estimate is temporarily unavailable.
      } finally {
        if (current) setStudyTodayLoading(false);
      }
    };
    void loadStudyToday();
    const timer = window.setInterval(loadStudyToday, 60_000);
    return () => {
      current = false;
      window.clearInterval(timer);
    };
  }, []);

  return (
    <section className={`library-dashboard mode-${dailyMode}`}>
      <header className="library-hero">
        <div className="library-hero-copy">
          <h1>把正在学的，<br /><em>放回眼前。</em></h1>
        </div>
        <div className="library-hero-aside">
          <StudyTimeSummary summary={studyToday} loading={studyTodayLoading} />
          <p className="library-summary">
            <strong>{shelfCount}</strong> 个领域 · <strong>{seriesCount}</strong> 个学习系列 · <strong>{bookCount}</strong> 本教材
          </p>
          <button className="library-create-button" onClick={() => setShowCreate(true)}>
            <span aria-hidden="true">＋</span> 创建新书架
          </button>
        </div>
      </header>

      <div className={`library-focus-grid ${dailyMode === 'fast' && currentReview ? 'fast-review-first' : ''}`}>
        <article className={`library-focus-card today-focus-card ${dashboard?.today ? '' : 'is-empty'}`}>
          <header>
            <span className="focus-card-label">今天从这里继续</span>
            {dashboard?.today && <small>{dailyMode === 'fast' ? '快速视图 · 约 3–5 分钟' : `约 ${dashboard.today.estimatedMinutes} 分钟`}</small>}
          </header>
          {dashboard?.today ? (
            <>
              <div className="today-book-path">
                <span>{dashboard.today.bookTitle}</span>
                <i aria-hidden="true">/</i>
                <span>{dashboard.today.chapterTitle}</span>
              </div>
              <h2>{dashboard.today.sectionTitle}</h2>
              <p className="today-question">{dashboard.today.question}</p>
              <div className="today-reason">
                <span aria-hidden="true">↳</span>
                <p><b>为什么学这一节</b>{dashboard.today.reason}</p>
              </div>
              <button className="today-continue-button" onClick={() => void onContinue(dashboard.today!.seriesId)}>
                继续学习 <span aria-hidden="true">→</span>
              </button>
            </>
          ) : (
            <div className="focus-empty-copy">
              <h2>先放入一本真正想学的书。</h2>
              <button onClick={() => setShowCreate(true)}>创建第一个书架 <span aria-hidden="true">→</span></button>
            </div>
          )}
        </article>

        <article className="library-focus-card review-entry-card">
          <header>
            <span className="focus-card-label">复习与补强</span>
            <small>跨书架</small>
          </header>
          {reviewBusy ? (
            <div className="review-entry-copy"><span>正在同步</span><h2>整理今天需要找回的知识</h2></div>
          ) : reviewError ? (
            <div className="review-entry-copy is-error"><span>读取失败</span><h2>今天的复习队列暂时没有取到</h2><button onClick={() => void loadDueReviews()}>重新读取</button></div>
          ) : reinforcement ? (
            <div className="review-entry-copy">
              <span>补强进行中 · {reinforcement.progress.activityCount}/{reinforcement.progress.maxActivities}</span>
              <h2>{reinforcement.objective}</h2>
              <p>断点已经定位，回到专用工作台继续当前一步。</p>
              <button onClick={onOpenReview}>继续补强 <i aria-hidden="true">→</i></button>
            </div>
          ) : currentReview ? (
            <div className="review-entry-copy">
              <span>{pendingReviews.length} 项需要唤醒</span>
              <h2>{currentReview.objective}</h2>
              <p>先快速找回关键概念；答不稳时，再进入针对性案例与独立校验。</p>
              <button onClick={onOpenReview}>进入复习中心 <i aria-hidden="true">→</i></button>
            </div>
          ) : (
            <div className="review-entry-copy is-clear"><span>今日已清空</span><h2>暂时没有需要唤醒的知识</h2><button onClick={onOpenReview}>查看复习中心 <i aria-hidden="true">→</i></button></div>
          )}
          <div className="review-entry-rail" aria-hidden="true"><i /><i /><i /></div>
        </article>

      </div>

      <section className="library-catalog" aria-labelledby="library-catalog-title">
        <header className="catalog-heading">
          <div>
            <p>按领域归档</p>
            <h2 id="library-catalog-title">我的书架</h2>
          </div>
        </header>

        <div className="library-shelf-stream">
        {data && data.shelves.length === 0 && (
          <div className="empty-library-message">
            <span>还没有书架</span>
            <small>从一个明确的领域开始，把教材、练习和学习进度放在一起。</small>
            <button className="primary-button" onClick={() => setShowCreate(true)}>创建第一个书架</button>
          </div>
        )}
        {data?.shelves.map((item, shelfIndex) => {
          const itemBookCount = item.series.reduce((total, itemSeries) => total + itemSeries.books.length, 0);
          return (
          <article className="home-shelf-section" key={item.id}>
            <header className="home-shelf-heading">
              <span className="home-shelf-index" aria-hidden="true">
                {String(shelfIndex + 1).padStart(2, '0')}
              </span>
              <div>
                <small>{shelfDescriptor(item) || '长期学习领域'}</small>
                <h3>{item.name}</h3>
                <p>{item.series.length} 个系列 · {itemBookCount} 本教材</p>
              </div>
              <button type="button" onClick={() => onOpen(item)}>
                管理书架 <span aria-hidden="true">→</span>
              </button>
            </header>

            {item.series.length > 0 ? (
              <div className="home-series-list">
                {item.series.map((itemSeries, seriesIndex) => (
                  <section className="home-series-row" key={itemSeries.id}>
                    <header>
                      <div>
                        <small>系列 {String(seriesIndex + 1).padStart(2, '0')}</small>
                        <h4>{itemSeries.title}</h4>
                      </div>
                    </header>
                    <div className="home-book-grid">
                      {itemSeries.books.map((book, bookIndex) => {
                        const canReviewOutline = bookReadyForOutlineReview(itemSeries, bookIndex);
                        const isTodayBook = Boolean(
                          itemSeries.id === dashboard?.today?.seriesId
                          && bookContainsSection(book, dashboard.today.sectionId),
                        );
                        const nextSection = isTodayBook
                          ? dashboard?.today?.sectionId || null
                          : nextBookSection(book)?.section.id
                            || book.chapters.flatMap((chapter) => chapter.sections)
                              .find((candidate) => candidate.status !== 'locked')?.id
                            || null;
                        const locked = book.status === 'locked';
                        return (
                          <button
                            type="button"
                            className={`home-book-card is-${book.status} ${isTodayBook ? 'is-today' : ''} ${canReviewOutline ? 'is-outline-ready' : ''}`}
                            key={book.id}
                            disabled={locked && !canReviewOutline}
                            onClick={() => {
                              if (canReviewOutline) void onActivateBook(book);
                              else void onContinue(itemSeries.id, nextSection, book.id);
                            }}
                          >
                            <span className="home-book-topline">
                              <small>第 {book.position} 本</small>
                              <em>{canReviewOutline ? '待确认' : bookProgressLabel(book, isTodayBook)}</em>
                            </span>
                            <strong>{book.title}</strong>
                            <span className="home-book-progress-copy">
                              <span>{bookProgressCopy(book, nextSection, isTodayBook || book.progress > 0)}</span>
                              <b>{book.progress}%</b>
                            </span>
                            <span
                              className="home-book-progress-track"
                              role="progressbar"
                              aria-label={`第 ${book.position} 本《${book.title}》进度`}
                              aria-valuemin={0}
                              aria-valuemax={100}
                              aria-valuenow={book.progress}
                            >
                              <i style={{ width: `${book.progress}%` }} />
                            </span>
                            <span className="home-book-action">
                              {canReviewOutline
                                ? '查看并确认目录'
                                : locked
                                  ? '完成前一本后解锁'
                                  : isTodayBook || book.progress > 0
                                    ? '继续学习'
                                    : '打开这本'}
                              {(!locked || canReviewOutline) && <i aria-hidden="true">→</i>}
                            </span>
                          </button>
                        );
                      })}
                    </div>
                  </section>
                ))}
              </div>
            ) : (
              <button type="button" className="home-shelf-empty" onClick={() => onOpen(item)}>
                <span>这里还没有学习系列</span>
                <small>进入书架，创建一个明确的学习目标。</small>
                <b>创建学习系列 <i aria-hidden="true">→</i></b>
              </button>
            )}
          </article>
          );
        })}
        </div>
      </section>
      {showCreate && (
        <ShelfCreateDialog
          onClose={() => setShowCreate(false)}
          onCreate={onCreate}
        />
      )}
    </section>
  );
}

function PrivacyConsentGate({
  userName,
  privacy,
  onAccept,
  onLogout,
}: {
  userName: string;
  privacy: PrivacyState;
  onAccept: () => Promise<void>;
  onLogout: () => Promise<void>;
}) {
  const [privacyAccepted, setPrivacyAccepted] = useState(false);
  const [trialAccepted, setTrialAccepted] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  const accept = async () => {
    if (!privacyAccepted || !trialAccepted) {
      setError('请分别确认隐私告知和自愿参加内测。');
      return;
    }
    setSubmitting(true);
    setError('');
    try {
      await onAccept();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '同意状态保存失败');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="privacy-gate-shell">
      <header className="privacy-gate-header">
        <span className="brand"><span className="brand-mark"><i /></span><b>slow</b></span>
        <button type="button" onClick={() => void onLogout()}>退出账号</button>
      </header>
      <main className="privacy-gate-main">
        <section className="privacy-gate-intro">
          <p className="eyebrow">INVITED LEARNING TRIAL</p>
          <small>{userName}，开始学习前</small>
          <h1>先知道你的学习记录<br />如何被使用。</h1>
          <p>{privacy.summary}</p>
          <div className="privacy-version-stamp">
            <span>告知版本</span><b>{privacy.noticeVersion}</b>
            <small>未来告知内容发生变化时，我们会重新询问。</small>
          </div>
        </section>

        <section className="privacy-notice-sheet" aria-labelledby="privacy-notice-title">
          <header>
            <p>内测试点说明</p>
            <h2 id="privacy-notice-title">{privacy.title}</h2>
          </header>
          <div className="privacy-notice-items">
            {privacy.items.map((item) => (
              <article key={item.title}>
                <h3>{item.title}</h3>
                <p>{item.body}</p>
              </article>
            ))}
          </div>
          <div className="privacy-consent-checks">
            <label className={privacyAccepted ? 'checked' : ''}>
              <input type="checkbox" checked={privacyAccepted} onChange={(event) => setPrivacyAccepted(event.target.checked)} />
              <span><b>我已阅读并同意隐私告知</b><small>允许 Slow 按上述范围处理我的学习数据。</small></span>
            </label>
            <label className={trialAccepted ? 'checked' : ''}>
              <input type="checkbox" checked={trialAccepted} onChange={(event) => setTrialAccepted(event.target.checked)} />
              <span><b>我自愿参加邀请制产品验证</b><small>我知道可以随时退出并提交数据删除申请。</small></span>
            </label>
          </div>
          {error && <p className="privacy-gate-error" role="alert">{error}</p>}
          <footer>
            <button className="primary-button" type="button" disabled={submitting} onClick={() => void accept()}>
              {submitting ? '正在保存同意记录…' : '同意并继续'}
            </button>
            <small>不同意不会创建学习内容；可以直接退出账号。</small>
          </footer>
        </section>
      </main>
    </div>
  );
}

function AccountExitReceiptPage({ receipt, onClose }: { receipt: AccountExitReceipt; onClose: () => void }) {
  const due = new Date(receipt.deletionDueAt).toLocaleDateString('zh-CN', { year: 'numeric', month: 'long', day: 'numeric' });
  return (
    <div className="exit-receipt-shell">
      <main className="exit-receipt-card">
        <span className="exit-receipt-mark" aria-hidden="true">✓</span>
        <p className="eyebrow">退出申请已登记</p>
        <h1>当前会话已经撤销。</h1>
        <p>Slow 已停止这个账号的新写入。主要数据将在 <b>{due}</b> 前删除或去标识化。</p>
        <dl>
          <div><dt>申请编号</dt><dd>{receipt.requestId}</dd></div>
          <div><dt>处理状态</dt><dd>删除处理中</dd></div>
          <div><dt>备份数据</dt><dd>预计 14 日内清除</dd></div>
        </dl>
        <p className="exit-receipt-note">请保存申请编号。如需撤回申请，请通过邀请消息的原渠道联系运营者。</p>
        <button className="primary-button" type="button" onClick={onClose}>返回登录页</button>
      </main>
    </div>
  );
}

const PROFILE_STAGE_OPTIONS: { value: LearningProfile['stage']; label: string }[] = [
  { value: 'exploring', label: '正在探索' },
  { value: 'beginner', label: '刚刚入门' },
  { value: 'foundation', label: '已有基础' },
  { value: 'practice', label: '实践提升' },
  { value: 'advanced', label: '系统进阶' },
];

const DEFAULT_LEARNING_PREFERENCES: LearningPreferences = {
  openingStyle: 'auto',
  explanationDensity: 'auto',
  formatPreferences: [],
  interactionRhythm: 'auto',
  dailyModePromptEnabled: false,
};

const PROFILE_PREFERENCE_OPTIONS = {
  openingStyle: [
    ['auto', '无特别偏好', ''],
    ['problem_first', '问题先行', '先抛出需要解决的问题'],
    ['example_first', '例子先行', '先从一个具体场景进入'],
    ['concept_first', '概念先行', '先建立准确的定义与框架'],
  ],
  explanationDensity: [
    ['auto', '无特别偏好', ''],
    ['concise', '更精炼', '减少铺垫，保留关键推理'],
    ['balanced', '适中', '解释与节奏保持平衡'],
    ['thorough', '更充分', '多展开机制、边界与反例'],
  ],
  interactionRhythm: [
    ['auto', '无特别偏好', ''],
    ['low_interruption', '连续阅读', '少打断，集中到段尾练习'],
    ['balanced', '适度停顿', '在关键转折处确认理解'],
    ['frequent_checkins', '频繁确认', '用更多短问题检查跟进'],
  ],
} as const;

const PROFILE_FORMAT_OPTIONS: { value: LearningPreferences['formatPreferences'][number]; label: string; note: string }[] = [
  { value: 'worked_example', label: '推演例题', note: '一步步展示判断过程' },
  { value: 'diagram', label: '关系图解', note: '适合结构、流程和关系' },
  { value: 'table', label: '对照表', note: '适合稳定维度的比较' },
  { value: 'code', label: '代码演示', note: '适合可执行的机制' },
  { value: 'analogy', label: '类比', note: '用熟悉对象搭桥' },
];

function parseProfileDomains(value: string) {
  return Array.from(new Set(
    value
      .split(/[,，、\n]/)
      .map((item) => item.trim())
      .filter(Boolean),
  )).slice(0, 6);
}

const CAPABILITY_STAGE_SHORT:Record<KnowledgeMapNode['stage'],string> = {
  unranked: '待验证', bronze: '青铜', silver: '白银', gold: '黄金', diamond: '钻石',
};

function KnowledgeMapPage({
  series,
  onBack,
  onOpenReview,
}: {
  series:{id:string;title:string;shelfName:string}[];
  onBack:()=>void;
  onOpenReview:()=>void;
}) {
  const [scope, setScope] = useState('');
  const [map, setMap] = useState<KnowledgeMap | null>(null);
  const [selectedId, setSelectedId] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError('');
    void api.knowledgeMap(scope || undefined).then((value) => {
      if (!alive) return;
      setMap(value);
      setSelectedId((current) => (
        value.nodes.some((item) => item.capabilityRevisionId === current)
          ? current
          : value.nodes[0]?.capabilityRevisionId || ''
      ));
    }).catch((reason) => {
      if (alive) setError(reason instanceof Error ? reason.message : '知识版图暂时无法加载');
    }).finally(() => {
      if (alive) setLoading(false);
    });
    return () => { alive = false; };
  }, [scope]);

  const selected = map?.nodes.find((item) => item.capabilityRevisionId === selectedId) || null;
  const positioned = useMemo(() => {
    const nodes = map?.nodes || [];
    return nodes.map((node, index) => {
      const column = index % 3;
      const row = Math.floor(index / 3);
      return {
        node,
        x: 130 + column * 220 + (row % 2 ? 28 : 0),
        y: 92 + row * 150 + (column === 1 ? 34 : 0),
      };
    });
  }, [map]);
  const coordinates = new Map(positioned.map((item) => [item.node.capabilityRevisionId, item]));
  const coverage = map ? Math.round(map.progress.coveragePpm / 10_000) : 0;

  const startSelectedReinforcement = async () => {
    if (selected?.nextAction.kind === 'wake') {
      onOpenReview();
    }
  };

  return (
    <section className="knowledge-map-page" aria-labelledby="knowledge-map-title">
      <header className="knowledge-map-hero">
        <button type="button" className="knowledge-map-back" onClick={onBack}>← 返回书架</button>
        <div className="knowledge-map-title-row">
          <div>
            <p className="eyebrow">MY KNOWLEDGE FIELD · 由正式证据生长</p>
            <h1 id="knowledge-map-title">不是读到了哪里，<br />是能力真正长到了哪里。</h1>
            <p>{map?.message || '正在重建你的个人知识子网…'}</p>
          </div>
          <label className="knowledge-scope-picker">
            <span>观察范围</span>
            <select value={scope} onChange={(event) => setScope(event.target.value)}>
              <option value="">全部学习目标</option>
              {series.map((item) => (
                <option key={item.id} value={item.id}>{item.shelfName} · {item.title}</option>
              ))}
            </select>
          </label>
        </div>
        <div className="knowledge-progress-strip" aria-label={`能力路线覆盖 ${coverage}%`}>
          <div className="knowledge-progress-number"><strong>{coverage}</strong><span>%</span></div>
          <div className="knowledge-progress-copy">
            <span>能力路线覆盖</span>
            <div><i style={{ width: `${coverage}%` }} /></div>
            <small>{map?.progress.stagedCapabilities || 0} / {map?.progress.requiredCapabilities || 0} 项稳定能力已取得正式阶段</small>
          </div>
          <dl>
            <div><dt>{map?.progress.activeCapabilities || 0}</dt><dd>可随时调用</dd></div>
            <div><dt>{map?.progress.needsWakeCapabilities || 0}</dt><dd>待唤醒</dd></div>
            <div><dt>{map?.progress.learningCapabilities || 0}</dt><dd>形成中</dd></div>
          </dl>
        </div>
      </header>

      {loading ? (
        <div className="knowledge-map-status"><i />正在从学习证据重建版图…</div>
      ) : error ? (
        <div className="knowledge-map-status is-error" role="alert">{error}</div>
      ) : !map?.nodes.length ? (
        <div className="knowledge-map-empty">
          <span aria-hidden="true">◎</span>
          <h2>第一颗知识坐标还在形成</h2>
          <p>{map?.message}</p>
          {Boolean(map?.excluded.targetWithoutCapabilityCount) && <small>仍有旧目标没有稳定能力身份，因此不会被拿来虚构能力阶段。</small>}
        </div>
      ) : (
        <div className="knowledge-map-workspace">
          <div className="knowledge-constellation" aria-label="知识节点关系图">
            <div className="knowledge-constellation-heading">
              <div><span>个人子网</span><b>{map.nodes.length} 个能力节点</b></div>
              <small>连线表示能力间共享正式知识身份；点击查看各自知识子网</small>
            </div>
            <svg viewBox={`0 0 700 ${Math.max(430, Math.ceil(positioned.length / 3) * 150 + 80)}`} role="img" aria-label="能力节点关系">
              <defs>
                <pattern id="knowledge-grid" width="28" height="28" patternUnits="userSpaceOnUse">
                  <circle cx="1" cy="1" r="1" fill="currentColor" />
                </pattern>
              </defs>
              <rect width="100%" height="100%" fill="url(#knowledge-grid)" className="knowledge-grid" />
              {map.edges.map((edge) => {
                const from = coordinates.get(edge.from);
                const to = coordinates.get(edge.to);
                if (!from || !to) return null;
                return <g key={edge.id} className="knowledge-edge"><line x1={from.x} y1={from.y} x2={to.x} y2={to.y} /><text x={(from.x + to.x) / 2} y={(from.y + to.y) / 2 - 8}>{edge.label}</text></g>;
              })}
              {positioned.map(({ node, x, y }) => (
                <g
                  key={node.capabilityRevisionId}
                  className={`knowledge-node rank-${node.stage} activation-${node.activationState === 'available' ? 'active' : node.activationState === 'due_for_reactivation' ? 'due' : 'learning'}${selectedId === node.capabilityRevisionId ? ' is-selected' : ''}`}
                  role="button"
                  tabIndex={0}
                  aria-label={`${node.label}，${node.stageLabel}，${node.nextAction.label}`}
                  onClick={() => setSelectedId(node.capabilityRevisionId)}
                  onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') setSelectedId(node.capabilityRevisionId); }}
                >
                  <circle cx={x} cy={y} r="42" className="knowledge-node-halo" />
                  <circle cx={x} cy={y} r="30" className="knowledge-node-core" />
                  <text x={x} y={y + 4} className="knowledge-node-rank">{CAPABILITY_STAGE_SHORT[node.stage]}</text>
                  <text x={x} y={y + 61} className="knowledge-node-label">{node.label.length > 11 ? `${node.label.slice(0, 10)}…` : node.label}</text>
                  {node.activationState === 'due_for_reactivation' && <text x={x + 31} y={y - 28} className="knowledge-node-signal">唤</text>}
                </g>
              ))}
            </svg>
          </div>

          <aside className="knowledge-node-ledger" aria-live="polite">
            {selected && (
              <>
                <div className={`knowledge-ledger-seal rank-${selected.stage}`}>
                  <span>{CAPABILITY_STAGE_SHORT[selected.stage]}</span>
                  <small>{selected.independentEvidenceCount} 次独立证据</small>
                </div>
                <p className="eyebrow">EVIDENCE LEDGER</p>
                <h2>{selected.label}</h2>
                <p className="knowledge-capability-scope">这是一项稳定能力；下方知识节点共同支撑它，不会各自生成段位。</p>
                <div className="knowledge-ledger-state">
                  <span>{selected.stageLabel}</span>
                  <i>→</i>
                  <span className={`activation-${selected.activationState === 'available' ? 'active' : selected.activationState === 'due_for_reactivation' ? 'due' : 'learning'}`}>{selected.nextAction.label}</span>
                </div>
                <dl>
                  <div><dt>{selected.independentEvidenceCount}</dt><dd>独立证据</dd></div>
                  <div><dt>{selected.knowledge.filter((item) => item.required).length}</dt><dd>必需知识</dd></div>
                  <div><dt>{selected.stabilityDays} 天</dt><dd>当前稳定期</dd></div>
                </dl>
                <div className="knowledge-ceiling-note">
                  <span>这项能力的自然上限</span>
                  <b>{selected.routeStageCeilingLabel}</b>
                  <small>{selected.nextCriterion || '当前路线已经没有更高阶的正式验证任务。'}</small>
                </div>
                <div className="knowledge-route-origin">
                  <span>能力知识子网</span>
                  <b>{selected.knowledge.filter((item) => item.required).map((item) => item.label).join(' · ') || '待冻结必需知识'}</b>
                  <small>{selected.relations.map((item) => item.statement).join('；') || '单节点最小子网'}</small>
                </div>
                {selected.routeContexts[0] && (
                  <div className="knowledge-route-origin">
                    <span>进入你版图的路线</span>
                    <b>{selected.routeContexts[0].seriesTitle}</b>
                    <small>{selected.routeContexts[0].bookTitle} · {selected.routeContexts[0].sectionTitle}</small>
                  </div>
                )}
                {selected.nextAction.kind === 'wake' && (
                  <button className="knowledge-reinforce-entry" onClick={() => void startSelectedReinforcement()}>
                    {selected.nextAction.label}
                    <span aria-hidden="true">→</span>
                  </button>
                )}
              </>
            )}
          </aside>
        </div>
      )}
      <footer className="knowledge-map-footnote">
        <b>这里不显示 AI 猜测。</b>
        <span>节末测验、口试、正式应用与迁移任务共同形成能力阶段；延迟复习只更新当前可调用状态，不会凭时间升降历史阶段。</span>
      </footer>
    </section>
  );
}

function ProfileCenterPage({
  user,
  mode,
  profile,
  stats,
  section,
  onSectionChange,
  onBack,
  onSave,
  onLogout,
  onRotateRecoveryCode,
  privacy,
  onRequestExit,
}: {
  user: AuthState['user'];
  mode: AuthState['mode'];
  profile: LearningProfile;
  stats: { shelves: number; series: number };
  section: 'profile' | 'account';
  onSectionChange: (section: 'profile' | 'account') => void;
  onBack: () => void;
  onSave: (body: object) => Promise<void>;
  onLogout: () => Promise<void>;
  onRotateRecoveryCode: (currentPassword: string) => Promise<string>;
  privacy: PrivacyState;
  onRequestExit: (confirmation: string, reason: string) => Promise<void>;
}) {
  const [profession, setProfession] = useState(profile.profession);
  const [stage, setStage] = useState<LearningProfile['stage']>(profile.stage);
  const [purpose, setPurpose] = useState(profile.purpose);
  const [domainText, setDomainText] = useState(profile.domains.join('，'));
  const [experience, setExperience] = useState(profile.experience);
  const [weeklyMinutes, setWeeklyMinutes] = useState(profile.weeklyMinutes || 0);
  const [targetDate, setTargetDate] = useState(profile.targetDate || '');
  const initialPreferences = profile.preferences || DEFAULT_LEARNING_PREFERENCES;
  const [openingStyle, setOpeningStyle] = useState(initialPreferences.openingStyle);
  const [explanationDensity, setExplanationDensity] = useState(initialPreferences.explanationDensity);
  const [formatPreferences, setFormatPreferences] = useState(initialPreferences.formatPreferences);
  const [interactionRhythm, setInteractionRhythm] = useState(initialPreferences.interactionRhythm);
  const [dailyModePromptEnabled, setDailyModePromptEnabled] = useState(
    initialPreferences.dailyModePromptEnabled ?? false,
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [showExitRequest, setShowExitRequest] = useState(false);
  const [exitConfirmation, setExitConfirmation] = useState('');
  const [exitReason, setExitReason] = useState('');
  const [exitSubmitting, setExitSubmitting] = useState(false);
  const [recoveryCodeBusy, setRecoveryCodeBusy] = useState(false);
  const [renewedRecoveryCode, setRenewedRecoveryCode] = useState('');
  const [recoveryPassword, setRecoveryPassword] = useState('');
  const [recoveryError, setRecoveryError] = useState('');

  const domains = useMemo(() => parseProfileDomains(domainText), [domainText]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!profession.trim() || !stage || !purpose.trim() || domains.length === 0) {
      setError('请完整填写当前身份、学习阶段、目标领域和学习目的。');
      return;
    }
    setSaving(true);
    setError('');
    setMessage('');
    try {
      await onSave({
        profession: profession.trim(),
        stage,
        purpose: purpose.trim(),
        domains,
        experience: experience.trim(),
        weeklyMinutes,
        targetDate,
        preferences: {
          openingStyle,
          explanationDensity,
          formatPreferences,
          interactionRhythm,
          dailyModePromptEnabled,
        },
      });
      setMessage('已保存');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '学习画像保存失败');
    } finally {
      setSaving(false);
    }
  };

  const modeLabel = {
    demo: '固定体验账号',
    local: '本地独立账号',
    password: '受邀学习账号',
    oidc: '统一身份账号',
  }[mode];

  return (
    <section className="profile-center-page" aria-labelledby="profile-center-title">
      <aside className="profile-center-sidebar">
        <p className="profile-center-kicker">PERSONAL CENTER</p>
        <div className="profile-sidebar-identity">
          <span className="profile-avatar" aria-hidden="true">{user.name.trim().slice(0, 1).toUpperCase() || '我'}</span>
          <div><h2>{user.name}</h2><small>{modeLabel}</small></div>
        </div>

        <nav aria-label="个人中心导航">
          <button className={section === 'profile' ? 'active' : ''} aria-current={section === 'profile' ? 'page' : undefined} onClick={() => onSectionChange('profile')}>
            <span>学习画像</span><small>目标、背景与节奏</small>
          </button>
          <button className={section === 'account' ? 'active' : ''} aria-current={section === 'account' ? 'page' : undefined} onClick={() => onSectionChange('account')}>
            <span>账号与数据</span><small>身份、归属与退出</small>
          </button>
        </nav>

        <div className="profile-sidebar-ledger">
          <span>当前学习设置</span><b>已保存</b>
        </div>
        <button type="button" className="profile-back-button" onClick={onBack}><span aria-hidden="true">←</span> 返回书架</button>
      </aside>

      {section === 'profile' ? (
        <form className="profile-center-form" onSubmit={(event) => void submit(event)}>
          <header className="profile-page-heading">
            <p className="eyebrow">学习画像</p>
            <h1 id="profile-center-title">让教材始终认识<br />现在的你。</h1>
          </header>

          <fieldset className="profile-field-group">
            <legend>当前起点</legend>
            <div className="profile-two-columns">
              <label>当前身份或职业
                <input required maxLength={120} value={profession} onChange={(event) => setProfession(event.target.value)} />
              </label>
              <label>学习阶段
                <select required value={stage} onChange={(event) => setStage(event.target.value as LearningProfile['stage'])}>
                  {PROFILE_STAGE_OPTIONS.map((item) => <option value={item.value} key={item.value}>{item.label}</option>)}
                </select>
              </label>
            </div>
          </fieldset>

          <fieldset className="profile-field-group">
            <legend>学习方向</legend>
            <label>目标领域
              <input required maxLength={240} value={domainText} onChange={(event) => setDomainText(event.target.value)} placeholder="用逗号分隔，最多 6 个" />
            </label>
            <label>我最终想获得的能力
              <textarea required maxLength={1000} value={purpose} onChange={(event) => setPurpose(event.target.value)} />
            </label>
            <label>相关经验 <em>可选</em>
              <textarea className="profile-experience-field" maxLength={1000} value={experience} onChange={(event) => setExperience(event.target.value)} />
            </label>
          </fieldset>

          <fieldset className="profile-field-group">
            <legend>教材表达偏好</legend>

            <div className="profile-preference-section">
              <span className="profile-preference-label">怎样进入一个新问题</span>
              <div className="profile-choice-grid">
                {PROFILE_PREFERENCE_OPTIONS.openingStyle.map(([value, label, note]) => (
                  <label className={openingStyle === value ? 'selected' : ''} key={value}>
                    <input type="radio" name="opening-style" value={value} checked={openingStyle === value} onChange={() => setOpeningStyle(value)} />
                    <span><b>{label}</b>{note && <small>{note}</small>}</span>
                  </label>
                ))}
              </div>
            </div>

            <div className="profile-preference-section">
              <span className="profile-preference-label">解释展开程度</span>
              <div className="profile-choice-grid">
                {PROFILE_PREFERENCE_OPTIONS.explanationDensity.map(([value, label, note]) => (
                  <label className={explanationDensity === value ? 'selected' : ''} key={value}>
                    <input type="radio" name="explanation-density" value={value} checked={explanationDensity === value} onChange={() => setExplanationDensity(value)} />
                    <span><b>{label}</b>{note && <small>{note}</small>}</span>
                  </label>
                ))}
              </div>
            </div>

            <div className="profile-preference-section">
              <span className="profile-preference-label">优先考虑的表现形式 <em>可多选</em></span>
              <div className="profile-format-grid">
                {PROFILE_FORMAT_OPTIONS.map((item) => {
                  const selected = formatPreferences.includes(item.value);
                  return (
                    <label className={selected ? 'selected' : ''} key={item.value}>
                      <input
                        type="checkbox"
                        checked={selected}
                        onChange={() => setFormatPreferences((current) => selected
                          ? current.filter((value) => value !== item.value)
                          : [...current, item.value])}
                      />
                      <span><b>{item.label}</b><small>{item.note}</small></span>
                    </label>
                  );
                })}
              </div>
            </div>

            <div className="profile-preference-section">
              <span className="profile-preference-label">阅读中的确认节奏</span>
              <div className="profile-choice-grid">
                {PROFILE_PREFERENCE_OPTIONS.interactionRhythm.map(([value, label, note]) => (
                  <label className={interactionRhythm === value ? 'selected' : ''} key={value}>
                    <input type="radio" name="interaction-rhythm" value={value} checked={interactionRhythm === value} onChange={() => setInteractionRhythm(value)} />
                    <span><b>{label}</b>{note && <small>{note}</small>}</span>
                  </label>
                ))}
              </div>
            </div>
          </fieldset>

          <fieldset className="profile-field-group">
            <legend>学习节奏</legend>
            <label className="profile-toggle-row">
              <span>
                <b>进入学习前自动询问 Fast / Slow 模式</b>
                <small>关闭时沿用上次选择，也可以随时从顶部切换。</small>
              </span>
              <input
                type="checkbox"
                role="switch"
                checked={dailyModePromptEnabled}
                onChange={(event) => setDailyModePromptEnabled(event.target.checked)}
              />
            </label>
            <div className="profile-two-columns">
              <label>每周投入
                <select value={weeklyMinutes} onChange={(event) => setWeeklyMinutes(Number(event.target.value))}>
                  <option value={0}>暂不设定</option>
                  <option value={120}>2 小时</option>
                  <option value={180}>3 小时</option>
                  <option value={300}>5 小时</option>
                  <option value={480}>8 小时</option>
                  <option value={600}>10 小时</option>
                </select>
              </label>
              <label>目标日期 <em>可选</em>
                <input type="date" value={targetDate} onChange={(event) => setTargetDate(event.target.value)} />
              </label>
            </div>
          </fieldset>

          {error && <p className="profile-flow-error" role="alert">{error}</p>}
          {message && <p className="profile-save-message" role="status">{message}</p>}
          <footer>
            <button className="primary-button" disabled={saving}>{saving ? '正在保存…' : '保存学习画像'}</button>
          </footer>
        </form>
      ) : (
        <section className="profile-account-page" aria-labelledby="account-page-title">
          <header className="profile-page-heading">
            <p className="eyebrow">账号与数据</p>
            <h1 id="account-page-title">一个账号，<br />一套学习记录。</h1>
          </header>

          <div className="account-summary-card">
            <span className="profile-avatar" aria-hidden="true">{user.name.trim().slice(0, 1).toUpperCase() || '我'}</span>
            <div><small>当前登录账号</small><h2>{user.name}</h2><p>{modeLabel}</p></div>
            <dl>
              <div><dt>书架</dt><dd>{stats.shelves}</dd></div>
              <div><dt>学习系列</dt><dd>{stats.series}</dd></div>
              <div><dt>学习设置</dt><dd>已保存</dd></div>
            </dl>
          </div>

          <div className="account-policy-grid">
            <article>
              <span>数据归属</span>
              <h3>学习记录属于当前账号</h3>
            </article>
            <article>
              <span>登录安全</span>
              <h3>浏览器只保存安全会话</h3>
              <p>{mode === 'local' || mode === 'password'
                ? '密码不会写入浏览器存储，退出后当前登录立即失效。'
                : '身份由登录服务确认，Slow 不接收身份提供商密码。'}</p>
            </article>
            <article>
              <span>内测同意</span>
              <h3>当前隐私告知已确认</h3>
              <p>{privacy.noticeVersion} · {privacy.acceptedAt
                ? new Date(privacy.acceptedAt).toLocaleDateString('zh-CN')
                : '当前环境无需同意'}</p>
            </article>
          </div>

          {mode === 'password' && (
            <section className="account-recovery-panel">
              <div>
                <span>账号恢复</span>
                <h3>恢复码</h3>
                <p>生成后，旧恢复码立即失效。</p>
                {renewedRecoveryCode && <code>{renewedRecoveryCode}</code>}
                {recoveryError && <p className="account-recovery-error" role="alert">{recoveryError}</p>}
              </div>
              <div className="account-recovery-action">
                <label>当前密码
                  <input
                    type="password"
                    autoComplete="current-password"
                    value={recoveryPassword}
                    onChange={(event) => setRecoveryPassword(event.target.value)}
                  />
                </label>
                <button type="button" disabled={recoveryCodeBusy || !recoveryPassword} onClick={async () => {
                  setRecoveryCodeBusy(true);
                  setRecoveryError('');
                  try {
                    setRenewedRecoveryCode(await onRotateRecoveryCode(recoveryPassword));
                    setRecoveryPassword('');
                  } catch (reason) {
                    setRecoveryError(reason instanceof Error ? reason.message : '恢复码生成失败');
                  } finally {
                    setRecoveryCodeBusy(false);
                  }
                }}>{recoveryCodeBusy ? '正在生成…' : renewedRecoveryCode ? '重新生成' : '生成新的恢复码'}</button>
              </div>
            </section>
          )}

          <section className="account-exit-panel">
            <div><h3>退出当前账号</h3></div>
            <button type="button" disabled={saving} onClick={() => void onLogout()}>退出账号</button>
          </section>

          <section className="account-deletion-panel">
            <div className="account-deletion-copy">
              <span>不可撤销操作</span>
              <h3>退出试点并申请删除数据</h3>
              <p>提交后会立即退出所有设备并停用账号。主要数据将在 7 日内删除或去标识化，备份数据预计在 14 日内清除。</p>
            </div>
            {!showExitRequest ? (
              <button type="button" className="account-deletion-open" onClick={() => setShowExitRequest(true)}>开始退出申请</button>
            ) : (
              <form className="account-deletion-form" onSubmit={async (event) => {
                event.preventDefault();
                if (exitConfirmation !== '退出并删除') {
                  setError('请输入“退出并删除”确认申请。');
                  return;
                }
                setExitSubmitting(true);
                setError('');
                try {
                  await onRequestExit(exitConfirmation, exitReason);
                } catch (reason) {
                  setError(reason instanceof Error ? reason.message : '退出申请提交失败');
                  setExitSubmitting(false);
                }
              }}>
                <label>退出原因 <em>可选</em>
                  <textarea maxLength={500} value={exitReason} onChange={(event) => setExitReason(event.target.value)} placeholder="帮助我们改进；不要填写密码或其他敏感信息" />
                </label>
                <label>输入“退出并删除”确认
                  <input required autoComplete="off" value={exitConfirmation} onChange={(event) => setExitConfirmation(event.target.value)} />
                </label>
                <div>
                  <button type="button" disabled={exitSubmitting} onClick={() => { setShowExitRequest(false); setExitConfirmation(''); setExitReason(''); }}>取消</button>
                  <button type="submit" disabled={exitSubmitting || exitConfirmation !== '退出并删除'}>{exitSubmitting ? '正在撤销账号…' : '提交退出与删除申请'}</button>
                </div>
              </form>
            )}
          </section>
        </section>
      )}
    </section>
  );
}

function ShelfCreateDialog({
  onClose,
  onCreate,
}: {
  onClose: () => void;
  onCreate: (body: ShelfCreateInput) => Promise<void>;
}) {
  const [name, setName] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState('');

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !submitting) onClose();
    };
    document.addEventListener('keydown', closeOnEscape);
    return () => document.removeEventListener('keydown', closeOnEscape);
  }, [onClose, submitting]);

  const send = async (event: FormEvent) => {
    event.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setFormError('');
    try {
      await onCreate({ name: name.trim() });
    } catch (reason) {
      setFormError(reason instanceof Error ? reason.message : '书架创建失败，请稍后重试');
      setSubmitting(false);
    }
  };

  return (
    <div
      className="confirm-backdrop shelf-create-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !submitting) onClose();
      }}
    >
      <section
        className="shelf-create-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="shelf-create-title"
      >
        <div className="shelf-create-heading">
          <div>
            <p className="eyebrow">新建学习空间</p>
            <h2 id="shelf-create-title">创建书架</h2>
          </div>
          <button className="dialog-close" aria-label="关闭创建书架" disabled={submitting} onClick={onClose}>×</button>
        </div>

        <form className="shelf-create-form" onSubmit={send}>
          <label>
            书架名称
            <input
              autoFocus
              required
              maxLength={100}
              disabled={submitting}
              value={name}
              placeholder="例如：技术、产品设计、经济学"
              onChange={(event) => {
                setName(event.target.value);
                setFormError('');
              }}
            />
          </label>
          {formError && <p className="shelf-create-error" role="alert">{formError}</p>}
          <div className="dialog-actions">
            <button type="button" className="quiet-button" disabled={submitting} onClick={onClose}>取消</button>
            <button className="primary-button" disabled={submitting}>
              {submitting ? '正在创建…' : '创建并进入书架'}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}

function ShelfRenameDialog({
  shelf,
  onClose,
  onRename,
}: {
  shelf: Shelf;
  onClose: () => void;
  onRename: (body: ShelfRenameInput) => Promise<void>;
}) {
  const [name, setName] = useState(shelf.name);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState('');
  const normalizedName = name.trim().replace(/\s+/g, ' ');
  const dialogRef = useModalFocus<HTMLElement>({
    open: true,
    canClose: !submitting,
    onRequestClose: onClose,
  });

  const send = async (event: FormEvent) => {
    event.preventDefault();
    if (!normalizedName || normalizedName === shelf.name) return;
    setSubmitting(true);
    setFormError('');
    try {
      await onRename({ name: normalizedName });
      onClose();
    } catch (reason) {
      setFormError(reason instanceof Error ? reason.message : '书架重命名失败，请稍后重试');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      className="confirm-backdrop shelf-create-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !submitting) onClose();
      }}
    >
      <section
        ref={dialogRef}
        className="shelf-create-dialog shelf-rename-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="shelf-rename-title"
        tabIndex={-1}
      >
        <div className="shelf-create-heading">
          <div>
            <p className="eyebrow">整理书架</p>
            <h2 id="shelf-rename-title">重命名书架</h2>
            <p>只会改变书架名称，已有系列、教材和学习记录保持不变。</p>
          </div>
          <button className="dialog-close" aria-label="关闭重命名书架" disabled={submitting} onClick={onClose}>×</button>
        </div>
        <form className="shelf-create-form" onSubmit={send}>
          <label>
            新名称
            <input
              data-dialog-initial-focus
              required
              maxLength={100}
              disabled={submitting}
              value={name}
              onFocus={(event) => event.currentTarget.select()}
              onChange={(event) => {
                setName(event.target.value);
                setFormError('');
              }}
            />
          </label>
          {formError && <p className="shelf-create-error" role="alert">{formError}</p>}
          <div className="dialog-actions">
            <button type="button" className="quiet-button" disabled={submitting} onClick={onClose}>取消</button>
            <button
              className="primary-button"
              disabled={submitting || !normalizedName || normalizedName === shelf.name}
            >
              {submitting ? '正在保存…' : '保存名称'}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}

function SeriesRenameDialog({
  series,
  onClose,
  onRename,
}: {
  series: Series;
  onClose: () => void;
  onRename: (body: SeriesRenameInput) => Promise<void>;
}) {
  const [name, setName] = useState(series.title);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState('');
  const normalizedName = name.trim().replace(/\s+/g, ' ');
  const dialogRef = useModalFocus<HTMLElement>({
    open: true,
    canClose: !submitting,
    onRequestClose: onClose,
  });

  const send = async (event: FormEvent) => {
    event.preventDefault();
    if (!normalizedName || normalizedName === series.title) return;
    setSubmitting(true);
    setFormError('');
    try {
      await onRename({ name: normalizedName });
    } catch (reason) {
      setFormError(reason instanceof Error ? reason.message : '系列重命名失败，请稍后重试');
      setSubmitting(false);
    }
  };

  return (
    <div
      className="confirm-backdrop shelf-create-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !submitting) onClose();
      }}
    >
      <section
        ref={dialogRef}
        className="shelf-create-dialog series-rename-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="series-rename-title"
        tabIndex={-1}
      >
        <div className="shelf-create-heading">
          <div>
            <p className="eyebrow">整理学习目标</p>
            <h2 id="series-rename-title">重命名系列</h2>
            <p>只会改变书架上的显示名称，学习目标、教材内容和学习记录保持不变。</p>
          </div>
          <button className="dialog-close" aria-label="关闭重命名系列" disabled={submitting} onClick={onClose}>×</button>
        </div>
        <form className="shelf-create-form" onSubmit={send}>
          <label>
            新名称
            <input
              data-dialog-initial-focus
              required
              maxLength={240}
              disabled={submitting}
              value={name}
              onFocus={(event) => event.currentTarget.select()}
              onChange={(event) => {
                setName(event.target.value);
                setFormError('');
              }}
            />
          </label>
          {formError && <p className="shelf-create-error" role="alert">{formError}</p>}
          <div className="dialog-actions">
            <button type="button" className="quiet-button" disabled={submitting} onClick={onClose}>取消</button>
            <button
              className="primary-button"
              disabled={submitting || !normalizedName || normalizedName === series.title}
            >
              {submitting ? '正在保存…' : '保存名称'}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}

function ShelfDeleteDialog({
  shelf,
  onClose,
  onDelete,
}: {
  shelf: Shelf;
  onClose: () => void;
  onDelete: () => Promise<void>;
}) {
  const [deleting, setDeleting] = useState(false);
  const dialogRef = useModalFocus<HTMLElement>({
    open: true,
    canClose: !deleting,
    onRequestClose: onClose,
  });
  const seriesCount = shelf.series.length;

  return (
    <div
      className="confirm-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !deleting) onClose();
      }}
    >
      <section
        ref={dialogRef}
        className="delete-confirm shelf-delete-confirm"
        role="dialog"
        aria-modal="true"
        aria-labelledby="delete-shelf-title"
        aria-describedby="delete-shelf-description"
        tabIndex={-1}
      >
        <span className="delete-confirm-icon"><TrashIcon size={20} /></span>
        <p className="eyebrow">删除书架</p>
        <h2 id="delete-shelf-title">删除“{shelf.name}”？</h2>
        <p id="delete-shelf-description">
          {seriesCount > 0
            ? `以下 ${seriesCount} 个学习系列及其中教材将从书架和学习入口移除：`
            : '这个书架目前没有学习系列，删除后将从你的书架列表移除。'}
        </p>
        {seriesCount > 0 && (
          <ul className="shelf-delete-series-list" aria-label="将删除的学习系列">
            {shelf.series.map((item) => (
              <li key={item.id}>
                <span>{item.title}</span>
                <small>{item.books.length} 本教材</small>
              </li>
            ))}
          </ul>
        )}
        <p className="shelf-delete-history-note">已有学习记录会保留，但当前界面暂不支持恢复。</p>
        <div>
          <button data-dialog-initial-focus className="quiet-button" disabled={deleting} onClick={onClose}>取消</button>
          <button
            className="danger-button"
            disabled={deleting}
            onClick={async () => {
              setDeleting(true);
              try {
                await onDelete();
              } catch {
                setDeleting(false);
              }
            }}
          >
            {deleting
              ? '正在删除…'
              : seriesCount > 0
                ? `删除书架和 ${seriesCount} 个系列`
                : '确认删除书架'}
          </button>
        </div>
      </section>
    </div>
  );
}

function ShelfPage({
  shelf,
  onBack,
  onStartCreate,
  onRename,
  onDeleteShelf,
  onOpen,
  onActivateBook,
  onRenameSeries,
  onDelete,
}: {
  shelf: Shelf;
  onBack: () => void;
  onStartCreate: () => void;
  onRename: (body: ShelfRenameInput) => Promise<void>;
  onDeleteShelf: () => Promise<void>;
  onOpen: (id: string, sectionId?: string | null, bookId?: string | null) => void;
  onActivateBook: (book: Book) => Promise<void>;
  onRenameSeries: (id: string, body: SeriesRenameInput) => Promise<void>;
  onDelete: (id: string) => Promise<void>;
}) {
  const [showRename, setShowRename] = useState(false);
  const [showDeleteShelf, setShowDeleteShelf] = useState(false);
  const [renameTarget, setRenameTarget] = useState<Series | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Series | null>(null);
  const [deleting, setDeleting] = useState(false);
  const deleteDialogRef = useModalFocus<HTMLElement>({
    open: Boolean(deleteTarget),
    canClose: !deleting,
    onRequestClose: () => setDeleteTarget(null),
  });

  return (
    <section className="landing-section">
      <button type="button" className="shelf-back-button" onClick={onBack}>
        <span aria-hidden="true">←</span> 全部书架
      </button>
      {shelfDescriptor(shelf) && <p className="eyebrow">{shelfDescriptor(shelf)}</p>}
      <div className="title-row">
        <div className="shelf-title-copy">
          <h1>{shelf.name}</h1>
          <div className="shelf-title-actions">
            <button
              type="button"
              className="shelf-rename-button"
              aria-label={`重命名书架：${shelf.name}`}
              onClick={() => setShowRename(true)}
            >
              <PencilIcon /> 重命名
            </button>
            <span aria-hidden="true" />
            <button
              type="button"
              className="shelf-delete-button"
              aria-label={`删除书架：${shelf.name}`}
              onClick={() => setShowDeleteShelf(true)}
            >
              <TrashIcon size={13} /> 删除书架
            </button>
          </div>
        </div>
        <button
          className="primary-button"
          onClick={onStartCreate}
        >
          ＋ 创建学习系列
        </button>
      </div>
      {showRename && (
        <ShelfRenameDialog
          shelf={shelf}
          onClose={() => setShowRename(false)}
          onRename={onRename}
        />
      )}
      {showDeleteShelf && (
        <ShelfDeleteDialog
          shelf={shelf}
          onClose={() => setShowDeleteShelf(false)}
          onDelete={onDeleteShelf}
        />
      )}
      <div className="series-shelf-heading">
        <span>书架上的学习系列</span>
        <small>每一排对应一个学习目标</small>
      </div>
      <div className="focused-series-shelves">
        {shelf.series.map((item, seriesIndex) => (
          <article className="focused-series-shelf" key={item.id}>
            <header>
              <span className="focused-series-number">
                <small>系列</small>
                <b>{String(seriesIndex + 1).padStart(2, '0')}</b>
              </span>
              <div className="focused-series-title">
                <h2>{item.title}</h2>
                <span>
                  <i><b style={{ width: `${item.progress}%` }} /></i>
                  <small>{item.books.length} 本书 · 已完成 {item.progress}%</small>
                </span>
              </div>
              <div className="focused-series-actions">
                <button
                  type="button"
                  className="series-rename-button"
                  aria-label={`重命名系列：${item.title}`}
                  title="重命名系列"
                  onClick={() => setRenameTarget(item)}
                >
                  <PencilIcon />
                </button>
                <button
                  className="series-delete-button"
                  aria-label={`删除 ${item.title}`}
                  title="删除系列"
                  onClick={() => setDeleteTarget(item)}
                >
                  <TrashIcon />
                </button>
              </div>
            </header>
            <div className="focused-series-book-bay">
              <div className="focused-series-books">
                {item.books.map((book, bookIndex) => {
                  const canReviewOutline = bookReadyForOutlineReview(item, bookIndex);
                  const nextSectionId = nextBookSection(book)?.section.id
                    || book.chapters.flatMap((chapter) => chapter.sections)
                      .find((candidate) => candidate.status !== 'locked')?.id
                    || null;
                  const locked = book.status === 'locked';
                  return (
                    <button
                      type="button"
                      className={`focused-book-volume book-tone-${(seriesIndex + bookIndex) % 6} is-${book.status} ${canReviewOutline ? 'is-outline-ready' : ''}`}
                      style={{ height: `${220 + ((bookIndex * 19) % 34)}px` }}
                      key={book.id}
                      disabled={locked && !canReviewOutline}
                      onClick={() => {
                        if (canReviewOutline) void onActivateBook(book);
                        else onOpen(item.id, nextSectionId, book.id);
                      }}
                    >
                      <span className="focused-book-number">第 {book.position} 本</span>
                      <strong>{book.title}</strong>
                      <small>{canReviewOutline ? '待确认' : bookProgressLabel(book, false)}</small>
                      <span className="focused-book-progress">
                        <span>{bookProgressCopy(book, nextSectionId)}</span>
                        <b>{book.progress}%</b>
                      </span>
                      <i className="focused-book-progress-track">
                        <b style={{ width: `${book.progress}%` }} />
                      </i>
                      <span className="focused-book-action">
                        {canReviewOutline
                          ? '查看并确认目录'
                          : locked
                            ? '完成前一本后解锁'
                            : book.progress > 0
                              ? '继续这本'
                              : '打开这本'}
                        {(!locked || canReviewOutline) && <i aria-hidden="true">→</i>}
                      </span>
                    </button>
                  );
                })}
                {item.books.length === 0 && (
                  <div className="focused-series-no-books">
                    <span>这排还没有书</span>
                    <small>学习路径正在准备中。</small>
                  </div>
                )}
              </div>
              <span className="focused-shelf-board" aria-hidden="true"><i /></span>
            </div>
          </article>
        ))}
        {shelf.series.length === 0 && (
          <div className="empty-shelf-message">
            <span>书架还是空的</span>
            <small>点击“创建学习系列”，新增一排围绕明确目标组织的书。</small>
          </div>
        )}
      </div>
      {renameTarget && (
        <SeriesRenameDialog
          series={renameTarget}
          onClose={() => setRenameTarget(null)}
          onRename={async (body) => {
            await onRenameSeries(renameTarget.id, body);
            setRenameTarget(null);
          }}
        />
      )}
      {deleteTarget && (
        <div
          className="confirm-backdrop"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget && !deleting) setDeleteTarget(null);
          }}
        >
          <section
            ref={deleteDialogRef}
            className="delete-confirm series-delete-confirm"
            role="dialog"
            aria-modal="true"
            aria-labelledby="delete-series-title"
            aria-describedby="delete-series-description"
            tabIndex={-1}
          >
            <span className="delete-confirm-icon"><TrashIcon size={20} /></span>
            <p className="eyebrow">删除学习系列</p>
            <h2 id="delete-series-title">删除“{deleteTarget.title}”？</h2>
            <p id="delete-series-description">
              {deleteTarget.books.length > 0
                ? `以下 ${deleteTarget.books.length} 本教材及其章节会从书架和学习入口中移除：`
                : '这个系列目前没有教材，删除后将从书架移除。'}
            </p>
            {deleteTarget.books.length > 0 && (
              <ol className="series-delete-book-list" aria-label="将移除的教材">
                {deleteTarget.books.map((book) => (
                  <li key={book.id}>
                    <small>第 {book.position} 本</small>
                    <span>{book.title}</span>
                  </li>
                ))}
              </ol>
            )}
            <p className="shelf-delete-history-note">已有学习记录会保留，但当前界面暂不支持恢复。</p>
            <div>
              <button data-dialog-initial-focus className="quiet-button" disabled={deleting} onClick={() => setDeleteTarget(null)}>取消</button>
              <button
                className="danger-button"
                disabled={deleting}
                onClick={async () => {
                  setDeleting(true);
                  try {
                    await onDelete(deleteTarget.id);
                    setDeleteTarget(null);
                  } catch {
                    // App-level error presentation already explains the failure.
                  } finally {
                    setDeleting(false);
                  }
                }}
              >
                {deleting
                  ? '正在删除…'
                  : deleteTarget.books.length > 0
                    ? `删除系列和 ${deleteTarget.books.length} 本书`
                    : '确认删除系列'}
              </button>
            </div>
          </section>
        </div>
      )}
    </section>
  );
}

function TrashIcon({ size = 16 }: { size?: number }) {
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none">
      <path d="M4 7h16M9 3h6l1 4H8l1-4Z" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
      <path d="m7 7 1 13h8l1-13M10 11v5M14 11v5" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
    </svg>
  );
}

function PencilIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M4 20h4.2L19 9.2a2.15 2.15 0 0 0 0-3L17.8 5a2.15 2.15 0 0 0-3 0L4 15.8V20Z" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
      <path d="m13.5 6.3 4.2 4.2" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}

type GoalDirection = {
  id: string;
  title: string;
  description: string;
  purpose: string;
  success: string;
  outOfScope: string;
  depth: 'overview' | 'deep' | 'mastery';
};

type GoalOutcome = {
  id: string;
  title: string;
  description: string;
  purpose?: string;
  success: string;
  depth: 'overview' | 'deep' | 'mastery';
};

function isLanguageLearningTopic(topic: string) {
  return /英语|英文|日语|韩语|法语|德语|西班牙语|俄语|雅思|托福|四六级|口语|听力|外语/.test(topic);
}

function isSchoolSubjectTopic(topic: string) {
  return /小学|初一|初二|初三|初中|高一|高二|高三|高中|语文|数学|物理|化学|生物|历史|地理|政治/.test(topic);
}

function goalDirectionsFor(profile: LearningProfile, topic: string): GoalDirection[] {
  const subject = topic.trim() || '这个领域';
  const identity = `${profile.profession} ${profile.purpose} ${profile.domains.join(' ')}`.toLowerCase();
  if (isLanguageLearningTopic(subject)) {
    return [
      {
        id: 'language-course',
        title: '跟上当前课程',
        description: '围绕现阶段教材、作业和考试要求，把最影响学习的能力先补起来。',
        purpose: `提升${subject}课程中的理解与运用表现，优先解决当前学习任务。`,
        success: `能够完成一项符合当前阶段要求的${subject}任务，并说明自己的判断依据。`,
        outOfScope: '暂不追求超出当前阶段的生僻知识和全面语言学理论。',
        depth: 'deep',
      },
      {
        id: 'language-gap',
        title: '补一个具体薄弱项',
        description: '把词汇语法、阅读写作或听说中的主要卡点单独解决。',
        purpose: `找出并补强${subject}中最影响当前表现的一项具体能力。`,
        success: `能够在新的${subject}材料或任务中独立使用这项能力。`,
        outOfScope: '不同时铺开所有语言能力，只围绕当前最关键的薄弱项训练。',
        depth: 'deep',
      },
      {
        id: 'language-use',
        title: '建立真实语言能力',
        description: '不只为了做题，而是逐步形成能读、能表达或能交流的能力。',
        purpose: `围绕真实使用场景建立${subject}理解与表达能力。`,
        success: `能够在一个符合当前水平的真实场景中理解或表达完整信息。`,
        outOfScope: '暂不机械覆盖全部考试题型和低频语言知识。',
        depth: 'mastery',
      },
    ];
  }
  if (isSchoolSubjectTopic(subject)) {
    return [
      {
        id: 'course-progress',
        title: '跟上当前课程',
        description: '理解正在学的章节，并能独立完成典型学习任务。',
        purpose: `跟上${subject}当前课程，理解核心概念并解决典型问题。`,
        success: `能够独立完成一组${subject}典型任务，并解释关键步骤。`,
        outOfScope: '暂不提前覆盖后续年级内容，也不追求偏题和竞赛技巧。',
        depth: 'deep',
      },
      {
        id: 'subject-gap',
        title: '补一个具体薄弱点',
        description: '找到反复出错的原因，针对一个知识或方法缺口补强。',
        purpose: `定位并补强${subject}中最影响当前学习的一个具体缺口。`,
        success: `能够在一道新题或新材料中独立运用补强后的知识与方法。`,
        outOfScope: '不把整门学科重新学一遍，只处理当前关键缺口及必要前置。',
        depth: 'deep',
      },
      {
        id: 'subject-framework',
        title: '建立学科框架',
        description: '把章节、概念和解题方法连接起来，知道遇到问题该从哪里入手。',
        purpose: `建立${subject}的知识与方法框架，形成有依据的问题分析能力。`,
        success: `能够说明${subject}主要知识之间的关系，并为典型问题选择合适方法。`,
        outOfScope: '不机械覆盖全部知识点，也不以记忆题型代替理解。',
        depth: 'mastery',
      },
    ];
  }
  if (/投资|金融|研究员|分析师|基金|证券/.test(identity)) {
    return [
      {
        id: 'industry-map',
        title: '先建立行业地图',
        description: '看清参与者、价值链和关键变化，知道机会可能出现在哪里。',
        purpose: `建立对${subject}的行业全景认识，能够定位值得继续研究的环节与机会。`,
        success: `能够画出${subject}的主要参与者和价值流，并说明两个值得继续验证的机会方向。`,
        outOfScope: '不进入底层工程实现，也不直接给出具体投资建议。',
        depth: 'overview',
      },
      {
        id: 'company-reading',
        title: '看懂公司与产品',
        description: '理解必要技术指标，用来判断公司宣传、产品定位与竞争差异。',
        purpose: `看懂${subject}相关公司的产品与技术材料，形成基本的竞争力比较。`,
        success: '能够比较两家公司的产品定位，指出优势判断依赖的技术与业务证据。',
        outOfScope: '不学习芯片设计、模型训练代码和生产系统运维。',
        depth: 'deep',
      },
      {
        id: 'investment-frame',
        title: '形成研究框架',
        description: '把行业、产品、商业模式和风险放进一套可复用的判断框架。',
        purpose: `围绕${subject}建立一套可复用的公司与行业研究框架。`,
        success: '能够独立完成一份结构化研究提纲，并明确结论、证据缺口与主要风险。',
        outOfScope: '不预测短期价格，不替代财务、法律和合规尽调。',
        depth: 'mastery',
      },
    ];
  }
  if (/教师|老师|教育|讲师|学生|研究生|本科|学校/.test(identity)) {
    return [
      {
        id: 'field-map',
        title: '进入一个新领域',
        description: '建立准确的概念地图，知道核心问题、发展脉络与现实边界。',
        purpose: `建立对${subject}的整体认识，能够准确解释核心概念与主要问题。`,
        success: `能够不用术语堆砌，向同学或同事讲清${subject}的核心机制和一个重要边界。`,
        outOfScope: '不追求专业工程实现，也不机械覆盖所有历史与名词。',
        depth: 'overview',
      },
      {
        id: 'teaching-frame',
        title: '形成可讲授框架',
        description: '把知识组织成能解释、能举例、也能检查理解的教学结构。',
        purpose: `形成一套可以讲授${subject}的知识框架与案例线索。`,
        success: '能够设计一段完整讲解，并用问题判断学习者是否真正理解。',
        outOfScope: '不把教学形式本身扩成新的学习目标。',
        depth: 'deep',
      },
      {
        id: 'academic-use',
        title: '用于研究或课程',
        description: '理解关键机制、证据和争议，为研究或课程设计打基础。',
        purpose: `系统理解${subject}的机制、证据边界与典型研究问题。`,
        success: '能够提出一个有边界的研究或课程问题，并说明需要什么证据回答。',
        outOfScope: '不把未经核实的观点当作学术结论。',
        depth: 'mastery',
      },
    ];
  }
  if (/开发|工程|程序|技术|架构|数据|算法|产品经理|设计师/.test(identity)) {
    return [
      {
        id: 'technical-map',
        title: '建立技术地图',
        description: '先理解系统由什么组成、关键机制怎样连接、边界在哪里。',
        purpose: `建立${subject}的技术全景，能够解释核心组件、机制与适用边界。`,
        success: '能够画出最小系统结构，并解释关键组件为什么这样协作。',
        outOfScope: '暂不进入大规模生产优化和复杂源码细节。',
        depth: 'overview',
      },
      {
        id: 'build-something',
        title: '完成一个实现',
        description: '围绕真实产出学习必要原理、实现步骤和验证方法。',
        purpose: `能够使用${subject}完成一个可运行、可验证的实际实现。`,
        success: '能够独立完成最小实现，记录验证结果，并解释一个失败边界。',
        outOfScope: '不为追求知识完整而扩展与实现无关的主题。',
        depth: 'deep',
      },
      {
        id: 'production-judgment',
        title: '形成工程判断',
        description: '能够比较方案、识别约束，并为真实系统做出有依据的选择。',
        purpose: `围绕${subject}形成可迁移的实现、评估与技术选型能力。`,
        success: '能够针对一个真实约束比较方案，给出选择、验证计划和回退边界。',
        outOfScope: '不把记忆 API 或命令当成独立掌握。',
        depth: 'mastery',
      },
    ];
  }
  return [
    {
      id: 'understand',
      title: '先建立整体认识',
      description: '抓住核心对象、用途和边界，知道接下来是否值得继续深入。',
      purpose: `建立对${subject}的基本认识，能够解释它解决什么问题、怎样工作以及何时不适用。`,
      success: `能够用自己的话讲清${subject}的核心机制，并判断一个典型场景是否适用。`,
      outOfScope: '不展开专业实现与复杂边缘主题。',
      depth: 'overview',
    },
    {
      id: 'solve',
      title: '解决一个实际问题',
      description: '围绕使用场景理解机制、典型应用与必要边界。',
      purpose: `理解并使用「${subject}」解决一个真实场景中的问题。`,
      success: '能够独立分析场景、选择方法并说明验证结果。',
      outOfScope: '不扩展与当前任务无关的理论和工具。',
      depth: 'deep',
    },
    {
      id: 'capability',
      title: '形成可迁移能力',
      description: '不仅会做一次，还能比较方案、处理边界并迁移到新场景。',
      purpose: `围绕${subject}形成可以独立判断、实践并迁移的能力。`,
      success: '能够在新场景中独立完成任务，解释选择依据并处理主要边界。',
      outOfScope: '不以机械覆盖全部知识为目标。',
      depth: 'mastery',
    },
  ];
}

function goalOutcomesFor(
  profile: LearningProfile,
  topic: string,
  direction: GoalDirection,
): GoalOutcome[] {
  const subject = topic.trim() || '这个主题';
  const identity = `${profile.profession} ${profile.purpose} ${profile.domains.join(' ')}`.toLowerCase();
  if (isLanguageLearningTopic(subject)) {
    if (direction.id === 'language-course') {
      return [
        { id: 'language-reading', title: '能读懂当前阶段的材料', description: '抓住主旨、细节和上下文线索，不再只靠逐词翻译。', purpose: `提升${subject}阅读理解能力，学会从当前阶段的材料中提取和判断信息。`, success: `能够读懂一篇符合${subject}当前阶段的材料，指出主旨、关键细节与判断依据。`, depth: 'deep' },
        { id: 'language-grammar', title: '能把词汇和语法用对', description: '在新句子和题目中正确理解、选择并使用。', purpose: `掌握${subject}当前阶段的核心词汇与语法，并能在新语境中正确运用。`, success: `能够在新的${subject}句子和任务中正确使用本阶段核心词汇与语法，并解释选择依据。`, depth: 'deep' },
        { id: 'language-writing', title: '能完成当前阶段的写作任务', description: '内容完整、结构清楚，常见语言错误可自行检查。', purpose: `提升${subject}写作能力，能够围绕当前阶段要求组织内容并完成修改。`, success: `能够独立完成一篇符合${subject}当前阶段要求的短文，并根据检查清单完成一次修改。`, depth: 'mastery' },
      ];
    }
    if (direction.id === 'language-gap') {
      return [
        { id: 'language-vocabulary-gap', title: '解决词汇与语法卡点', description: '理解规则，并能在新语境中正确使用。', purpose: `补强${subject}中当前最影响表现的词汇或语法能力。`, success: `能够在新的${subject}语境中正确理解和使用当前薄弱的词汇或语法，并解释常见错误。`, depth: 'deep' },
        { id: 'language-input-gap', title: '提升听力或阅读理解', description: '抓住信息结构，不再被个别生词完全卡住。', purpose: `补强${subject}听力或阅读理解能力，能够抓住材料中的主要信息与线索。`, success: `能够从一段符合当前水平的${subject}听力或阅读材料中提取主旨、关键细节与线索。`, depth: 'deep' },
        { id: 'language-output-gap', title: '提升口语或写作表达', description: '把想法组织成对方能够理解的完整表达。', purpose: `补强${subject}口语或写作表达能力，把想法组织成完整、可理解的信息。`, success: `能够围绕一个熟悉主题完成一段${subject}口头或书面表达，并自行修正主要问题。`, depth: 'mastery' },
      ];
    }
    return [
      { id: 'language-read-real', title: '能读懂真实材料', description: '从短文、说明或内容中获得自己需要的信息。', purpose: `建立使用${subject}阅读真实材料和获取信息的能力。`, success: `能够独立读懂一份符合当前水平的${subject}真实材料，并准确提取所需信息。`, depth: 'overview' },
      { id: 'language-express-real', title: '能清楚表达自己的意思', description: '不追求完美，但能让对方准确理解。', purpose: `建立使用${subject}清楚表达观点与信息的能力。`, success: `能够使用${subject}围绕一个熟悉主题完成结构清楚的口头或书面表达。`, depth: 'deep' },
      { id: 'language-communicate-real', title: '能完成一次真实沟通', description: '理解对方、回应信息，并处理一次澄清或追问。', purpose: `建立使用${subject}完成真实沟通、回应和澄清的能力。`, success: `能够在一个符合当前水平的场景中使用${subject}完成交流，并处理一次澄清或追问。`, depth: 'mastery' },
    ];
  }
  if (isSchoolSubjectTopic(subject)) {
    if (direction.id === 'course-progress') {
      return [
        { id: 'course-understand', title: '能讲清当前章节', description: '概念、规律和典型例子能够连接起来。', success: `能够用自己的话讲清${subject}当前章节的核心概念、关系与一个典型例子。`, depth: 'overview' },
        { id: 'course-solve', title: '能独立完成典型问题', description: '知道使用什么方法，也能解释关键步骤。', success: `能够独立完成一组${subject}当前章节的典型问题，并解释方法选择与关键步骤。`, depth: 'deep' },
        { id: 'course-check', title: '能稳定完成一次综合检验', description: '面对题目变化仍能分析、作答并检查错误。', success: `能够完成一次${subject}当前阶段的综合检验，定位错误并说明改进方法。`, depth: 'mastery' },
      ];
    }
    if (direction.id === 'subject-gap') {
      return [
        { id: 'gap-locate', title: '能找到自己错在哪里', description: '区分概念不清、方法不熟和粗心等不同原因。', success: `能够分析三次${subject}相关错误，定位知识、方法或执行层面的主要原因。`, depth: 'overview' },
        { id: 'gap-practice', title: '能解决这一类问题', description: '补足必要知识，并形成稳定的问题处理步骤。', success: `能够独立解决一组围绕${subject}当前薄弱点的新问题，并解释每一步依据。`, depth: 'deep' },
        { id: 'gap-transfer', title: '题目变化后仍然会做', description: '不依赖原题记忆，能把方法迁移到新形式。', success: `能够把补强后的${subject}知识或方法迁移到一个新形式的问题，并说明变化在哪里。`, depth: 'mastery' },
      ];
    }
    return [
      { id: 'framework-connect', title: '能说清知识之间的关系', description: '知道章节不是孤立的，前后概念怎样连接。', success: `能够画出${subject}核心知识之间的关系，并解释三条关键连接。`, depth: 'overview' },
      { id: 'framework-choose', title: '能为问题选择合适方法', description: '先判断结构，再决定使用哪个知识或工具。', success: `能够针对三类${subject}典型问题选择合适方法，并说明判断依据。`, depth: 'deep' },
      { id: 'framework-reason', title: '能解决跨章节综合问题', description: '组合多个知识点，完整表达分析过程。', success: `能够独立解决一个${subject}跨章节问题，清楚表达推理链并检查边界条件。`, depth: 'mastery' },
    ];
  }
  if (/投资|金融|研究员|分析师|基金|证券/.test(identity)) {
    if (direction.id === 'industry-map') {
      return [
        { id: 'explain-landscape', title: '能讲清行业怎么运转', description: '说清参与者、价值流和关键变化。', success: `能够画出${subject}的主要参与者与价值流，并讲清它们怎样相互影响。`, depth: 'overview' },
        { id: 'spot-signals', title: '能找到值得研究的线索', description: '从变化中识别机会，同时知道还缺什么证据。', success: `能够指出${subject}中两个值得继续验证的机会方向，并列出结论成立所需的证据。`, depth: 'deep' },
        { id: 'write-industry-note', title: '能完成一份行业研究提纲', description: '把格局、变量、风险和待验证问题组织起来。', success: `能够独立完成一份${subject}行业研究提纲，明确核心判断、证据缺口与主要风险。`, depth: 'mastery' },
      ];
    }
    if (direction.id === 'company-reading') {
      return [
        { id: 'read-material', title: '能读懂一份产品材料', description: '区分技术事实、产品定位和宣传表达。', success: `能够读懂一份${subject}相关产品材料，标出关键指标、适用场景和仍需核实的说法。`, depth: 'overview' },
        { id: 'compare-companies', title: '能比较两家公司', description: '找到差异，并说明判断依赖什么证据。', success: direction.success, depth: 'deep' },
        { id: 'company-thesis', title: '能形成公司研究提纲', description: '把产品、竞争力、商业模式与风险连起来。', success: `能够围绕${subject}完成一份公司研究提纲，给出竞争力判断、证据依据与反例条件。`, depth: 'mastery' },
      ];
    }
    return [
      { id: 'use-checklist', title: '能使用一套研究清单', description: '面对新公司时知道该看什么、先问什么。', success: `能够使用一套结构化清单分析一个${subject}相关对象，并标出信息缺口。`, depth: 'overview' },
      { id: 'make-judgment', title: '能形成有依据的判断', description: '结论、证据和风险能够相互对应。', success: direction.success, depth: 'deep' },
      { id: 'transfer-framework', title: '能把框架迁移到新对象', description: '换一家公司或细分领域仍能独立分析。', success: `能够把${subject}研究框架迁移到一个新对象，解释调整了哪些判断维度及原因。`, depth: 'mastery' },
    ];
  }
  if (/教师|老师|教育|讲师|学生|研究生|本科|学校/.test(identity)) {
    if (direction.id === 'field-map') {
      return [
        { id: 'explain-clearly', title: '能向别人讲清核心机制', description: '不靠术语堆砌，也能回答“为什么”。', success: `能够向同学或学习者讲清${subject}的核心机制，并回答一个常见追问。`, depth: 'overview' },
        { id: 'spot-misconception', title: '能识别常见误解', description: '知道哪里最容易懂错，以及错在哪里。', success: `能够识别关于${subject}的三个常见误解，并用机制或例子解释为什么不成立。`, depth: 'deep' },
        { id: 'decide-next-study', title: '能判断下一步是否深入', description: '知道这个领域的核心问题、价值和进入门槛。', success: `能够说明${subject}值得继续深入的两个方向、所需基础和自己的下一步选择。`, depth: 'overview' },
      ];
    }
    if (direction.id === 'teaching-frame') {
      return [
        { id: 'organize-explanation', title: '能组织一段清楚的讲解', description: '有顺序、有例子，也有明确边界。', success: `能够围绕${subject}完成一段结构清楚的讲解，包含机制、例子和一个边界。`, depth: 'overview' },
        { id: 'teach-and-check', title: '能讲，也能检查是否听懂', description: '用问题识别表面记忆和真实理解。', success: direction.success, depth: 'deep' },
        { id: 'design-mini-lesson', title: '能设计一节可用的小课', description: '目标、讲解、活动和验证彼此一致。', success: `能够设计一节关于${subject}的小课，并说明每个活动怎样服务学习目标。`, depth: 'mastery' },
      ];
    }
    return [
      { id: 'frame-question', title: '能提出一个有边界的问题', description: '问题不空泛，也知道它为什么值得回答。', success: `能够围绕${subject}提出一个边界清楚的问题，并说明它的研究或课程价值。`, depth: 'overview' },
      { id: 'evaluate-evidence', title: '能判断证据是否支持结论', description: '区分观点、相关性和真正有力的证据。', success: direction.success, depth: 'deep' },
      { id: 'design-inquiry', title: '能形成研究或课程方案', description: '问题、材料、方法和判断标准互相对应。', success: `能够围绕${subject}形成一份研究或课程方案，并说明需要的证据与判断标准。`, depth: 'mastery' },
    ];
  }
  if (/开发|工程|程序|技术|架构|数据|算法|产品经理|设计师/.test(identity)) {
    if (direction.id === 'technical-map') {
      return [
        { id: 'read-system', title: '能看懂系统由什么组成', description: '说清组件、数据流和彼此关系。', success: `能够画出${subject}的最小系统结构，并解释关键组件为什么这样协作。`, depth: 'overview' },
        { id: 'explain-mechanism', title: '能解释关键机制为什么有效', description: '不只知道怎么用，还知道结果怎样产生。', success: `能够解释${subject}的关键机制、可观察结果以及一个失效条件。`, depth: 'deep' },
        { id: 'judge-fit', title: '能判断它是否适合当前场景', description: '把能力边界和真实约束放在一起比较。', success: `能够根据一个真实约束判断${subject}是否适用，并说明替代方案与验证方法。`, depth: 'deep' },
      ];
    }
    if (direction.id === 'build-something') {
      return [
        { id: 'follow-example', title: '能完成一个基础例子', description: '先跑通完整链路，理解每一步在做什么。', success: `能够完成一个${subject}基础例子，并解释关键步骤与输入输出。`, depth: 'overview' },
        { id: 'build-minimum', title: '能独立做出最小实现', description: '跑得起来，也知道怎样验证是否有效。', success: `能够使用${subject}完成一个最小实现，记录验证结果，并解释一个失败边界。`, depth: 'deep' },
        { id: 'debug-and-extend', title: '能定位问题并继续扩展', description: '面对变化和失败，不需要重新照抄教程。', success: `能够诊断一个${subject}实现中的问题，完成修复，并把方案扩展到一个新需求。`, depth: 'mastery' },
      ];
    }
    return [
      { id: 'compare-solutions', title: '能比较两个候选方案', description: '让差异回到约束、成本和风险。', success: `能够针对一个真实约束比较两个${subject}相关方案，并说明关键取舍。`, depth: 'overview' },
      { id: 'choose-architecture', title: '能给出可验证的技术选择', description: '选择、验证计划和回退条件都说得清。', success: direction.success, depth: 'deep' },
      { id: 'transfer-judgment', title: '能把判断迁移到新场景', description: '约束变化后，知道哪些结论要重新评估。', success: `能够把${subject}的选型方法迁移到一个新场景，解释约束变化怎样影响结论。`, depth: 'mastery' },
    ];
  }
  if (direction.id === 'understand') {
    return [
      { id: 'explain', title: '能用自己的话讲清楚', description: '抓住核心机制，而不是只记住几个名词。', success: `能够用自己的话讲清${subject}解决什么问题、怎样工作以及一个重要边界。`, depth: 'overview' },
      { id: 'recognize', title: '能识别真实例子与常见误解', description: '遇到案例时知道它属于什么、又不属于什么。', success: `能够判断三个${subject}相关案例是否成立，并解释一个常见误解。`, depth: 'deep' },
      { id: 'decide-depth', title: '能决定是否值得继续深入', description: '知道价值、限制和下一阶段需要投入什么。', success: `能够说明${subject}的价值、主要限制与继续深入所需的基础，并做出下一步选择。`, depth: 'overview' },
    ];
  }
  if (direction.id === 'solve') {
    return [
      { id: 'analyze-problem', title: '能把问题分析清楚', description: '知道关键条件、目标和限制分别是什么。', success: `能够把一个${subject}相关问题拆成目标、条件、限制和验证标准。`, depth: 'overview' },
      { id: 'choose-method', title: '能选择合适的方法', description: '比较可选方案，并说明为什么这样选。', success: direction.success, depth: 'deep' },
      { id: 'complete-solution', title: '能完成并验证一个解决方案', description: '结果可检查，也知道失败后怎样调整。', success: `能够使用「${subject}」完成一个真实问题的解决方案，验证结果并分析一个失败边界。`, depth: 'mastery' },
    ];
  }
  return [
    { id: 'compare', title: '能比较不同做法', description: '知道差异来自哪里，不能只凭熟悉程度选择。', success: `能够比较三种${subject}相关做法，说明各自适用条件与主要代价。`, depth: 'overview' },
    { id: 'handle-boundary', title: '能处理变化和边界', description: '条件变化后，知道原来的做法为什么可能失效。', success: direction.success, depth: 'deep' },
    { id: 'transfer', title: '能迁移到一个新场景', description: '不靠照抄，也能重新判断并完成任务。', success: `能够把${subject}相关能力迁移到一个新场景，说明调整依据并验证结果。`, depth: 'mastery' },
  ];
}

type SeriesCreationStage = 'planning' | 'first-section' | 'opening';

const SERIES_CREATION_STAGES: {
  id: SeriesCreationStage;
  title: string;
  description: string;
  estimate: string;
}[] = [
  {
    id: 'planning',
    title: '章节规划中',
    description: '正在把确认好的目标拆成有顺序的书、章和节。',
    estimate: '通常 30–90 秒',
  },
  {
    id: 'first-section',
    title: '首节生成中',
    description: '正在生成第一节正文和完成后的验证题。',
    estimate: '通常 1–3 分钟',
  },
  {
    id: 'opening',
    title: '即将进入第一节',
    description: '内容已经发布，正在同步目录并打开阅读位置。',
    estimate: '通常不到 10 秒',
  },
];

function formatSeriesCreationElapsed(totalSeconds: number) {
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
}

function SeriesCreationProgress({
  stage,
  topic,
  elapsedSeconds,
}: {
  stage: SeriesCreationStage;
  topic: string;
  elapsedSeconds: number;
}) {
  const currentIndex = SERIES_CREATION_STAGES.findIndex((item) => item.id === stage);
  const current = SERIES_CREATION_STAGES[currentIndex];
  return (
    <section className="series-generation-progress" aria-live="polite" aria-busy="true">
      <div className="series-generation-progress-copy">
        <p className="eyebrow">正在把目标变成教材</p>
        <h2>正在生成「{topic}」</h2>
        <p className="series-generation-current">
          <span>{String(currentIndex + 1).padStart(2, '0')}</span>
          <strong>{current.title}</strong>
        </p>
        <p className="series-generation-description">{current.description}</p>
        <div className="series-generation-timing">
          <span>全程通常需要 2–5 分钟</span>
          <span>已等待 {formatSeriesCreationElapsed(elapsedSeconds)}</span>
        </div>
      </div>
      <ol className="series-generation-stages" aria-label="教材生成进度">
        {SERIES_CREATION_STAGES.map((item, index) => {
          const state = index < currentIndex ? 'complete' : index === currentIndex ? 'current' : 'pending';
          return (
            <li className={state} key={item.id} aria-current={state === 'current' ? 'step' : undefined}>
              <span className="series-generation-marker" aria-hidden="true">{state === 'complete' ? '✓' : index + 1}</span>
              <div>
                <strong>{item.title}</strong>
                <p>{item.description}</p>
                <small>{state === 'current' ? item.estimate : state === 'complete' ? '已完成' : '等待开始'}</small>
              </div>
            </li>
          );
        })}
      </ol>
      <footer>
        <span className="series-generation-pulse" aria-hidden="true" />
        请保持此页面打开，准备好后会自动进入第一节。
      </footer>
    </section>
  );
}

function SeriesCreationPage({
  shelf,
  profile,
  onboardingSeed,
  onCreate,
  onCancel,
}: {
  shelf: Shelf;
  profile: LearningProfile;
  onboardingSeed: FirstSeriesSeed | null;
  onCreate: (
    body: object,
    idempotencyKey: string,
    onProgress: (stage: SeriesCreationStage) => void,
  ) => Promise<void>;
  onCancel: () => void;
}) {
  const maxInterviewAnswers = 8;
  const [step, setStep] = useState<'intent' | 'clarify' | 'confirm' | 'map'>('intent');
  const [topic, setTopic] = useState(onboardingSeed?.topic || '');
  const [dailyCommitmentHours, setDailyCommitmentHours] = useState('1');
  const [completionHorizonValue, setCompletionHorizonValue] = useState('2');
  const [completionHorizonUnit, setCompletionHorizonUnit] = useState<'day' | 'week' | 'month'>('week');
  const [experience, setExperience] = useState(
    onboardingSeed?.experience || profile.experience || '',
  );
  const [interview, setInterview] = useState<LearningGoalInterview | null>(null);
  const [goalDraft, setGoalDraft] = useState<NonNullable<LearningGoalInterview['brief']> | null>(null);
  const [interviewAnswers, setInterviewAnswers] = useState<LearningGoalInterviewAnswer[]>([]);
  const [interviewHistory, setInterviewHistory] = useState<{
    interview: LearningGoalInterview;
    answers: LearningGoalInterviewAnswer[];
  }[]>([]);
  const [selectedInterviewAnswer, setSelectedInterviewAnswer] = useState('');
  const [customInterviewAnswer, setCustomInterviewAnswer] = useState('');
  const [interviewBusy, setInterviewBusy] = useState(false);
  const [preview, setPreview] = useState<LearningStartPreview | null>(null);
  const [selectedConcepts, setSelectedConcepts] = useState<string[]>([]);
  const [learningPreferences, setLearningPreferences] = useState<LearningStartPreference[]>([]);
  const [formError, setFormError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [creationStage, setCreationStage] = useState<SeriesCreationStage>('planning');
  const [creationStartedAt, setCreationStartedAt] = useState<number | null>(null);
  const [creationElapsedSeconds, setCreationElapsedSeconds] = useState(0);
  const [previewing, setPreviewing] = useState(false);
  const idempotencyKey = useRef(crypto.randomUUID());
  const goalBrief = goalDraft || interview?.brief || null;
  const currentStep = step === 'intent' ? 1 : step === 'clarify' ? 2 : 3;
  const resolvedDimensionCount = interview?.dimensions.filter((item) => (
    item.status === 'confirmed' || item.status === 'inferred' || item.status === 'immaterial'
  )).length || 0;

  useEffect(() => {
    window.scrollTo({ top: 0, left: 0, behavior: 'auto' });
  }, [step]);

  useEffect(() => {
    if (!submitting || creationStartedAt === null) return undefined;
    const updateElapsed = () => {
      setCreationElapsedSeconds(Math.max(0, Math.floor((Date.now() - creationStartedAt) / 1000)));
    };
    updateElapsed();
    const timer = window.setInterval(updateElapsed, 1000);
    return () => window.clearInterval(timer);
  }, [creationStartedAt, submitting]);

  const planDetails = {
    shelfId: shelf.id,
    topic: goalBrief?.topic || topic.trim(),
    role: profile.profession,
    experience: goalBrief?.startingPoint || experience.trim() || profile.experience || '尚未提供相关经验',
    purpose: goalBrief?.purpose || '',
    depth: goalBrief?.recommendedDepth || 'deep',
    details: [
      goalBrief?.successMarker ? `用户确认的完成标志：${goalBrief.successMarker}` : '',
      goalBrief?.dailyCommitment ? `每天投入：${goalBrief.dailyCommitment}` : '',
      goalBrief?.completionHorizon ? `完成周期：${goalBrief.completionHorizon}` : '',
      goalBrief?.scope ? `本次优先范围：${goalBrief.scope}` : '',
      goalBrief?.outOfScope ? `本次暂不覆盖：${goalBrief.outOfScope}` : '',
    ].filter(Boolean).join('\n'),
  };

  const requestInterview = async (
    answers: LearningGoalInterviewAnswer[],
    previous?: LearningGoalInterview,
  ) => {
    setInterviewBusy(true);
    setFormError('');
    try {
      const value = await api.learningGoalInterview({
        shelfId: shelf.id,
        topic: topic.trim(),
        dailyCommitmentHours: Number(dailyCommitmentHours),
        completionHorizonValue: Number(completionHorizonValue),
        completionHorizonUnit,
        relatedExperience: experience.trim(),
        answers,
      });
      if (previous) {
        setInterviewHistory((current) => [...current, { interview: previous, answers: interviewAnswers }]);
      }
      setInterviewAnswers(answers);
      setInterview(value);
      setSelectedInterviewAnswer('');
      setCustomInterviewAnswer('');
      if (value.status === 'ready' && value.brief) {
        setGoalDraft({ ...value.brief });
        setStep('confirm');
      } else {
        setStep('clarify');
      }
    } catch (reason) {
      setFormError(reason instanceof Error ? reason.message : '暂时无法继续明确目标，请重试');
    } finally {
      setInterviewBusy(false);
    }
  };
  const continueInterview = async () => {
    if (interviewBusy) return;
    const question = interview?.question;
    if (!question) return;
    if (interviewAnswers.length >= maxInterviewAnswers) {
      await requestInterview(interviewAnswers, interview);
      return;
    }
    const selectedOption = question.options.find((item) => item.id === selectedInterviewAnswer);
    const answer = customInterviewAnswer.trim()
      || (selectedOption
        ? `${selectedOption.label}${selectedOption.description ? `：${selectedOption.description}` : ''}`
        : '');
    if (!answer) {
      setFormError('请选择最接近的一项，或直接写下你的答案');
      return;
    }
    await requestInterview([
      ...interviewAnswers,
      {
        questionId: question.id,
        dimension: question.dimension,
        question: question.prompt,
        answer,
      },
    ], interview);
  };
  const continueFromIntent = async (event: FormEvent) => {
    event.preventDefault();
    if (interviewBusy) return;
    if (!topic.trim()) {
      setFormError('先写下这次真正想了解或解决的内容');
      return;
    }
    if (!dailyCommitmentHours.trim() || Number(dailyCommitmentHours) <= 0) {
      setFormError('请输入每天可以投入的小时数');
      return;
    }
    if (!completionHorizonValue.trim() || Number(completionHorizonValue) <= 0) {
      setFormError('请先写明每天能投入多久，以及希望多久完成');
      return;
    }
    setInterview(null);
    setGoalDraft(null);
    setInterviewAnswers([]);
    setInterviewHistory([]);
    setFormError('');
    await requestInterview([]);
  };
  const submitPlan = async (mode: 'direct' | 'guided') => {
    if (submitting) return;
    if (!goalBrief || [goalBrief.topic, goalBrief.purpose, goalBrief.successMarker, goalBrief.startingPoint, goalBrief.dailyCommitment, goalBrief.completionHorizon, goalBrief.scope, goalBrief.outOfScope].some((item) => !item.trim())) {
      setFormError('先把目标确认稿补充完整，再创建系列');
      return;
    }
    if (mode === 'guided' && !selectedConcepts.length) {
      setFormError('至少点亮一个你愿意投入时间的方向');
      return;
    }
    setFormError('');
    setCreationStage('planning');
    setCreationStartedAt(Date.now());
    setCreationElapsedSeconds(0);
    setSubmitting(true);
    try {
      await onCreate(
        mode === 'guided' && preview
          ? {
              ...planDetails,
              startMode: 'guided',
              learningStartSelection: {
                previewId: preview.previewId,
                selectedConceptRevisionIds: selectedConcepts,
                learningPreferences,
              },
            }
          : { ...planDetails, startMode: 'direct' },
        idempotencyKey.current,
        setCreationStage,
      );
    } catch (reason) {
      setSubmitting(false);
      setCreationStartedAt(null);
      setFormError(reason instanceof Error ? reason.message : '学习路线生成失败，请稍后重试');
    }
  };
  const openKnowledgeMap = async () => {
    if (previewing) return;
    if (!goalBrief || [goalBrief.topic, goalBrief.purpose, goalBrief.successMarker, goalBrief.startingPoint, goalBrief.dailyCommitment, goalBrief.completionHorizon, goalBrief.scope, goalBrief.outOfScope].some((item) => !item.trim())) {
      setFormError('先把目标确认稿补充完整，再选择重点');
      return;
    }
    setPreviewing(true);
    setFormError('');
    try {
      const value = await api.learningStartPreview(planDetails);
      setPreview(value);
      setSelectedConcepts([]);
      setLearningPreferences([]);
      setStep('map');
    } catch (reason) {
      setFormError(reason instanceof Error ? reason.message : '暂时无法打开知识版图');
    } finally {
      setPreviewing(false);
    }
  };

  return (
    <section className={`series-creation-page series-creation-${step}`} aria-labelledby="series-creation-title">
      <header className="series-creation-header">
        <button type="button" className="shelf-back-button" disabled={submitting} onClick={onCancel}>
          {onboardingSeed ? `← 暂时跳过，先看${shelf.name}书架` : `← 返回${shelf.name}书架`}
        </button>
        <div className="series-creation-heading">
          <div>
            <p className="eyebrow">创建学习系列</p>
            <h1 id="series-creation-title">这次，<em>想学会什么？</em></h1>
            <p>先写下主题和可投入时间，再一起把目标说清。</p>
          </div>
        </div>
        <nav className="series-creation-steps" aria-label="创建系列进度">
          {[
            ['01', '主题与时间'],
            ['02', '说清目标'],
            ['03', '确认开始'],
          ].map(([number, label], index) => (
            <span
              className={currentStep === index + 1 ? 'active current' : currentStep > index + 1 ? 'complete' : ''}
              key={number}
            >
              <b>{number}</b>{label}
            </span>
          ))}
        </nav>
      </header>

      {submitting && (
        <SeriesCreationProgress
          stage={creationStage}
          topic={goalBrief?.topic || topic.trim()}
          elapsedSeconds={creationElapsedSeconds}
        />
      )}

      {step === 'intent' && (
        <form className="series-intent-sheet" onSubmit={continueFromIntent}>
          {onboardingSeed && (
            <div className="series-onboarding-continuation" role="status">
              <span aria-hidden="true">✓</span>
              <p><b>「{shelf.name}」书架已创建</b>接着说清第一个学习目标，Slow 会据此准备第一套教材。</p>
            </div>
          )}
          <div className="series-goal-inscription">
            <span>学习主题</span>
            <label>
              <span className="sr-only">想学习的内容</span>
              <input
                autoFocus
                required
                value={topic}
                onChange={(event) => { setTopic(event.target.value); setFormError(''); }}
              />
            </label>
          </div>
          <div className="series-schedule-fields">
            <label>
              <span>每天能投入多久？</span>
              <span className="series-schedule-control">
                <input
                  required
                  type="number"
                  min="0.25"
                  max="24"
                  step="0.25"
                  inputMode="decimal"
                  aria-label="每天投入小时数"
                  value={dailyCommitmentHours}
                  onChange={(event) => { setDailyCommitmentHours(event.target.value); setFormError(''); }}
                />
                <b>小时</b>
              </span>
            </label>
            <label>
              <span>希望多久完成？</span>
              <span className="series-schedule-control series-schedule-control-horizon">
                <input
                  required
                  type="number"
                  min="1"
                  max="365"
                  step="1"
                  inputMode="numeric"
                  aria-label="完成周期数值"
                  value={completionHorizonValue}
                  onChange={(event) => { setCompletionHorizonValue(event.target.value); setFormError(''); }}
                />
                <select
                  aria-label="完成周期单位"
                  value={completionHorizonUnit}
                  onChange={(event) => { setCompletionHorizonUnit(event.target.value as 'day' | 'week' | 'month'); setFormError(''); }}
                >
                  <option value="day">天</option>
                  <option value="week">周</option>
                  <option value="month">月</option>
                </select>
              </span>
            </label>
          </div>
          <div className="series-experience-field">
            <div>
              <span>已经知道什么？ <small>可选</small></span>
              <small>写下接触过的内容，或者最容易卡住的地方。</small>
            </div>
            <textarea
              value={experience}
              onChange={(event) => { setExperience(event.target.value); setFormError(''); }}
            />
          </div>
          {formError && <p className="series-creation-error" role="alert">{formError}</p>}
          <footer className="series-creation-actions">
            <button className="primary-button" disabled={interviewBusy}>{interviewBusy ? '正在整理下一步…' : '下一步：说清目标 →'}</button>
          </footer>
        </form>
      )}

      {step === 'clarify' && interview?.question && (
        <div className={`series-interview-shell ${interviewBusy ? 'is-busy' : ''}`} aria-live="polite" aria-busy={interviewBusy}>
          <aside className="series-interview-context">
            <span>学习目标 · 已明确 {resolvedDimensionCount}/{interview.dimensions.length}</span>
            <strong>{topic}</strong>
            <div className="series-interview-dimension-groups">
              {([
                { title: '目标', keys: ['purpose', 'success_marker'] },
                { title: '条件', keys: ['starting_point', 'daily_commitment', 'completion_horizon'] },
                { title: '边界', keys: ['learning_object', 'scope'] },
              ] as const).map((group) => (
                <section key={group.title}>
                  <h3>{group.title}</h3>
                  <ol className="series-interview-dimensions">
                    {group.keys.map((key) => {
                      const item = interview.dimensions.find((dimension) => dimension.key === key);
                      if (!item) return null;
                      const labels = {
                        learning_object: '学习对象',
                        purpose: '实际目的',
                        success_marker: '完成标志',
                        starting_point: '当前起点',
                        daily_commitment: '每天投入',
                        completion_horizon: '完成周期',
                        scope: '范围取舍',
                      };
                      const resolved = item.status === 'confirmed' || item.status === 'inferred' || item.status === 'immaterial';
                      const active = interview.question?.dimension === item.key;
                      return (
                        <li className={`${resolved ? 'resolved' : item.status === 'conflict' ? 'conflict' : ''} ${active ? 'active' : ''}`} key={item.key}>
                          <i aria-hidden="true" />
                          <b>{labels[item.key]}</b>
                        </li>
                      );
                    })}
                  </ol>
                </section>
              ))}
            </div>
            {interview.generationMode === 'demo' && <p className="series-interview-demo">当前是 Demo 访谈，答案由本地规则生成。</p>}
          </aside>
          <section className="series-interview-question" key={interview.question.id}>
            <header>
              <span>第 {interviewAnswers.length + 1} 问</span>
              <small>已明确 {resolvedDimensionCount}/{interview.dimensions.length}</small>
            </header>
            <p className="eyebrow">继续把目标说清</p>
            <h2>{interview.question.prompt}</h2>
            <p>{interview.question.helper}</p>
            <p className="series-interview-selection-note">
              即使多项都符合，也请选择这次最优先的一项；其他情况可以在「我自己说」中补充。
            </p>
            <div className="series-interview-options">
              {interview.question.options.map((option, index) => (
                <button
                  type="button"
                  className={`series-interview-choice ${selectedInterviewAnswer === option.id ? 'selected' : ''}`}
                  aria-pressed={selectedInterviewAnswer === option.id}
                  disabled={interviewBusy}
                  onClick={() => { setSelectedInterviewAnswer(option.id); setCustomInterviewAnswer(''); setFormError(''); }}
                  key={option.id}
                >
                  <span>{String.fromCharCode(65 + index)}</span>
                  <b>{option.label}</b>
                  <small>{option.description}</small>
                  <i aria-hidden="true">→</i>
                </button>
              ))}
              <label className={`series-interview-choice series-interview-custom-answer ${customInterviewAnswer.trim() ? 'selected' : ''}`}>
                <span>{String.fromCharCode(65 + interview.question.options.length)}</span>
                <b>我自己说</b>
                <input
                  type="text"
                  value={customInterviewAnswer}
                  disabled={interviewBusy}
                  onChange={(event) => {
                    setCustomInterviewAnswer(event.target.value);
                    if (event.target.value) setSelectedInterviewAnswer('');
                    setFormError('');
                  }}
                  placeholder="直接写下你的真实情况"
                />
              </label>
            </div>
            {formError && <p className="series-creation-error" role="alert">{formError}</p>}
            <footer>
              <button
                type="button"
                className="quiet-button"
                disabled={interviewBusy}
                onClick={() => {
                  const previous = interviewHistory[interviewHistory.length - 1];
                  if (previous) {
                    setInterview(previous.interview);
                    setInterviewAnswers(previous.answers);
                    setInterviewHistory((current) => current.slice(0, -1));
                    setSelectedInterviewAnswer('');
                    setCustomInterviewAnswer('');
                  } else {
                    setStep('intent');
                  }
                  setFormError('');
                }}
              >
                ← {interviewHistory.length ? '回到上一问' : '修改主题与经验'}
              </button>
              <div>
                <button
                  type="button"
                  className="primary-button"
                  disabled={interviewBusy || (
                    interviewAnswers.length < maxInterviewAnswers
                    && !selectedInterviewAnswer
                    && !customInterviewAnswer.trim()
                  )}
                  onClick={() => void continueInterview()}
                >
                  {interviewBusy
                    ? interviewAnswers.length >= maxInterviewAnswers
                      ? '正在整理目标确认稿…'
                      : '正在整理下一问…'
                    : interviewAnswers.length >= maxInterviewAnswers
                      ? '整理目标确认稿 →'
                      : '继续 →'}
                </button>
              </div>
            </footer>
          </section>
        </div>
      )}

      {!submitting && step === 'confirm' && goalBrief && (
        <div className="series-confirm-layout">
          <article className="series-goal-brief series-goal-editor">
            <header>
              <span>确认学习目标</span>
            </header>
            <section className="series-goal-editor-section">
              <header><b>目标</b><span>学什么、为什么、做到什么</span></header>
              <div className="series-goal-editor-grid series-goal-editor-grid-objective">
                <label>
                  <span>学习对象</span>
                  <input
                    value={goalBrief.topic}
                    onChange={(event) => { setGoalDraft({ ...goalBrief, topic: event.target.value }); setFormError(''); }}
                    aria-label="学习对象"
                  />
                </label>
                {([
                  ['purpose', '实际目的'],
                  ['successMarker', '完成的标志'],
                ] as const).map(([field, label]) => (
                  <label key={field}>
                    <span>{label}</span>
                    <textarea
                      value={goalBrief[field]}
                      onChange={(event) => { setGoalDraft({ ...goalBrief, [field]: event.target.value }); setFormError(''); }}
                      aria-label={label}
                      rows={3}
                    />
                  </label>
                ))}
              </div>
            </section>
            <section className="series-goal-editor-section">
              <header><b>条件</b><span>起点与学习节奏</span></header>
              <div className="series-goal-editor-grid series-goal-editor-grid-condition">
                {([
                  ['startingPoint', '从哪里开始'],
                  ['dailyCommitment', '每天投入'],
                  ['completionHorizon', '完成周期'],
                ] as const).map(([field, label]) => (
                  <label key={field}>
                    <span>{label}</span>
                    <textarea
                      value={goalBrief[field]}
                      onChange={(event) => { setGoalDraft({ ...goalBrief, [field]: event.target.value }); setFormError(''); }}
                      aria-label={label}
                      rows={3}
                    />
                  </label>
                ))}
              </div>
            </section>
            <section className="series-goal-editor-section">
              <header><b>边界</b><span>这次覆盖什么、暂时放下什么</span></header>
              <div className="series-goal-editor-grid">
                {([
                  ['scope', '本次范围'],
                  ['outOfScope', '这次先不学'],
                ] as const).map(([field, label]) => (
                  <label key={field}>
                    <span>{label}</span>
                    <textarea
                      value={goalBrief[field]}
                      onChange={(event) => { setGoalDraft({ ...goalBrief, [field]: event.target.value }); setFormError(''); }}
                      aria-label={label}
                      rows={3}
                    />
                  </label>
                ))}
              </div>
            </section>
            {formError && <p className="series-creation-error" role="alert">{formError}</p>}
            <footer className="series-confirm-actions">
              <div>
                <button type="button" className="quiet-button" disabled={submitting || previewing} onClick={() => void openKnowledgeMap()}>
                  {previewing ? '正在打开学习重点…' : '选择学习重点'}
                </button>
                <button type="button" className="primary-button" disabled={submitting || previewing} onClick={() => void submitPlan('direct')}>
                  {submitting ? '正在创建学习系列…' : '确认目标，创建系列 →'}
                </button>
              </div>
            </footer>
          </article>
        </div>
      )}

      {!submitting && step === 'map' && preview && (() => {
        const ready = preview.availability === 'ready' && preview.nodes.length > 0;
        return (
          <section className="learning-start-flow knowledge-interest-step series-creation-map" aria-labelledby="knowledge-interest-title">
            <header className="learning-start-heading">
              <div>
                <p className="eyebrow">目标已经确认</p>
                <h2 id="knowledge-interest-title">再点亮你真正关心的内容</h2>
                <p>{preview.message}</p>
              </div>
              <span className="knowledge-selection-count">已点亮 <b>{selectedConcepts.length}</b></span>
            </header>
            {ready ? (
              <KnowledgeInterestGraph
                preview={preview}
                selected={selectedConcepts}
                onToggle={(conceptId) => {
                  setSelectedConcepts((current) => current.includes(conceptId)
                    ? current.filter((item) => item !== conceptId)
                    : [...current, conceptId]);
                  setFormError('');
                }}
              />
            ) : (
              <div className="knowledge-interest-empty">
                <span aria-hidden="true">◌</span>
                <h3>这个方向暂时没有可选择的知识关系</h3>
                <p>可以直接创建系列，进入每一章时仍然能学习、挑战或略过。</p>
              </div>
            )}
            {ready && (
              <fieldset className="learning-preference-picks">
                <legend>再选一两个学习偏好 <small>可选</small></legend>
                {([
                  ['practical_application', '实际应用'],
                  ['understand_principles', '理解原理'],
                  ['case_based', '案例带入'],
                  ['practice_heavy', '多做练习'],
                ] as [LearningStartPreference, string][]).map(([value, label]) => {
                  const selected = learningPreferences.includes(value);
                  return (
                    <button
                      type="button"
                      key={value}
                      className={selected ? 'selected' : ''}
                      aria-pressed={selected}
                      onClick={() => setLearningPreferences((current) => {
                        if (selected) return current.filter((item) => item !== value);
                        return current.length < 2 ? [...current, value] : current;
                      })}
                    >
                      <span aria-hidden="true">{selected ? '●' : '○'}</span>{label}
                    </button>
                  );
                })}
              </fieldset>
            )}
            {formError && <p className="series-creation-error" role="alert">{formError}</p>}
            <footer className="learning-start-footer">
              <button type="button" className="quiet-button" disabled={submitting} onClick={() => { setStep('confirm'); setFormError(''); }}>← 返回目标确认</button>
              <button type="button" className="primary-button" disabled={submitting} onClick={() => void submitPlan(ready ? 'guided' : 'direct')}>
                {submitting ? '正在创建学习系列…' : ready ? '按这些重点创建系列 →' : '直接创建系列 →'}
              </button>
            </footer>
          </section>
        );
      })()}
    </section>
  );
}

function KnowledgeInterestGraph({
  preview,
  selected,
  onToggle,
}: {
  preview: LearningStartPreview;
  selected: string[];
  onToggle: (conceptId: string) => void;
}) {
  const points = useMemo(() => {
    const count = preview.nodes.length;
    return preview.nodes.map((node, index) => {
      const ring = count > 8 && index % 3 !== 0 ? 35 : 24;
      const angle = -Math.PI / 2 + ((Math.PI * 2 * index) / Math.max(count, 1));
      return {
        ...node,
        x: 50 + Math.cos(angle) * ring,
        y: 50 + Math.sin(angle) * ring,
      };
    });
  }, [preview]);
  const pointById = new Map(points.map((point) => [point.conceptRevisionId, point]));
  return (
    <div className="knowledge-interest-graph" aria-label="可点选的知识关系图">
      <svg aria-hidden="true" viewBox="0 0 100 100" preserveAspectRatio="none">
        {preview.edges.map((edge) => {
          const from = pointById.get(edge.from);
          const to = pointById.get(edge.to);
          if (!from || !to) return null;
          const active = selected.includes(edge.from) && selected.includes(edge.to);
          return <line key={edge.id} x1={from.x} y1={from.y} x2={to.x} y2={to.y} className={active ? 'active' : ''} />;
        })}
      </svg>
      {points.map((point, index) => {
        const active = selected.includes(point.conceptRevisionId);
        return (
          <button
            type="button"
            key={point.conceptRevisionId}
            className={active ? 'active' : ''}
            aria-pressed={active}
            title={point.meaning}
            style={{ '--node-x': `${point.x}%`, '--node-y': `${point.y}%`, '--node-delay': `${index * 20}ms` } as CSSProperties}
            onClick={() => onToggle(point.conceptRevisionId)}
          >
            <span aria-hidden="true" />
            <b>{point.label}</b>
          </button>
        );
      })}
      <div className="knowledge-interest-center" aria-hidden="true">
        <span>你的目标</span>
        <b>{preview.topic}</b>
      </div>
    </div>
  );
}

type WorkspacePanel = 'directory' | 'qa';
type ChapterLaunchAction = 'challenge' | 'skip';
type WorkspaceLayoutRatios = {
  threeDirectory: number;
  threeQa: number;
  directoryOnly: number;
  qaOnly: number;
};
type WorkspaceLayoutRatioKey = keyof WorkspaceLayoutRatios;
type ReaderTypographyPreset = {
  label: string;
  bodySize: number;
  lineHeight: number;
  toolbarTitleSize: number;
  questionSize: number;
  blockTitleSize: number;
  markdownH1Size: number;
  markdownH2Size: number;
  markdownH3Size: number;
  paragraphGap: number;
};

const workspacePanelSizing = {
  directory: { defaultWidth: 240, minWidth: 220, maxWidth: 360 },
  qa: { defaultWidth: 380, minWidth: 300, maxWidth: Number.POSITIVE_INFINITY },
} as const;
// Side panels share at most this proportion. The remaining reader width is
// therefore stable across laptop and wide desktop viewports without relying
// on one fixed-pixel breakpoint.
const workspaceReaderMinRatio = 0.48;
const workspaceRatioMigrationReferenceWidth = 1440;
const workspaceFocusMediaQuery = '(max-width: 1280px), (max-height: 760px)';
const defaultReaderTypographyStep = 3;
const readerTypographyPresets: ReaderTypographyPreset[] = [
  { label: '极小', bodySize: 15.5, lineHeight: 2.05, toolbarTitleSize: 22, questionSize: 20, blockTitleSize: 25, markdownH1Size: 23, markdownH2Size: 21, markdownH3Size: 18, paragraphGap: 20 },
  { label: '较小', bodySize: 16.5, lineHeight: 2.02, toolbarTitleSize: 23, questionSize: 21, blockTitleSize: 27, markdownH1Size: 24, markdownH2Size: 22, markdownH3Size: 19, paragraphGap: 21 },
  { label: '稍小', bodySize: 17.25, lineHeight: 2, toolbarTitleSize: 24, questionSize: 22, blockTitleSize: 28, markdownH1Size: 25, markdownH2Size: 23, markdownH3Size: 20, paragraphGap: 22 },
  { label: '标准', bodySize: 18, lineHeight: 1.96, toolbarTitleSize: 25, questionSize: 24, blockTitleSize: 30, markdownH1Size: 27, markdownH2Size: 25, markdownH3Size: 22, paragraphGap: 23 },
  { label: '稍大', bodySize: 19, lineHeight: 1.92, toolbarTitleSize: 26, questionSize: 25, blockTitleSize: 32, markdownH1Size: 29, markdownH2Size: 27, markdownH3Size: 24, paragraphGap: 24 },
  { label: '较大', bodySize: 20.5, lineHeight: 1.88, toolbarTitleSize: 28, questionSize: 27, blockTitleSize: 34, markdownH1Size: 31, markdownH2Size: 29, markdownH3Size: 26, paragraphGap: 25 },
  { label: '特大', bodySize: 22, lineHeight: 1.84, toolbarTitleSize: 30, questionSize: 29, blockTitleSize: 37, markdownH1Size: 34, markdownH2Size: 31, markdownH3Size: 28, paragraphGap: 26 },
];
const defaultWorkspaceLayoutRatios: WorkspaceLayoutRatios = {
  threeDirectory: workspacePanelSizing.directory.defaultWidth / workspaceRatioMigrationReferenceWidth,
  threeQa: workspacePanelSizing.qa.defaultWidth / workspaceRatioMigrationReferenceWidth,
  directoryOnly: workspacePanelSizing.directory.defaultWidth / workspaceRatioMigrationReferenceWidth,
  qaOnly: workspacePanelSizing.qa.defaultWidth / workspaceRatioMigrationReferenceWidth,
};
const legacyWorkspacePanelStorageKeys: Record<WorkspacePanel, string> = {
  directory: 'slow.learning-workspace.directory-width',
  qa: 'slow.learning-workspace.qa-width',
};

function legacyUserWorkspacePanelStorageKey(userId: string, panel: WorkspacePanel) {
  return `slow.learning-workspace.${userId}.${panel}-width`;
}

function workspaceLayoutRatiosStorageKey(userId: string) {
  return `slow.learning-workspace.${userId}.layout-ratios`;
}

function readerTypographyStorageKey(userId: string) {
  return `slow.reader.${userId}.typography-step`;
}

function readReaderTypographyStep(userId: string) {
  try {
    const storedValue = window.localStorage.getItem(readerTypographyStorageKey(userId));
    if (storedValue === null) return defaultReaderTypographyStep;
    const stored = Number(storedValue);
    return Number.isInteger(stored) && stored >= 0 && stored < readerTypographyPresets.length
      ? stored
      : defaultReaderTypographyStep;
  } catch {
    return defaultReaderTypographyStep;
  }
}

function persistReaderTypographyStep(userId: string, step: number) {
  try {
    window.localStorage.setItem(readerTypographyStorageKey(userId), String(step));
  } catch {
    // The current reading session can still change size when storage is unavailable.
  }
}

function clampWorkspacePanelWidth(panel: WorkspacePanel, width: number, availableWidth = Number.POSITIVE_INFINITY) {
  const sizing = workspacePanelSizing[panel];
  return Math.min(Math.max(sizing.minWidth, width), sizing.maxWidth, Math.max(sizing.minWidth, availableWidth));
}

function validWorkspaceLayoutRatio(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 && value < 1;
}

function persistWorkspaceLayoutRatios(userId: string, ratios: WorkspaceLayoutRatios) {
  try {
    window.localStorage.setItem(
      workspaceLayoutRatiosStorageKey(userId),
      JSON.stringify({ version: 1, ...ratios }),
    );
  } catch {
    // Reading remains fully usable if browser storage is unavailable.
  }
}

function readWorkspaceLayoutRatios(userId: string): WorkspaceLayoutRatios {
  try {
    const stored = JSON.parse(window.localStorage.getItem(workspaceLayoutRatiosStorageKey(userId)) || 'null');
    if (
      stored
      && validWorkspaceLayoutRatio(stored.threeDirectory)
      && validWorkspaceLayoutRatio(stored.threeQa)
      && validWorkspaceLayoutRatio(stored.directoryOnly)
      && validWorkspaceLayoutRatio(stored.qaOnly)
    ) {
      return {
        threeDirectory: stored.threeDirectory,
        threeQa: stored.threeQa,
        directoryOnly: stored.directoryOnly,
        qaOnly: stored.qaOnly,
      };
    }

    const legacyWidth = (panel: WorkspacePanel) => {
      const scopedKey = legacyUserWorkspacePanelStorageKey(userId, panel);
      const scopedValue = window.localStorage.getItem(scopedKey);
      const globalKey = legacyWorkspacePanelStorageKeys[panel];
      const rawValue = scopedValue ?? window.localStorage.getItem(globalKey);
      const width = Number(rawValue);
      if (rawValue !== null) {
        window.localStorage.removeItem(scopedKey);
        window.localStorage.removeItem(globalKey);
      }
      return Number.isFinite(width) && width > 0 ? width : null;
    };
    const directoryWidth = legacyWidth('directory');
    const qaWidth = legacyWidth('qa');
    const ratios = {
      threeDirectory: directoryWidth
        ? directoryWidth / workspaceRatioMigrationReferenceWidth
        : defaultWorkspaceLayoutRatios.threeDirectory,
      threeQa: qaWidth
        ? qaWidth / workspaceRatioMigrationReferenceWidth
        : defaultWorkspaceLayoutRatios.threeQa,
      directoryOnly: directoryWidth
        ? directoryWidth / workspaceRatioMigrationReferenceWidth
        : defaultWorkspaceLayoutRatios.directoryOnly,
      qaOnly: qaWidth
        ? qaWidth / workspaceRatioMigrationReferenceWidth
        : defaultWorkspaceLayoutRatios.qaOnly,
    };
    persistWorkspaceLayoutRatios(userId, ratios);
    return ratios;
  } catch {
    return defaultWorkspaceLayoutRatios;
  }
}

function workspaceLayoutRatioKey(
  panel: WorkspacePanel,
  directoryHidden: boolean,
  qaHidden: boolean,
): WorkspaceLayoutRatioKey | null {
  if (!directoryHidden && !qaHidden) return panel === 'directory' ? 'threeDirectory' : 'threeQa';
  if (panel === 'directory' && !directoryHidden) return 'directoryOnly';
  if (panel === 'qa' && !qaHidden) return 'qaOnly';
  return null;
}

function fitVisibleWorkspacePanelWidths(
  workspaceWidth: number,
  directoryWidth: number,
  qaWidth: number,
  directoryHidden: boolean,
  qaHidden: boolean,
) {
  let nextDirectoryWidth = clampWorkspacePanelWidth('directory', directoryWidth);
  let nextQaWidth = clampWorkspacePanelWidth('qa', qaWidth);
  if (!directoryHidden && !qaHidden) {
    const availableForPanels = Math.max(
      workspacePanelSizing.directory.minWidth + workspacePanelSizing.qa.minWidth,
      workspaceWidth * (1 - workspaceReaderMinRatio),
    );
    let overflow = Math.max(0, nextDirectoryWidth + nextQaWidth - availableForPanels);
    const qaReduction = Math.min(overflow, nextQaWidth - workspacePanelSizing.qa.minWidth);
    nextQaWidth -= qaReduction;
    overflow -= qaReduction;
    nextDirectoryWidth -= Math.min(overflow, nextDirectoryWidth - workspacePanelSizing.directory.minWidth);
  } else if (!directoryHidden) {
    nextDirectoryWidth = clampWorkspacePanelWidth(
      'directory',
      nextDirectoryWidth,
      workspaceWidth * (1 - workspaceReaderMinRatio),
    );
  } else if (!qaHidden) {
    nextQaWidth = clampWorkspacePanelWidth(
      'qa',
      nextQaWidth,
      workspaceWidth * (1 - workspaceReaderMinRatio),
    );
  }
  return { directoryWidth: nextDirectoryWidth, qaWidth: nextQaWidth };
}

function resolveWorkspacePanelWidths(
  workspaceWidth: number,
  ratios: WorkspaceLayoutRatios,
  currentDirectoryWidth: number,
  currentQaWidth: number,
  directoryHidden: boolean,
  qaHidden: boolean,
) {
  let directoryWidth = currentDirectoryWidth;
  let qaWidth = currentQaWidth;
  if (!directoryHidden && !qaHidden) {
    directoryWidth = workspaceWidth * ratios.threeDirectory;
    qaWidth = workspaceWidth * ratios.threeQa;
  } else if (!directoryHidden) {
    directoryWidth = workspaceWidth * ratios.directoryOnly;
  } else if (!qaHidden) {
    qaWidth = workspaceWidth * ratios.qaOnly;
  }
  return fitVisibleWorkspacePanelWidths(
    workspaceWidth,
    directoryWidth,
    qaWidth,
    directoryHidden,
    qaHidden,
  );
}

function LearningWorkspace({
  userId,
  series,
  section,
  directoryBookId,
  dailyMode,
  onSelectSection,
  onGenerateSection,
  onRegenerateSection,
  onGenerateChapter,
  onActivateBook,
  onStartNextBook,
  chapterGenerationDisabled,
  generatingChapterId,
  onSectionChange,
  onRefreshSeries,
  onDeleteBook,
  onFeedbackBlock,
  onGlobalFeedback,
  onReturnHome,
  onQaVisibilityChange,
}: {
  userId: string;
  series: Series;
  section: Section | null;
  directoryBookId: string;
  dailyMode: DailyMode;
  onSelectSection: (id: string) => Promise<Section>;
  onGenerateSection: (id: string) => Promise<void>;
  onRegenerateSection: (id: string) => Promise<void>;
  onGenerateChapter: (chapter: Chapter) => Promise<void>;
  onActivateBook: (book: Book) => Promise<void>;
  onStartNextBook: (bookId: string) => Promise<void>;
  chapterGenerationDisabled: boolean;
  generatingChapterId: string;
  onSectionChange: (section: Section | null) => void;
  onRefreshSeries: () => Promise<void>;
  onDeleteBook: (bookId: string) => Promise<void>;
  onFeedbackBlock: (block: Block) => void;
  onGlobalFeedback: () => void;
  onReturnHome: () => void;
  onQaVisibilityChange: (open: boolean) => void;
}) {
  const [selectedBlockId, setSelectedBlockId] = useState('');
  const [selectedChapterId, setSelectedChapterId] = useState('');
  const [chapterLaunchAction, setChapterLaunchAction] = useState<ChapterLaunchAction | null>(null);
  const [selectedQuote, setSelectedQuote] = useState<TextQuote | null>(null);
  const [explanationRequest, setExplanationRequest] = useState<ExplanationRequest | null>(null);
  const [focusLayout, setFocusLayout] = useState(() => window.matchMedia(workspaceFocusMediaQuery).matches);
  const [directoryHidden, setDirectoryHidden] = useState(() => window.matchMedia(workspaceFocusMediaQuery).matches);
  const [qaHidden, setQaHidden] = useState(true);
  const [readerTab, setReaderTab] = useState<ReaderTab>('content');
  const [quizReviewVisible, setQuizReviewVisible] = useState(false);
  const [readerTypographyStep, setReaderTypographyStep] = useState(() => readReaderTypographyStep(userId));
  const [askAiStreaming, setAskAiStreaming] = useState(false);
  const [layoutRatios, setLayoutRatios] = useState(() => readWorkspaceLayoutRatios(userId));
  const [directoryWidth, setDirectoryWidth] = useState(() => clampWorkspacePanelWidth(
    'directory',
    window.innerWidth * layoutRatios.directoryOnly,
  ));
  const [qaWidth, setQaWidth] = useState(() => clampWorkspacePanelWidth(
    'qa',
    window.innerWidth * layoutRatios.qaOnly,
  ));
  const [resizingPanel, setResizingPanel] = useState<WorkspacePanel | null>(null);
  const workspaceRef = useRef<HTMLDivElement>(null);
  const panelLayoutRef = useRef({ directoryWidth, qaWidth, directoryHidden, qaHidden, layoutRatios });
  const resizeSessionRef = useRef<{
    panel: WorkspacePanel;
    pointerId: number;
    startClientX: number;
    startWidth: number;
    startedCollapsed: boolean;
    moved: boolean;
    latestWidth: number;
  } | null>(null);
  panelLayoutRef.current = { directoryWidth, qaWidth, directoryHidden, qaHidden, layoutRatios };

  // Answering and reviewing are deliberately different states. Ask AI remains
  // unavailable while a learner is choosing answers, but becomes available
  // again once the submitted result and its explanations are visible.
  const qaAvailable = readerTab !== 'quiz' || quizReviewVisible;
  const effectiveQaHidden = qaHidden || !qaAvailable;
  const studyActivityKind = !effectiveQaHidden
    ? 'ask_ai'
    : readerTab === 'quiz'
      ? 'verification_review'
      : 'reading_thinking';
  const studyActivity = useStudyActivity({
    sectionId: section?.content ? section.id : null,
    activityKind: studyActivityKind,
    keepActive: askAiStreaming,
  });

  useEffect(() => {
    onQaVisibilityChange(!effectiveQaHidden);
  }, [onQaVisibilityChange, effectiveQaHidden]);

  useEffect(() => () => onQaVisibilityChange(false), [onQaVisibilityChange]);

  useEffect(() => {
    setReaderTypographyStep(readReaderTypographyStep(userId));
  }, [userId]);

  useEffect(() => {
    const focusMedia = window.matchMedia(workspaceFocusMediaQuery);
    const adaptPanels = () => {
      const focus = focusMedia.matches;
      setFocusLayout(focus);
      if (focus) {
        setDirectoryHidden(true);
        setQaHidden(true);
      }
    };
    adaptPanels();
    focusMedia.addEventListener('change', adaptPanels);
    return () => {
      focusMedia.removeEventListener('change', adaptPanels);
    };
  }, []);

  useEffect(() => {
    const workspace = workspaceRef.current;
    if (!workspace || typeof ResizeObserver === 'undefined') return;
    const fitPanelWidths = (workspaceWidth: number) => {
      const current = panelLayoutRef.current;
      const { directoryWidth: nextDirectoryWidth, qaWidth: nextQaWidth } = resolveWorkspacePanelWidths(
        workspaceWidth,
        current.layoutRatios,
        current.directoryWidth,
        current.qaWidth,
        current.directoryHidden,
        current.qaHidden,
      );
      if (nextDirectoryWidth !== current.directoryWidth) setDirectoryWidth(nextDirectoryWidth);
      if (nextQaWidth !== current.qaWidth) setQaWidth(nextQaWidth);
    };
    const observer = new ResizeObserver(([entry]) => fitPanelWidths(entry.contentRect.width));
    observer.observe(workspace);
    fitPanelWidths(workspace.clientWidth);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const workspace = workspaceRef.current;
    if (!workspace) return;
    const fitted = resolveWorkspacePanelWidths(
      workspace.clientWidth,
      layoutRatios,
      directoryWidth,
      qaWidth,
      directoryHidden,
      qaHidden,
    );
    if (fitted.directoryWidth !== directoryWidth) setDirectoryWidth(fitted.directoryWidth);
    if (fitted.qaWidth !== qaWidth) setQaWidth(fitted.qaWidth);
  }, [directoryHidden, qaHidden]);

  useEffect(() => {
    setSelectedBlockId(section?.content?.blocks[0]?.id || '');
    setSelectedQuote(null);
    setExplanationRequest(null);
    setQuizReviewVisible(false);
    if (section?.id) setChapterLaunchAction(null);
  }, [section?.id, section?.content?.id]);

  const location = useMemo(() => findSectionLocation(series, section?.id), [series, section?.id]);
  const routeChapters = useMemo(
    () => series.books.flatMap((book) => book.chapters),
    [series],
  );
  const selectedChapter = routeChapters.find((chapter) => chapter.id === selectedChapterId) || null;
  useEffect(() => {
    if (location?.chapter.id) {
      setSelectedChapterId(location.chapter.id);
      return;
    }
    if (selectedChapter && selectedChapter.status !== 'locked') return;
    const next = routeChapters.find((chapter) => chapter.status === 'available')
      || routeChapters.find((chapter) => chapter.status === 'skipped')
      || null;
    setSelectedChapterId(next?.id || '');
  }, [location?.chapter.id, selectedChapter?.id, selectedChapter?.status, routeChapters]);
  const selectChapter = async (chapter: Chapter) => {
    if (chapter.status === 'locked') return;
    setSelectedChapterId(chapter.id);
    setChapterLaunchAction(null);
    if (focusLayout) setDirectoryHidden(true);
    if (chapter.status === 'skipped') {
      await api.resumeChapter(chapter.id, `resume-${crypto.randomUUID()}`);
      await onRefreshSeries();
    }
    const first = chapter.sections.find(
      (item) => !['locked', 'completed'].includes(item.status),
    ) || chapter.sections.find((item) => item.status === 'completed');
    if (chapter.generated && first) {
      await onSelectSection(first.id);
      return;
    }
    await onGenerateChapter(chapter);
  };
  const openChapterAction = (chapter: Chapter, action: ChapterLaunchAction) => {
    if (chapter.status === 'locked' || chapter.status === 'completed') return;
    setSelectedChapterId(chapter.id);
    setChapterLaunchAction(action);
    onSectionChange(null);
    updateBrowserLocation(seriesPath(series.id), 'push');
    if (focusLayout) setDirectoryHidden(true);
  };
  const activeBlockId = selectedBlockId || section?.content?.blocks[0]?.id || '';
  const selectBlock = (blockId: string) => {
    setSelectedBlockId(blockId);
    setSelectedQuote(null);
  };
  const toggleDirectory = () => {
    if (focusLayout && directoryHidden) setQaHidden(true);
    setDirectoryHidden((hidden) => !hidden);
  };
  const toggleQa = () => {
    if (focusLayout && qaHidden) setDirectoryHidden(true);
    setQaHidden((hidden) => !hidden);
  };

  const setPanelWidth = (panel: WorkspacePanel, width: number) => {
    if (panel === 'directory') setDirectoryWidth(width);
    else setQaWidth(width);
  };
  const openPanel = (panel: WorkspacePanel) => {
    if (panel === 'directory') {
      if (focusLayout) setQaHidden(true);
      setDirectoryHidden(false);
    } else {
      if (focusLayout) setDirectoryHidden(true);
      setQaHidden(false);
    }
  };
  const panelAvailableWidth = (panel: WorkspacePanel, startedCollapsed = false) => {
    const workspaceWidth = workspaceRef.current?.clientWidth || window.innerWidth;
    const otherPanelWillClose = startedCollapsed && focusLayout;
    const otherWidth = panel === 'directory'
      ? (!qaHidden && !otherPanelWillClose ? qaWidth : 0)
      : (!directoryHidden && !otherPanelWillClose ? directoryWidth : 0);
    return workspaceWidth * (1 - workspaceReaderMinRatio) - otherWidth;
  };
  const persistPanelRatio = (
    panel: WorkspacePanel,
    ratio: number,
    currentDirectoryHidden = directoryHidden,
    currentQaHidden = qaHidden,
  ) => {
    const key = workspaceLayoutRatioKey(panel, currentDirectoryHidden, currentQaHidden);
    if (!key) return;
    setLayoutRatios((current) => {
      const next = { ...current, [key]: ratio };
      persistWorkspaceLayoutRatios(userId, next);
      return next;
    });
  };
  const rememberPanelWidth = (
    panel: WorkspacePanel,
    width: number,
    currentDirectoryHidden = directoryHidden,
    currentQaHidden = qaHidden,
  ) => {
    const workspaceWidth = workspaceRef.current?.clientWidth || window.innerWidth;
    persistPanelRatio(panel, width / workspaceWidth, currentDirectoryHidden, currentQaHidden);
  };
  const beginPanelResize = (panel: WorkspacePanel, event: ReactPointerEvent<HTMLDivElement>) => {
    if (focusLayout || (event.pointerType === 'mouse' && event.button !== 0)) return;
    const startedCollapsed = panel === 'directory' ? directoryHidden : qaHidden;
    const startWidth = panel === 'directory' ? directoryWidth : qaWidth;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    resizeSessionRef.current = {
      panel,
      pointerId: event.pointerId,
      startClientX: event.clientX,
      startWidth,
      startedCollapsed,
      moved: false,
      latestWidth: startWidth,
    };
    setResizingPanel(panel);
    if (startedCollapsed) openPanel(panel);
  };
  const movePanelResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    const session = resizeSessionRef.current;
    const workspace = workspaceRef.current;
    if (!session || !workspace || session.pointerId !== event.pointerId) return;
    const pointerTravel = event.clientX - session.startClientX;
    if (!session.moved && Math.abs(pointerTravel) < 3) return;
    session.moved = true;
    const bounds = workspace.getBoundingClientRect();
    const proposedWidth = session.startedCollapsed
      ? session.panel === 'directory'
        ? event.clientX - bounds.left
        : bounds.right - event.clientX
      : session.startWidth + (session.panel === 'directory' ? pointerTravel : -pointerTravel);
    const nextWidth = clampWorkspacePanelWidth(
      session.panel,
      proposedWidth,
      panelAvailableWidth(session.panel, session.startedCollapsed),
    );
    session.latestWidth = nextWidth;
    setPanelWidth(session.panel, nextWidth);
  };
  const finishPanelResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    const session = resizeSessionRef.current;
    if (!session || session.pointerId !== event.pointerId) return;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    if (session.moved) {
      const current = panelLayoutRef.current;
      rememberPanelWidth(
        session.panel,
        session.latestWidth,
        current.directoryHidden,
        current.qaHidden,
      );
    }
    resizeSessionRef.current = null;
    setResizingPanel(null);
  };
  const resetPanelWidth = (panel: WorkspacePanel) => {
    const key = workspaceLayoutRatioKey(panel, directoryHidden, qaHidden);
    if (!key) return;
    const defaultRatio = defaultWorkspaceLayoutRatios[key];
    const workspaceWidth = workspaceRef.current?.clientWidth || window.innerWidth;
    const defaultWidth = clampWorkspacePanelWidth(
      panel,
      workspaceWidth * defaultRatio,
      panelAvailableWidth(panel),
    );
    setPanelWidth(panel, defaultWidth);
    persistPanelRatio(panel, defaultRatio);
  };
  const handlePanelKeyDown = (panel: WorkspacePanel, event: ReactKeyboardEvent<HTMLDivElement>) => {
    const hidden = panel === 'directory' ? directoryHidden : qaHidden;
    if (hidden && (event.key === 'Enter' || event.key === ' ')) {
      event.preventDefault();
      openPanel(panel);
      return;
    }
    if (hidden) return;
    const currentWidth = panel === 'directory' ? directoryWidth : qaWidth;
    let nextWidth = currentWidth;
    const step = event.shiftKey ? 32 : 12;
    if (event.key === 'Home') nextWidth = workspacePanelSizing[panel].minWidth;
    else if (event.key === 'End') nextWidth = panelAvailableWidth(panel);
    else if (event.key === 'ArrowLeft') nextWidth += panel === 'directory' ? -step : step;
    else if (event.key === 'ArrowRight') nextWidth += panel === 'directory' ? step : -step;
    else return;
    event.preventDefault();
    nextWidth = clampWorkspacePanelWidth(panel, nextWidth, panelAvailableWidth(panel));
    setPanelWidth(panel, nextWidth);
    rememberPanelWidth(panel, nextWidth);
  };
  const workspaceStyle = {
    '--directory-width': `${directoryWidth}px`,
    '--qa-width': `${qaWidth}px`,
    '--reader-body-size': `${readerTypographyPresets[readerTypographyStep].bodySize}px`,
    '--reader-body-line-height': readerTypographyPresets[readerTypographyStep].lineHeight,
    '--reader-toolbar-title-size': `${readerTypographyPresets[readerTypographyStep].toolbarTitleSize}px`,
    '--reader-question-size': `${readerTypographyPresets[readerTypographyStep].questionSize}px`,
    '--reader-block-title-size': `${readerTypographyPresets[readerTypographyStep].blockTitleSize}px`,
    '--reader-markdown-h1-size': `${readerTypographyPresets[readerTypographyStep].markdownH1Size}px`,
    '--reader-markdown-h2-size': `${readerTypographyPresets[readerTypographyStep].markdownH2Size}px`,
    '--reader-markdown-h3-size': `${readerTypographyPresets[readerTypographyStep].markdownH3Size}px`,
    '--reader-paragraph-gap': `${readerTypographyPresets[readerTypographyStep].paragraphGap}px`,
  } as CSSProperties;
  const changeReaderTypographyStep = (step: number) => {
    const nextStep = Math.min(Math.max(0, step), readerTypographyPresets.length - 1);
    setReaderTypographyStep(nextStep);
    persistReaderTypographyStep(userId, nextStep);
  };

  return (
    <div
      ref={workspaceRef}
      style={workspaceStyle}
      className={`learning-workspace mode-${dailyMode} ${focusLayout ? 'focus-layout' : ''} ${directoryHidden ? 'directory-collapsed' : ''} ${effectiveQaHidden ? 'qa-collapsed' : ''} ${qaAvailable ? '' : 'assessment-focus'} ${resizingPanel ? 'is-resizing' : ''}`}
    >
      {focusLayout && (!directoryHidden || !effectiveQaHidden) && (
        <button
          className="panel-backdrop"
          aria-label="关闭侧栏"
          onClick={() => {
            setDirectoryHidden(true);
            setQaHidden(true);
          }}
        />
      )}
      <DirectoryPanel
        series={series}
        directoryBookId={directoryBookId}
        hidden={directoryHidden}
        onClose={() => setDirectoryHidden(true)}
        currentSectionId={section?.id}
        currentChapterId={selectedChapter?.id}
        onSelectSection={onSelectSection}
        onSelectChapter={selectChapter}
        onChallengeChapter={(chapter) => openChapterAction(chapter, 'challenge')}
        onSkipChapter={(chapter) => openChapterAction(chapter, 'skip')}
        onStartNextBook={onStartNextBook}
        onActivateBook={onActivateBook}
        chapterGenerationDisabled={chapterGenerationDisabled}
        generatingChapterId={generatingChapterId}
        onRefreshSeries={onRefreshSeries}
        onDeleteBook={onDeleteBook}
      />
      <ReaderPanel
        series={series}
        section={section}
        chapter={selectedChapter}
        chapterAction={chapterLaunchAction}
        dailyMode={dailyMode}
        studySessionSeconds={studyActivity.sessionSeconds}
        studyPaused={studyActivity.paused}
        onResumeStudy={studyActivity.resume}
        directoryHidden={directoryHidden}
        qaHidden={effectiveQaHidden}
        qaAvailable={qaAvailable}
        readerTypographyStep={readerTypographyStep}
        readerTypographyLabel={readerTypographyPresets[readerTypographyStep].label}
        readerTypographyBodySize={readerTypographyPresets[readerTypographyStep].bodySize}
        readerTypographyStepCount={readerTypographyPresets.length}
        onReaderTypographyStepChange={changeReaderTypographyStep}
        onToggleDirectory={toggleDirectory}
        onToggleQa={toggleQa}
        onOpenQa={() => openPanel('qa')}
        onTabChange={(nextTab) => {
          setReaderTab(nextTab);
          if (nextTab === 'quiz') {
            setQaHidden(true);
          } else {
            setQuizReviewVisible(false);
          }
        }}
        onQuizReviewVisibilityChange={setQuizReviewVisible}
        location={location}
        selectedBlockId={activeBlockId}
        onSelectBlock={selectBlock}
        onQuote={(quote) => {
          setSelectedBlockId(quote.blockId);
          setSelectedQuote(quote);
          if (focusLayout) setDirectoryHidden(true);
          setQaHidden(false);
        }}
        onGenerate={() => section && onGenerateSection(section.id)}
        onRegenerate={() => (section ? onRegenerateSection(section.id) : Promise.resolve())}
        onSelectSection={onSelectSection}
        onSelectChapter={(chapterId) => {
          const target = routeChapters.find((chapter) => chapter.id === chapterId);
          if (target) void selectChapter(target);
        }}
        onCloseChapterAction={() => {
          setChapterLaunchAction(null);
          setDirectoryHidden(false);
        }}
        onSectionChange={onSectionChange}
        onRefreshSeries={onRefreshSeries}
        onFeedbackBlock={onFeedbackBlock}
        onGlobalFeedback={onGlobalFeedback}
        onReturnHome={onReturnHome}
        onRestorePersonalPresentation={async (block) => {
          if (!section?.content || !block.personalPresentation) return;
          await api.restorePersonalPresentation(section.id, block.id, section.content.id);
          onSectionChange(await api.section(section.id));
        }}
        onExplainBlock={async (block, style, customInstruction) => {
          const option = style === 'custom' ? null : EXPLANATION_STYLE_OPTIONS[style];
          const instruction = customInstruction?.trim();
          const question = style === 'custom'
            ? (instruction ? `请按这个要求重新解释当前段落：${instruction}` : '')
            : option?.prompt;
          if (!question) return;
          const blockKind = ['text', 'bullet_list', 'ordered_steps', 'diagram', 'table', 'code', 'formula'].includes(block.kind)
            ? block.kind
            : 'text';
          setSelectedBlockId(block.id);
          setSelectedQuote(null);
          const requestId = crypto.randomUUID();
          let evidenceEventId: string | undefined;
          let preferenceStatus: ExplanationRequest['preferenceStatus'] = 'unsaved';
          if (section?.content) {
            try {
              await api.recordPreferenceEvidence({
                eventId: requestId,
                sectionId: section.id,
                contentVersionId: section.content.id,
                blockId: block.id,
                blockKind,
                style,
                signal: 'requested',
                customInstruction: style === 'custom' ? instruction : undefined,
              });
              evidenceEventId = requestId;
              preferenceStatus = 'saved';
            } catch {
              // Ask AI remains available; the panel exposes retryable unsaved state.
            }
          }
          setExplanationRequest({
            requestId,
            blockId: block.id,
            blockKind,
            style,
            label: option?.label || '我的讲法',
            question,
            displayQuestion: style === 'custom' ? `按这个讲：${instruction}` : option?.label || '我的讲法',
            evidenceEventId,
            preferenceStatus,
            customInstruction: style === 'custom' ? instruction : undefined,
          });
          if (focusLayout) setDirectoryHidden(true);
          setQaHidden(false);
          telemetry.track('explanation_style_requested', {
            view: 'learn',
            entityType: 'section',
            entityId: section?.id || '',
            properties: { style, blockKind },
          });
        }}
      />
      <QaPanel
        key={section?.content ? `${section.id}:${section.content.id}` : section?.id || 'empty'}
        section={section}
        dailyMode={dailyMode}
        hidden={effectiveQaHidden}
        onClose={() => setQaHidden(true)}
        selectedBlockId={activeBlockId}
        selectedQuote={selectedQuote}
        onAnchor={selectBlock}
        onClearQuote={() => setSelectedQuote(null)}
        explanationRequest={explanationRequest}
        onSectionChange={onSectionChange}
        onStreamingChange={setAskAiStreaming}
      />
      {(['directory', ...(qaAvailable ? ['qa' as const] : [])] as const).map((panel) => {
        const hidden = panel === 'directory' ? directoryHidden : qaHidden;
        const width = panel === 'directory' ? directoryWidth : qaWidth;
        const sizing = workspacePanelSizing[panel];
        const panelName = panel === 'directory' ? '目录' : '答疑';
        const maximumWidth = Math.max(sizing.minWidth, Math.min(sizing.maxWidth, panelAvailableWidth(panel)));
        return (
          <div
            key={panel}
            className={`workspace-resize-handle ${panel}-resize-handle ${hidden ? 'is-collapsed' : ''}`}
            role="separator"
            aria-label={hidden ? `展开${panelName}` : `调整${panelName}宽度`}
            aria-orientation="vertical"
            aria-valuemin={sizing.minWidth}
            aria-valuemax={Math.round(maximumWidth)}
            aria-valuenow={Math.round(width)}
            aria-expanded={!hidden}
            tabIndex={0}
            title={hidden ? `点击或向内拖动展开${panelName}` : `拖动调整${panelName}宽度，双击恢复默认`}
            onPointerDown={(event) => beginPanelResize(panel, event)}
            onPointerMove={movePanelResize}
            onPointerUp={finishPanelResize}
            onPointerCancel={finishPanelResize}
            onLostPointerCapture={finishPanelResize}
            onDoubleClick={() => resetPanelWidth(panel)}
            onKeyDown={(event) => handlePanelKeyDown(panel, event)}
          >
            <span aria-hidden="true">{panel === 'directory' ? '›' : '‹'}</span>
          </div>
        );
      })}
    </div>
  );
}

function findSectionLocation(series: Series, sectionId?: string) {
  if (!sectionId) return null;
  for (const book of series.books) {
    for (const chapter of book.chapters) {
      const section = chapter.sections.find((item) => item.id === sectionId);
      if (section) return { book, chapter, section };
    }
  }
  return null;
}

function LockIcon({ size = 14 }: { size?: number }) {
  return (
    <svg
      aria-hidden="true"
      className="lock-icon"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
    >
      <rect x="5" y="10" width="14" height="10" rx="3" stroke="currentColor" strokeWidth="1.8" />
      <path d="M8.5 10V7.5a3.5 3.5 0 0 1 7 0V10" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}

function ChevronIcon() {
  return (
    <svg aria-hidden="true" className="chevron-icon" width="13" height="13" viewBox="0 0 20 20" fill="none">
      <path d="m5 7.5 5 5 5-5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function GenerateIcon() {
  return (
    <svg aria-hidden="true" width="14" height="14" viewBox="0 0 20 20" fill="none">
      <path d="M10 3v14M3 10h14" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
    </svg>
  );
}

function DirectoryPanel({
  series,
  directoryBookId,
  hidden,
  onClose,
  currentSectionId,
  currentChapterId,
  onSelectSection,
  onSelectChapter,
  onChallengeChapter,
  onSkipChapter,
  onStartNextBook,
  onActivateBook,
  chapterGenerationDisabled,
  generatingChapterId,
  onRefreshSeries,
  onDeleteBook,
}: {
  series: Series;
  directoryBookId: string;
  hidden: boolean;
  onClose: () => void;
  currentSectionId?: string;
  currentChapterId?: string;
  onSelectSection: (id: string) => Promise<Section>;
  onSelectChapter: (chapter: Chapter) => void;
  onChallengeChapter: (chapter: Chapter) => void;
  onSkipChapter: (chapter: Chapter) => void;
  onStartNextBook: (bookId: string) => Promise<void>;
  onActivateBook: (book: Book) => Promise<void>;
  chapterGenerationDisabled: boolean;
  generatingChapterId: string;
  onRefreshSeries: () => Promise<void>;
  onDeleteBook: (bookId: string) => Promise<void>;
}) {
  const [deleteTarget, setDeleteTarget] = useState<Book | null>(null);
  const [deleting, setDeleting] = useState(false);
  const activeBookIndex = series.books.findIndex((book) => book.chapters.some(
    (chapter) => chapter.sections.some((item) => item.id === currentSectionId),
  ));
  const focusedBookIndex = series.books.findIndex((book) => book.id === directoryBookId);
  const resolvedBookIndex = focusedBookIndex >= 0
    ? focusedBookIndex
    : activeBookIndex >= 0
      ? activeBookIndex
      : Math.max(0, series.books.findIndex((book) => book.status !== 'locked'));
  const activeBook = series.books[resolvedBookIndex];
  const nextBook = activeBook?.status === 'completed'
    ? series.books[resolvedBookIndex + 1]
    : null;
  const [settlementTarget, setSettlementTarget] = useState<Book | null>(null);
  const [settlement, setSettlement] = useState<BookSettlement | null>(null);
  const [settlementLoading, setSettlementLoading] = useState(false);
  const [settlementError, setSettlementError] = useState('');
  const deleteDialogRef = useModalFocus<HTMLElement>({
    open: Boolean(deleteTarget),
    canClose: !deleting,
    onRequestClose: () => setDeleteTarget(null),
  });
  const settlementDialogRef = useModalFocus<HTMLElement>({
    open: Boolean(settlementTarget),
    onRequestClose: () => setSettlementTarget(null),
  });
  const openSettlement = async (book: Book) => {
    setSettlementTarget(book);
    setSettlement(null);
    setSettlementError('');
    setSettlementLoading(true);
    try {
      setSettlement(await api.settleBook(book.id));
      await onRefreshSeries();
    } catch (reason) {
      setSettlementError(reason instanceof Error ? reason.message : '全书结算暂时不可用');
    } finally {
      setSettlementLoading(false);
    }
  };

  return (
    <aside className="directory-panel" id="course-directory-panel" aria-label="课程目录" hidden={hidden}>
      <div className="directory-heading">
        <button className="panel-collapse-button" aria-label="收起目录" onClick={onClose}>收起</button>
        <span className="panel-label">当前书目录</span>
        <small className="directory-series-name">{series.title}</small>
        <h2>{activeBook?.title || '这本书'}</h2>
        <div className="series-progress">
          <span><i style={{ width: `${activeBook?.progress || 0}%` }} /></span>
          <b>路线 {activeBook?.progress || 0}%</b>
        </div>
      </div>
      {nextBook && nextBook.status !== 'locked' && (
          <div className="next-book-callout" role="status">
            <b>{activeBook.chapters.some((chapter) => chapter.status === 'skipped')
              ? `第 ${activeBook.position} 册路线已走完`
              : `第 ${activeBook.position} 册已完成`}</b>
            <span>第 {nextBook.position} 册《{nextBook.title}》已经解锁。</span>
            <button className="secondary-button" onClick={() => void onStartNextBook(nextBook.id)}>
              开始第 {nextBook.position} 册
            </button>
          </div>
      )}
      <nav className="book-tree">
        {activeBook && (
          <BookTree
            key={activeBook.id}
            book={activeBook}
            currentSectionId={currentSectionId}
            currentChapterId={currentChapterId}
            onSelectSection={onSelectSection}
            onSelectChapter={onSelectChapter}
            onChallengeChapter={onChallengeChapter}
            onSkipChapter={onSkipChapter}
            canActivate={
              activeBook.outlineStatus === 'draft'
              && (resolvedBookIndex === 0 || series.books[resolvedBookIndex - 1].status === 'completed')
            }
            onActivate={onActivateBook}
            chapterGenerationDisabled={chapterGenerationDisabled}
            generatingChapterId={generatingChapterId}
            onOpenSettlement={openSettlement}
            onRequestDelete={setDeleteTarget}
          />
        )}
      </nav>
      {deleteTarget && (
        <div
          className="confirm-backdrop"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget && !deleting) setDeleteTarget(null);
          }}
        >
          <section
            ref={deleteDialogRef}
            className="delete-confirm"
            role="dialog"
            aria-modal="true"
            aria-labelledby="delete-book-title"
            aria-describedby="delete-book-description"
            tabIndex={-1}
          >
            <span className="delete-confirm-icon"><TrashIcon size={20} /></span>
            <p className="eyebrow">删除书籍</p>
            <h2 id="delete-book-title">{deleteTarget.title}</h2>
            <p id="delete-book-description">
              书籍及其章节会从学习入口中移除，已有学习记录会保留。
              {series.books.length === 1 ? '这是系列中的最后一本书，删除后该系列也会从书架隐藏。' : ''}
              当前界面暂不支持恢复。
            </p>
            <div>
              <button data-dialog-initial-focus className="quiet-button" disabled={deleting} onClick={() => setDeleteTarget(null)}>取消</button>
              <button
                className="danger-button"
                disabled={deleting}
                onClick={async () => {
                  setDeleting(true);
                  try {
                    await onDeleteBook(deleteTarget.id);
                    setDeleteTarget(null);
                  } catch {
                    // App-level error presentation already explains the failure.
                  } finally {
                    setDeleting(false);
                  }
                }}
              >
                {deleting ? '正在删除…' : '确认删除'}
              </button>
            </div>
          </section>
        </div>
      )}
      {settlementTarget && (
        <div
          className="confirm-backdrop"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) setSettlementTarget(null);
          }}
        >
          <section
            ref={settlementDialogRef}
            className="book-settlement-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="book-settlement-title"
            tabIndex={-1}
          >
            <header>
              <p className="eyebrow">全书结算</p>
              <h2 id="book-settlement-title">{settlementTarget.title}</h2>
              <p>结算只汇总已经写入的学习与验证记录，不需要额外上传成果。</p>
            </header>
            {settlementLoading ? (
              <div className="book-settlement-loading" role="status">正在汇总全书学习记录…</div>
            ) : settlement ? (
              <>
                <div className="book-settlement-result">
                  <div><strong>{settlement.completedChapterCount}/{settlement.chapterCount}</strong><span>完成章节</span></div>
                  <div><strong>{settlement.completedSectionCount}/{settlement.sectionCount}</strong><span>完成小节</span></div>
                  <div><strong>{settlement.verificationRate === null ? '—' : `${settlement.verificationRate}%`}</strong><span>验证最佳成绩</span></div>
                  <div><strong>{settlement.perfectSectionCount}</strong><span>满分小节</span></div>
                </div>
                <div className="book-settlement-followup">
                  <b>{settlement.reviewSectionCount > 0
                    ? `${settlement.reviewSectionCount} 节验证未满分，可继续重点巩固`
                    : '本书验证记录已完整结算'}</b>
                  <p>后续复习仍以真实测验、口试和实际复习安排为准；结算不会把浏览或上传文件算作掌握。</p>
                </div>
              </>
            ) : (
              <div className="book-settlement-error" role="alert">
                <b>暂时无法完成结算</b>
                <p>{settlementError}</p>
                <button className="secondary-button" onClick={() => void openSettlement(settlementTarget)}>重新结算</button>
              </div>
            )}
            <footer>
              <button data-dialog-initial-focus className="primary-button" onClick={() => setSettlementTarget(null)}>完成</button>
            </footer>
          </section>
        </div>
      )}
    </aside>
  );
}

function BookTree({
  book,
  currentSectionId,
  currentChapterId,
  onSelectSection,
  onSelectChapter,
  onChallengeChapter,
  onSkipChapter,
  canActivate,
  onActivate,
  chapterGenerationDisabled,
  generatingChapterId,
  onOpenSettlement,
  onRequestDelete,
}: {
  book: Book;
  currentSectionId?: string;
  currentChapterId?: string;
  onSelectSection: (id: string) => Promise<Section>;
  onSelectChapter: (chapter: Chapter) => void;
  onChallengeChapter: (chapter: Chapter) => void;
  onSkipChapter: (chapter: Chapter) => void;
  canActivate: boolean;
  onActivate: (book: Book) => Promise<void>;
  chapterGenerationDisabled: boolean;
  generatingChapterId: string;
  onOpenSettlement: (book: Book) => Promise<void>;
  onRequestDelete: (book: Book) => void;
}) {
  const containsCurrent = book.chapters.some((chapter) => chapter.sections.some((item) => item.id === currentSectionId));
  const canExpand = book.status !== 'locked' || canActivate;
  return (
    <details
      className={`book-node ${canExpand ? '' : 'is-unavailable'}`}
      open={canExpand && (containsCurrent || book.status !== 'locked')}
    >
      <button
        className="book-delete-button"
        aria-label={`删除书籍 ${book.title}`}
        title="删除书籍"
        onClick={() => onRequestDelete(book)}
      >
        <TrashIcon size={14} />
      </button>
      <summary
        aria-disabled={!canExpand}
        onClick={(event) => {
          if (!canExpand) event.preventDefault();
        }}
      >
        <span className="book-number">书 {book.position}</span>
        <span>
          <b>{book.title}</b>
          <small>
            {!canExpand
              ? '未解锁'
              : book.outlineStatus === 'draft'
              ? '待确认'
              : book.status === 'completed'
                ? book.chapters.some((chapter) => chapter.status === 'skipped')
                  ? '路线已走完'
                  : '已完成'
                : book.status === 'locked'
                  ? '未解锁'
                  : '已解锁'}
            {' · '}{book.progress}% · {Math.round(book.estimatedMinutes / 60)} 小时
          </small>
        </span>
        <i>{book.status === 'locked' ? <LockIcon /> : <ChevronIcon />}</i>
      </summary>
      {canExpand && book.outlineStatus === 'draft' && (
        <div className="book-outline-callout" role="status">
          <span>
            <b>{canActivate ? '下一本书可以开始准备' : '下一本书将在完成前一册后调整'}</b>
            <small>
              {canActivate
                ? '会根据你最近的学习表现调整章节；确认后即可开始。'
                : '完成上一本书后，会根据你的学习情况调整章节。'}
            </small>
          </span>
          {canActivate && (
            <button className="secondary-button" onClick={() => onActivate(book)}>
              查看并确认章节
            </button>
          )}
        </div>
      )}
      {canExpand && <div className="chapter-tree">
        {book.chapters.map((chapter) => {
          const chapterLocked = chapter.status === 'locked';
          const chapterBusy = generatingChapterId === chapter.id;
          return (
            <div className="chapter-node" key={chapter.id}>
              {chapter.generated || chapterLocked ? (
                <button
                  type="button"
                  className={`chapter-title chapter-select ${chapterLocked ? 'locked' : ''} ${currentChapterId === chapter.id && !currentSectionId ? 'active' : ''} ${chapter.status}`}
                  disabled={chapterLocked}
                  aria-label={chapterLocked
                    ? `第 ${chapter.position} 章 ${chapter.title}，未解锁`
                    : `学习第 ${chapter.position} 章 ${chapter.title}`}
                  onClick={() => onSelectChapter(chapter)}
                >
                  <span>第 {chapter.position} 章</span>
                  <b>{chapter.title}</b>
                  {chapterLocked && <LockIcon size={13} />}
                  {chapter.status === 'skipped' && <small>暂时继续</small>}
                </button>
              ) : (
                <button
                  className={`chapter-title chapter-entry chapter-select ${currentChapterId === chapter.id && !currentSectionId ? 'active' : ''}`}
                  aria-label={`学习第 ${chapter.position} 章 ${chapter.title}`}
                  disabled={chapterGenerationDisabled || chapterBusy}
                  onClick={() => onSelectChapter(chapter)}
                >
                  <span>第 {chapter.position} 章</span>
                  <b>{chapter.title}</b>
                  <i aria-hidden="true">→</i>
                </button>
              )}
            {!chapterLocked && chapter.status !== 'completed' && (
              <div className="chapter-route-actions" aria-label={`第 ${chapter.position} 章的其他学习方式`}>
                <button type="button" disabled={chapterBusy} onClick={() => onChallengeChapter(chapter)}>直接挑战</button>
                {chapter.status !== 'skipped' && (
                  <button type="button" disabled={chapterBusy} onClick={() => onSkipChapter(chapter)}>暂时略过</button>
                )}
              </div>
            )}
            {chapter.generated ? (
              <div className="section-tree">
                {chapter.workloadHint && chapter.workloadHint.level !== 'typical' && (
                  <small className={`chapter-workload-hint ${chapter.workloadHint.level}`}>
                    {chapter.workloadHint.message}
                  </small>
                )}
                {chapter.sections.map((item) => (
                  <SectionTreeButton
                    key={item.id}
                    item={item}
                    chapterPosition={chapter.position}
                    active={item.id === currentSectionId}
                    onClick={() => onSelectSection(item.id)}
                  />
                ))}
              </div>
            ) : null}
            {!chapter.generated && (
              <div className={`chapter-plan-placeholder ${chapterLocked ? 'locked' : 'ready'}`}>
                <span aria-hidden="true">
                  {chapterLocked ? <LockIcon size={10} /> : <GenerateIcon />}
                </span>
                <small>
                  {chapterLocked ? '完成上一章后解锁' : chapterBusy ? '正在准备本章…' : '点击章节标题开始学习'}
                </small>
              </div>
            )}
            </div>
          );
        })}
        <button
          className={`book-settlement-entry ${book.status === 'completed' ? 'enabled' : ''}`}
          disabled={book.status !== 'completed'}
          onClick={() => void onOpenSettlement(book)}
        >
          <span>{book.status === 'completed' ? '◆' : <LockIcon size={12} />}</span>
          <b>全书结算</b>
          <small>· {book.status !== 'completed'
            ? '完成全书后开启'
            : book.capstone?.status === 'completed'
              ? '查看总结'
              : '生成总结'}</small>
        </button>
      </div>}
    </details>
  );
}

function SectionTreeButton({
  item,
  chapterPosition,
  active,
  onClick,
}: {
  item: SectionSummary;
  chapterPosition: number;
  active: boolean;
  onClick: () => void;
}) {
  const preparing = item.status === 'preparing';
  const state = item.status === 'completed'
    ? '✓'
    : item.status === 'skipped'
      ? '↷'
    : item.status === 'locked'
      ? <LockIcon size={11} />
      : preparing
        ? '…'
        : '•';
  const sectionNumber = `${chapterPosition}.${item.position}`;
  return (
    <button
      className={`section-tree-button ${active ? 'active' : ''} ${item.status}`}
      disabled={item.status === 'locked' || item.status === 'skipped' || preparing}
      aria-label={`${sectionNumber} ${item.title}`}
      onClick={onClick}
    >
      <span>{state}</span>
      <small className="section-number">{sectionNumber}</small>
      <b>{item.title}{preparing ? ' · 准备中' : ''}</b>
    </button>
  );
}

function ChapterLaunchPanel({
  chapter,
  initialAction,
  onCancel,
  onSelectSection,
  onSelectChapter,
  onRefreshSeries,
}: {
  chapter: Chapter;
  initialAction: ChapterLaunchAction;
  onCancel: () => void;
  onSelectSection: (id: string) => Promise<Section>;
  onSelectChapter: (chapterId: string) => void;
  onRefreshSeries: () => Promise<void>;
}) {
  const [screen, setScreen] = useState<'preparing' | 'skip' | 'challenge' | 'result'>(
    initialAction === 'challenge' ? 'preparing' : 'skip',
  );
  const [challenge, setChallenge] = useState<ChapterChallenge | null>(null);
  const [result, setResult] = useState<ChapterChallengeResult | null>(null);
  const [answers, setAnswers] = useState<Record<string, number[][]>>({});
  const [busy, setBusy] = useState('');
  const [localError, setLocalError] = useState('');
  const preparedActionRef = useRef('');

  const ensureActive = async () => {
    if (chapter.status !== 'skipped') return;
    await api.resumeChapter(chapter.id, `resume-${crypto.randomUUID()}`);
    await onRefreshSeries();
  };
  const prepareChallenge = async () => {
    setScreen('preparing');
    setBusy('challenge');
    setLocalError('');
    try {
      await ensureActive();
      const value = await api.prepareChapterChallenge(chapter.id);
      setChallenge(value);
      setAnswers(Object.fromEntries(value.sections.map((section) => [
        section.sectionId,
        section.questions.map(() => []),
      ])));
      setScreen('challenge');
    } catch (reason) {
      setLocalError(reason instanceof Error ? reason.message : '章挑战暂时没有准备好');
    } finally {
      setBusy('');
    }
  };
  useEffect(() => {
    const actionKey = `${chapter.id}:${initialAction}`;
    if (preparedActionRef.current === actionKey) return;
    preparedActionRef.current = actionKey;
    setChallenge(null);
    setResult(null);
    setAnswers({});
    setBusy('');
    setLocalError('');
    if (initialAction === 'challenge') void prepareChallenge();
    else setScreen('skip');
  }, [chapter.id, initialAction]);
  const skipChapter = async (reason: 'not_focus' | 'defer_unknown' | 'challenge_exit') => {
    setBusy(`skip-${reason}`);
    setLocalError('');
    try {
      const route = await api.skipChapter(chapter.id, reason, `skip-${crypto.randomUUID()}`);
      await onRefreshSeries();
      if (route.nextChapterId) onSelectChapter(route.nextChapterId);
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : '暂时无法略过本章');
    } finally {
      setBusy('');
    }
  };
  const toggleAnswer = (
    sectionId: string,
    questionIndex: number,
    optionIndex: number,
    mode: 'single' | 'multiple',
  ) => {
    setAnswers((current) => {
      const sectionAnswers = (current[sectionId] || []).map((item) => [...item]);
      const selected = sectionAnswers[questionIndex] || [];
      sectionAnswers[questionIndex] = mode === 'single'
        ? [optionIndex]
        : selected.includes(optionIndex)
          ? selected.filter((item) => item !== optionIndex)
          : [...selected, optionIndex].sort((left, right) => left - right);
      return { ...current, [sectionId]: sectionAnswers };
    });
  };
  const allAnswered = challenge?.sections.every((section) => (
    answers[section.sectionId]?.length === section.questions.length
    && answers[section.sectionId].every((answer) => answer.length > 0)
  )) ?? false;
  const submitChallenge = async () => {
    if (!challenge || !allAnswered) return;
    setBusy('grading');
    setLocalError('');
    try {
      const graded = await api.submitChapterChallenge(
        chapter.id,
        challenge.sections.map((section) => ({
          sectionId: section.sectionId,
          quizSetId: section.quizSetId,
          answers: answers[section.sectionId],
        })),
        `challenge-${crypto.randomUUID()}`,
      );
      setResult(graded);
      setScreen('result');
      await onRefreshSeries();
    } catch (reason) {
      setLocalError(reason instanceof Error ? reason.message : '章挑战提交失败');
    } finally {
      setBusy('');
    }
  };

  if (screen === 'preparing') {
    return (
      <div className="chapter-launch-scroll chapter-route-pending">
        <button type="button" className="quiet-button" disabled={Boolean(busy)} onClick={onCancel}>← 返回目录</button>
        <span aria-hidden="true" />
        <p className="eyebrow">直接挑战</p>
        <h1>正在准备本章验证</h1>
        <p>会按小节出题，答对的部分直接形成掌握证据。</p>
        {localError && <p className="chapter-launch-error" role="alert">{localError}</p>}
        {localError && (
          <button type="button" className="secondary-button" disabled={Boolean(busy)} onClick={() => void prepareChallenge()}>重新准备</button>
        )}
      </div>
    );
  }

  if (screen === 'challenge' && challenge) {
    let questionNumber = 0;
    return (
      <div className="chapter-launch-scroll challenge-screen">
        <header className="chapter-challenge-heading">
          <button type="button" className="quiet-button" disabled={Boolean(busy)} onClick={onCancel}>← 返回目录</button>
          <p className="eyebrow">直接挑战 · {challenge.questionCount} 题</p>
          <h1>{challenge.chapterTitle}</h1>
          <p>每一组对应一个小节。答完后只留下真正薄弱的部分。</p>
        </header>
        <div className="chapter-challenge-sections">
          {challenge.sections.map((section) => (
            <section key={section.sectionId} className="chapter-challenge-section">
              <header>
                <span>{String(section.position).padStart(2, '0')}</span>
                <div><small>小节验证</small><h2>{section.title}</h2></div>
              </header>
              {section.questions.map((question, questionIndex) => {
                questionNumber += 1;
                const selected = answers[section.sectionId]?.[questionIndex] || [];
                return (
                  <fieldset key={`${section.sectionId}-${questionIndex}`} className="chapter-challenge-question">
                    <legend><span>{String(questionNumber).padStart(2, '0')}</span>{question.prompt}</legend>
                    {question.options.map((option, optionIndex) => (
                      <button
                        type="button"
                        key={option}
                        className={selected.includes(optionIndex) ? 'selected' : ''}
                        aria-pressed={selected.includes(optionIndex)}
                        onClick={() => toggleAnswer(
                          section.sectionId,
                          questionIndex,
                          optionIndex,
                          question.selectionMode,
                        )}
                      >
                        <span aria-hidden="true">{String.fromCharCode(65 + optionIndex)}</span>{option}
                      </button>
                    ))}
                  </fieldset>
                );
              })}
            </section>
          ))}
        </div>
        {localError && <p className="chapter-launch-error" role="alert">{localError}</p>}
        <footer className="chapter-challenge-submit">
          <span>{allAnswered ? '已经答完，可以查看薄弱小节' : '完成全部题目后提交'}</span>
          <button type="button" className="primary-button" disabled={!allAnswered || Boolean(busy)} onClick={() => void submitChallenge()}>
            {busy === 'grading' ? '正在判断…' : '提交挑战 →'}
          </button>
        </footer>
      </div>
    );
  }

  if (screen === 'result' && result) {
    const weakSections = result.sectionResults.filter((item) => item.status === 'needs_learning');
    return (
      <div className={`chapter-launch-scroll challenge-result ${result.passed ? 'passed' : 'partial'}`}>
        <header>
          <span className="challenge-result-mark" aria-hidden="true">{result.passed ? '✓' : weakSections.length}</span>
          <p className="eyebrow">挑战结果</p>
          <h1>{result.passed ? '这一章可以放心略过' : `重点只剩 ${weakSections.length} 个薄弱小节`}</h1>
          <p>{result.passed
            ? '本次答题已经形成掌握证据，本章按通过处理。'
            : '答对的小节已经记为完成；薄弱小节不会被算作掌握。'}</p>
        </header>
        <div className="challenge-result-sections">
          {result.sectionResults.map((item) => (
            <article key={item.sectionId} className={item.status}>
              <span>{item.status === 'passed' ? '✓' : '!'}</span>
              <div><small>第 {item.position} 节 · {item.score}/{item.total}</small><b>{item.title}</b></div>
              <em>{item.status === 'passed' ? '已通过' : '建议学习'}</em>
            </article>
          ))}
        </div>
        {localError && <p className="chapter-launch-error" role="alert">{localError}</p>}
        <div className="challenge-result-actions">
          {weakSections.length > 0 ? (
            <>
              <button type="button" className="primary-button" disabled={Boolean(busy)} onClick={() => void onSelectSection(weakSections[0].sectionId)}>
                学习薄弱小节 →
              </button>
              <button type="button" className="quiet-button" disabled={Boolean(busy)} onClick={() => void skipChapter('challenge_exit')}>
                {busy ? '正在继续…' : '暂时继续下一章'}
              </button>
              <small>继续不代表通过；后续依赖这里时会提醒回来补。</small>
            </>
          ) : (
            <button type="button" className="primary-button" onClick={() => result.nextChapterId ? onSelectChapter(result.nextChapterId) : onCancel()}>
              {result.nextChapterId ? '进入下一章 →' : '返回目录'}
            </button>
          )}
        </div>
      </div>
    );
  }

  if (screen === 'skip') {
    return (
      <div className="chapter-launch-scroll chapter-skip-screen">
        <header>
          <button type="button" className="quiet-button" disabled={Boolean(busy)} onClick={onCancel}>← 返回目录</button>
          <p className="eyebrow">暂时略过</p>
          <h1>为什么不学这一章？</h1>
          <p>原因不同，系统对你的学习画像也会不同处理。</p>
        </header>
        <div className="chapter-skip-reasons">
          <button type="button" disabled={Boolean(busy)} onClick={() => void skipChapter('not_focus')}>
            <span aria-hidden="true">◎</span>
            <small>不属于重点</small>
            <b>这不是我的目标</b>
            <p>只降低路线优先级，不判断你会或不会，也不改变知识段位。</p>
            <i aria-hidden="true">→</i>
          </button>
          <button type="button" disabled={Boolean(busy)} onClick={() => void skipChapter('defer_unknown')}>
            <span aria-hidden="true">↷</span>
            <small>以后再说</small>
            <b>我还不会，但现在先继续</b>
            <p>本章暂不计为掌握；后面真正依赖这里时，会提醒回来补。</p>
            <i aria-hidden="true">→</i>
          </button>
        </div>
        {localError && <p className="chapter-launch-error" role="alert">{localError}</p>}
        <aside className="chapter-skip-note">系列最终完成时，关键目标仍然需要有效证据；略过不是通过。</aside>
      </div>
    );
  }

  return null;
}

function ReaderPanel({
  series,
  section,
  chapter,
  chapterAction,
  dailyMode,
  studySessionSeconds,
  studyPaused,
  onResumeStudy,
  directoryHidden,
  qaHidden,
  qaAvailable,
  readerTypographyStep,
  readerTypographyLabel,
  readerTypographyBodySize,
  readerTypographyStepCount,
  onReaderTypographyStepChange,
  onToggleDirectory,
  onToggleQa,
  onOpenQa,
  onTabChange,
  onQuizReviewVisibilityChange,
  location,
  selectedBlockId,
  onSelectBlock,
  onQuote,
  onGenerate,
  onRegenerate,
  onSelectSection,
  onSelectChapter,
  onCloseChapterAction,
  onSectionChange,
  onRefreshSeries,
  onFeedbackBlock,
  onGlobalFeedback,
  onReturnHome,
  onRestorePersonalPresentation,
  onExplainBlock,
}: {
  series: Series;
  section: Section | null;
  chapter: Chapter | null;
  chapterAction: ChapterLaunchAction | null;
  dailyMode: DailyMode;
  studySessionSeconds: number;
  studyPaused: boolean;
  onResumeStudy: () => void;
  directoryHidden: boolean;
  qaHidden: boolean;
  qaAvailable: boolean;
  readerTypographyStep: number;
  readerTypographyLabel: string;
  readerTypographyBodySize: number;
  readerTypographyStepCount: number;
  onReaderTypographyStepChange: (step: number) => void;
  onToggleDirectory: () => void;
  onToggleQa: () => void;
  onOpenQa: () => void;
  onTabChange: (tab: ReaderTab) => void;
  onQuizReviewVisibilityChange: (visible: boolean) => void;
  location: ReturnType<typeof findSectionLocation>;
  selectedBlockId: string;
  onSelectBlock: (blockId: string) => void;
  onQuote: (quote: TextQuote) => void;
  onGenerate: () => void;
  onRegenerate: () => Promise<void>;
  onSelectSection: (id: string) => Promise<Section>;
  onSelectChapter: (chapterId: string) => void;
  onCloseChapterAction: () => void;
  onSectionChange: (section: Section) => void;
  onRefreshSeries: () => Promise<void>;
  onFeedbackBlock: (block: Block) => void;
  onGlobalFeedback: () => void;
  onReturnHome: () => void;
  onRestorePersonalPresentation: (block: Block) => Promise<void>;
  onExplainBlock: (block: Block, style: ExplanationStyle, customQuestion?: string) => void;
}) {
  const [tab, setTab] = useState<ReaderTab>('content');
  const [selectionPopup, setSelectionPopup] = useState<SelectionPopup | null>(null);
  const [annotations, setAnnotations] = useState<ReadingAnnotation[]>([]);
  const [annotationsOpen, setAnnotationsOpen] = useState(false);
  const [annotationComposer, setAnnotationComposer] = useState<AnnotationComposerDraft | null>(null);
  const [annotationBusy, setAnnotationBusy] = useState(false);
  const [annotationMessage, setAnnotationMessage] = useState('');
  const [editingAnnotationId, setEditingAnnotationId] = useState('');
  const [editingAnnotationBody, setEditingAnnotationBody] = useState('');
  const [regenerationConfirmOpen, setRegenerationConfirmOpen] = useState(false);
  const [regenerating, setRegenerating] = useState(false);
  const [regenerationStartedAt, setRegenerationStartedAt] = useState(0);
  const [regenerationClock, setRegenerationClock] = useState(Date.now());
  const [reviewTargetBlockId, setReviewTargetBlockId] = useState('');
  const [readerHeaderCondensed, setReaderHeaderCondensed] = useState(false);
  const readerScrollRef = useRef<HTMLDivElement>(null);
  const tabScrollPositionsRef = useRef<Record<ReaderTab, number>>({
    content: 0,
    quiz: 0,
    note: 0,
  });
  const reviewHighlightTimerRef = useRef<number | null>(null);
  const regenerationDialogRef = useModalFocus<HTMLElement>({
    open: regenerationConfirmOpen,
    canClose: !regenerating,
    onRequestClose: () => setRegenerationConfirmOpen(false),
  });
  const annotationsDialogRef = useModalFocus<HTMLElement>({
    open: annotationsOpen,
    canClose: !annotationBusy,
    onRequestClose: () => setAnnotationsOpen(false),
  });

  useEffect(() => {
    const focusAskMe = window.location.hash === '#ask-me';
    const initialTab = focusAskMe
      ? 'quiz'
      : section?.status === 'completed' && section.note
        ? 'note'
        : 'content';
    setTab(initialTab);
    onTabChange(initialTab);
    setSelectionPopup(null);
    setAnnotationsOpen(false);
    setAnnotationComposer(null);
    setAnnotationMessage('');
    setEditingAnnotationId('');
    setEditingAnnotationBody('');
    setRegenerationConfirmOpen(false);
    setReviewTargetBlockId('');
    setReaderHeaderCondensed(false);
    tabScrollPositionsRef.current = { content: 0, quiz: 0, note: 0 };
    if (reviewHighlightTimerRef.current !== null) {
      window.clearTimeout(reviewHighlightTimerRef.current);
      reviewHighlightTimerRef.current = null;
    }
    if (readerScrollRef.current) readerScrollRef.current.scrollTop = 0;
    if (focusAskMe) {
      requestAnimationFrame(() => requestAnimationFrame(() => {
        readerScrollRef.current?.querySelector<HTMLElement>('.askme-view')?.scrollIntoView({
          behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
          block: 'start',
        });
      }));
    }
  }, [section?.id, section?.content?.id]);

  useEffect(() => {
    let active = true;
    if (!section?.content) {
      setAnnotations([]);
      return () => { active = false; };
    }
    api.annotations(section.id)
      .then((result) => { if (active) setAnnotations(result.items); })
      .catch((reason) => {
        if (active) setAnnotationMessage(
          reason instanceof Error ? reason.message : '暂时无法读取标注。',
        );
      });
    return () => { active = false; };
  }, [section?.id, section?.content?.id]);

  useEffect(() => {
    const registry = (CSS as unknown as {
      highlights?: { set: (name: string, value: unknown) => void; delete: (name: string) => void };
    }).highlights;
    const HighlightConstructor = (window as unknown as {
      Highlight?: new (...ranges: Range[]) => unknown;
    }).Highlight;
    if (!registry || !HighlightConstructor || tab !== 'content') return undefined;

    const paint = () => {
      const highlightRanges: Range[] = [];
      const commentRanges: Range[] = [];
      annotations.forEach((annotation) => {
        if (!annotation.displayBlockId) return;
        const block = Array.from(
          readerScrollRef.current?.querySelectorAll<HTMLElement>('[data-block-id]') || [],
        ).find((element) => element.dataset.blockId === annotation.displayBlockId);
        const body = block?.querySelector<HTMLElement>('[data-annotation-body]');
        if (!body) return;
        const range = annotationRange(body, annotation.anchor);
        if (!range) return;
        (annotation.kind === 'comment' ? commentRanges : highlightRanges).push(range);
      });
      registry.delete('slow-reading-highlights');
      registry.delete('slow-reading-comments');
      if (highlightRanges.length) {
        registry.set('slow-reading-highlights', new HighlightConstructor(...highlightRanges));
      }
      if (commentRanges.length) {
        registry.set('slow-reading-comments', new HighlightConstructor(...commentRanges));
      }
    };
    const frame = window.requestAnimationFrame(paint);
    const observer = new MutationObserver(() => window.requestAnimationFrame(paint));
    if (readerScrollRef.current) {
      observer.observe(readerScrollRef.current, { childList: true, subtree: true });
    }
    return () => {
      window.cancelAnimationFrame(frame);
      observer.disconnect();
      registry.delete('slow-reading-highlights');
      registry.delete('slow-reading-comments');
    };
  }, [annotations, tab, section?.content?.id]);

  useEffect(() => () => {
    if (reviewHighlightTimerRef.current !== null) {
      window.clearTimeout(reviewHighlightTimerRef.current);
    }
  }, []);

  useEffect(() => {
    if (!regenerating) return undefined;
    const timer = window.setInterval(
      () => setRegenerationClock(Date.now()),
      1000,
    );
    return () => window.clearInterval(timer);
  }, [regenerating]);

  const switchTab = (nextTab: ReaderTab) => {
    if (readerScrollRef.current) {
      tabScrollPositionsRef.current[tab] = readerScrollRef.current.scrollTop;
    }
    if (nextTab === 'quiz' && tab !== 'quiz' && section) {
      telemetry.track('quiz_viewed', {
        view: 'learn',
        entityType: 'section',
        entityId: section.id,
      });
    }
    onTabChange(nextTab);
    setTab(nextTab);
    requestAnimationFrame(() => {
      if (readerScrollRef.current) {
        const nextScrollTop = tabScrollPositionsRef.current[nextTab];
        readerScrollRef.current.scrollTop = nextScrollTop;
        setReaderHeaderCondensed(nextScrollTop > 28);
      }
    });
  };

  const reviewContent = (blockId?: string) => {
    const targetExists = Boolean(
      blockId && section?.content?.blocks.some((block) => block.id === blockId),
    );
    if (!blockId || !targetExists) {
      switchTab('content');
      return;
    }

    if (readerScrollRef.current) {
      tabScrollPositionsRef.current[tab] = readerScrollRef.current.scrollTop;
    }
    onSelectBlock(blockId);
    onTabChange('content');
    setTab('content');
    setSelectionPopup(null);
    setReviewTargetBlockId(blockId);
    if (reviewHighlightTimerRef.current !== null) {
      window.clearTimeout(reviewHighlightTimerRef.current);
    }
    reviewHighlightTimerRef.current = window.setTimeout(() => {
      setReviewTargetBlockId('');
      reviewHighlightTimerRef.current = null;
    }, 3200);

    requestAnimationFrame(() => requestAnimationFrame(() => {
      const target = Array.from(
        readerScrollRef.current?.querySelectorAll<HTMLElement>('[data-block-id]') || [],
      ).find((element) => element.dataset.blockId === blockId);
      if (!target) return;
      target.focus({ preventScroll: true });
      target.scrollIntoView({
        behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches
          ? 'auto'
          : 'smooth',
        block: 'center',
      });
    }));
  };

  const captureTextSelection = () => {
    const selection = window.getSelection();
    const range = selection?.rangeCount ? selection.getRangeAt(0) : null;
    const anchorElement =
      selection?.anchorNode instanceof Element
        ? selection.anchorNode
        : selection?.anchorNode?.parentElement;
    const focusElement =
      selection?.focusNode instanceof Element
        ? selection.focusNode
        : selection?.focusNode?.parentElement;
    const blockElement = anchorElement?.closest<HTMLElement>('[data-block-id]');
    const focusBlock = focusElement?.closest<HTMLElement>('[data-block-id]');
    const annotationBody = anchorElement?.closest<HTMLElement>('[data-annotation-body]');
    const focusBody = focusElement?.closest<HTMLElement>('[data-annotation-body]');
    const rawText = selection?.toString() || '';
    const text = rawText.replace(/\s+/g, ' ').trim();

    if (
      !range || range.collapsed || !blockElement || blockElement !== focusBlock
      || !annotationBody || annotationBody !== focusBody || text.length < 2
    ) {
      setSelectionPopup(null);
      return;
    }

    const exact = rawText.slice(0, 1200);
    const before = document.createRange();
    before.selectNodeContents(annotationBody);
    before.setEnd(range.startContainer, range.startOffset);
    const startOffset = before.toString().length;
    const bodyText = annotationBody.textContent || '';
    const endOffset = startOffset + exact.length;
    const rect = range.getBoundingClientRect();
    const compactViewport = window.matchMedia('(max-width: 760px)').matches;
    const toolbarHeight = 40;
    const toolbarGap = 10;
    const viewportPadding = 12;
    const nativeMenuClearance = 66;
    const placeAbove = !compactViewport
      && rect.top >= toolbarHeight + toolbarGap + viewportPadding;
    const toolbarTop = placeAbove
      ? Math.max(viewportPadding, rect.top - toolbarHeight - toolbarGap)
      : Math.min(
        rect.bottom + nativeMenuClearance,
        window.innerHeight - toolbarHeight - viewportPadding,
      );
    setSelectionPopup({
      text: text.slice(0, 600),
      blockId: blockElement.dataset.blockId || '',
      contentVersionId: section?.content?.id || '',
      anchor: {
        exact,
        prefix: bodyText.slice(Math.max(0, startOffset - 80), startOffset),
        suffix: bodyText.slice(endOffset, endOffset + 80),
        startOffset,
        endOffset,
      },
      top: toolbarTop,
      left: Math.min(Math.max(rect.left + rect.width / 2, 112), window.innerWidth - 112),
      placement: placeAbove ? 'above' : 'below',
      composerTop: Math.max(12, Math.min(rect.bottom + 12, window.innerHeight - 230)),
    });
  };

  const clearNativeSelection = () => {
    const currentSelection = window.getSelection();
    if (typeof currentSelection?.removeAllRanges === 'function') {
      currentSelection.removeAllRanges();
    }
  };

  const reloadAnnotations = async () => {
    if (!section?.content) return;
    const result = await api.annotations(section.id);
    setAnnotations(result.items);
  };

  const saveAnnotation = async (
    kind: 'highlight' | 'comment',
    selection: SelectionPopup,
    body = '',
  ) => {
    if (!section?.content || annotationBusy) return;
    setAnnotationBusy(true);
    setAnnotationMessage('');
    try {
      await api.createAnnotation(section.id, {
        contentVersionId: selection.contentVersionId,
        blockId: selection.blockId,
        kind,
        anchor: selection.anchor,
        body,
        color: 'amber',
      }, crypto.randomUUID());
      await reloadAnnotations();
      setAnnotationMessage(kind === 'comment' ? '批注已保存。' : '已高亮。');
      setSelectionPopup(null);
      setAnnotationComposer(null);
      clearNativeSelection();
    } catch (reason) {
      setAnnotationMessage(reason instanceof Error ? reason.message : '标注没有保存成功。');
    } finally {
      setAnnotationBusy(false);
    }
  };

  const updateAnnotation = async (annotationId: string) => {
    if (!editingAnnotationBody.trim() || annotationBusy) return;
    setAnnotationBusy(true);
    setAnnotationMessage('');
    try {
      await api.updateAnnotation(annotationId, { body: editingAnnotationBody.trim() });
      await reloadAnnotations();
      setEditingAnnotationId('');
      setEditingAnnotationBody('');
      setAnnotationMessage('批注已更新。');
    } catch (reason) {
      setAnnotationMessage(reason instanceof Error ? reason.message : '批注没有更新成功。');
    } finally {
      setAnnotationBusy(false);
    }
  };

  const deleteAnnotation = async (annotationId: string) => {
    if (annotationBusy) return;
    setAnnotationBusy(true);
    setAnnotationMessage('');
    try {
      await api.deleteAnnotation(annotationId);
      await reloadAnnotations();
      setAnnotationMessage('标注已删除。');
    } catch (reason) {
      setAnnotationMessage(reason instanceof Error ? reason.message : '标注没有删除成功。');
    } finally {
      setAnnotationBusy(false);
    }
  };

  const focusAnnotation = (annotation: ReadingAnnotation) => {
    if (!annotation.displayBlockId) return;
    switchTab('content');
    onSelectBlock(annotation.displayBlockId);
    setAnnotationsOpen(false);
    requestAnimationFrame(() => requestAnimationFrame(() => {
      const target = Array.from(
        readerScrollRef.current?.querySelectorAll<HTMLElement>('[data-block-id]') || [],
      ).find((element) => element.dataset.blockId === annotation.displayBlockId);
      target?.scrollIntoView({
        behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
        block: 'center',
      });
    }));
  };

  const activeGeneration =
    section?.generation?.operation === 'regeneration'
      ? section.generation
      : null;
  const generationTrace = activeGeneration?.trace || {};
  const generationStage =
    typeof generationTrace.stage === 'string'
      ? generationTrace.stage
      : 'queued';
  const generationElapsed = Math.max(
    activeGeneration?.durationMs || 0,
    regenerationStartedAt ? regenerationClock - regenerationStartedAt : 0,
  );

  if (!section) {
    return (
      <main className="reader-panel route-preview-reader">
        <ReaderPanelToggles
          directoryHidden={directoryHidden}
          qaHidden={qaHidden}
          qaAvailable={qaAvailable}
          onToggleDirectory={onToggleDirectory}
          onToggleQa={onToggleQa}
          onOpenQa={onOpenQa}
        />
        {chapter && chapterAction ? (
          <ChapterLaunchPanel
            chapter={chapter}
            initialAction={chapterAction}
            onCancel={onCloseChapterAction}
            onSelectSection={onSelectSection}
            onSelectChapter={onSelectChapter}
            onRefreshSeries={onRefreshSeries}
          />
        ) : <SeriesRoutePreview series={series} />}
      </main>
    );
  }

  return (
    <main className="reader-panel">
      <ReaderPanelToggles
        directoryHidden={directoryHidden}
        qaHidden={qaHidden}
        qaAvailable={qaAvailable}
        onToggleDirectory={onToggleDirectory}
        onToggleQa={onToggleQa}
        onOpenQa={onOpenQa}
      />
      <LessonReaderHeader
        bookPosition={location?.book.position}
        chapterPosition={location?.chapter.position}
        chapterTitle={location?.chapter.title}
        sectionPosition={section.position}
        title={section.title}
        sessionSeconds={studySessionSeconds}
        status={
          !section.content && section.generation?.status === 'failed'
            ? 'failed'
            : section.status
        }
        canRegenerate={Boolean(section.content && section.bestScore === 0 && section.totalScore === 0)}
        regenerating={regenerating}
        readerTypographyStep={readerTypographyStep}
        readerTypographyLabel={readerTypographyLabel}
        readerTypographyBodySize={readerTypographyBodySize}
        readerTypographyStepCount={readerTypographyStepCount}
        onReaderTypographyStepChange={onReaderTypographyStepChange}
        onRequestRegenerate={() => setRegenerationConfirmOpen(true)}
        onFeedback={onGlobalFeedback}
        condensed={readerHeaderCondensed}
        directoryOpen={!directoryHidden}
        onToggleDirectory={onToggleDirectory}
        onReturnHome={onReturnHome}
      />

      <LessonReaderTabs
        active={tab}
        quizAvailable={Boolean(section.quiz)}
        noteAvailable={Boolean(section.note)}
        completed={section.status === 'completed'}
        onChange={switchTab}
      />

      <div className="reader-scroll-shell">
        <div
          className="reader-scroll"
          id="reader-tabpanel"
          role="tabpanel"
          aria-labelledby={`reader-tab-${tab}`}
          ref={readerScrollRef}
          onMouseUp={captureTextSelection}
          onKeyUp={captureTextSelection}
          onScroll={(event) => {
            setSelectionPopup(null);
            const condensed = event.currentTarget.scrollTop > 28;
            setReaderHeaderCondensed((current) => current === condensed ? current : condensed);
          }}
        >
          {tab === 'content' && (
            <LessonContent
              section={section}
              dailyMode={dailyMode}
              annotations={annotations}
              onOpenAnnotations={() => setAnnotationsOpen(true)}
              selectedBlockId={selectedBlockId}
              reviewTargetBlockId={reviewTargetBlockId}
              onGenerate={onGenerate}
              onStartQuiz={() => switchTab('quiz')}
              onFeedbackBlock={onFeedbackBlock}
              onRestorePersonalPresentation={onRestorePersonalPresentation}
              onExplainBlock={onExplainBlock}
            />
          )}
          {tab === 'quiz' && section.quiz && (
            <Quiz
              key={section.quiz.id}
              series={series}
              section={section}
              onUpgrade={() => setRegenerationConfirmOpen(true)}
              onSectionChange={onSectionChange}
              onRefreshSeries={onRefreshSeries}
              onSelectSection={onSelectSection}
              onReviewContent={reviewContent}
              onFeedback={onGlobalFeedback}
              onOpenQa={onOpenQa}
              onReviewVisibilityChange={onQuizReviewVisibilityChange}
              onSubmissionComplete={() => {
                tabScrollPositionsRef.current.quiz = 0;
                setReaderHeaderCondensed(false);
                if (readerScrollRef.current) readerScrollRef.current.scrollTop = 0;
              }}
            />
          )}
          {tab === 'note' && section.note && (
            <Note sectionId={section.id} note={section.note} onSaved={onSectionChange} />
          )}
        </div>
        {studyPaused && (
          <button
            type="button"
            className="reader-study-pause"
            onClick={onResumeStudy}
          >
            <span>刚才是在思考吗？</span>
            <small>轻触、滚动或按任意键继续</small>
          </button>
        )}
      </div>
      {regenerationConfirmOpen && (
        <div
          className="confirm-backdrop"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget && !regenerating) setRegenerationConfirmOpen(false);
          }}
        >
          <section
            ref={regenerationDialogRef}
            className="delete-confirm regenerate-confirm"
            role="dialog"
            aria-modal="true"
            aria-labelledby="regenerate-section-title"
            aria-describedby="regenerate-section-description"
            tabIndex={-1}
          >
            <p className="eyebrow">重新生成本节</p>
            <h2 id="regenerate-section-title">{section.title}</h2>
            <p id="regenerate-section-description">重新生成会替换当前正文和验证题；已完成验证的内容无法重新生成。</p>
            {regenerating && (
              <div className="regeneration-progress" aria-live="polite">
                <span><i />{GENERATION_STAGE_LABELS[generationStage] || '正在处理'}</span>
                <b>处理中</b>
              </div>
            )}
            <div>
              <button data-dialog-initial-focus className="quiet-button" disabled={regenerating} onClick={() => setRegenerationConfirmOpen(false)}>取消</button>
              <button
                className="primary-button"
                disabled={regenerating}
                onClick={async () => {
                  const startedAt = Date.now();
                  setRegenerationStartedAt(startedAt);
                  setRegenerationClock(startedAt);
                  setRegenerating(true);
                  try {
                    await onRegenerate();
                    setRegenerationConfirmOpen(false);
                  } finally {
                    setRegenerating(false);
                  }
                }}
              >
                {regenerating ? '正在重新生成…' : '确认重新生成'}
              </button>
            </div>
          </section>
        </div>
      )}
      {selectionPopup && (
        <div
          className="selection-actions"
          style={{ top: selectionPopup.top, left: selectionPopup.left }}
          data-placement={selectionPopup.placement}
          onMouseDown={(event) => event.preventDefault()}
        >
          <button
            className="selection-qa-button"
            onClick={() => {
              onQuote({ text: selectionPopup.text, blockId: selectionPopup.blockId });
              setSelectionPopup(null);
              clearNativeSelection();
            }}
          >
            <span aria-hidden="true">AI</span>
            Ask AI
          </button>
          <span className="selection-actions-divider" aria-hidden="true" />
          <button
            className="selection-mark-button"
            disabled={annotationBusy}
            onClick={() => void saveAnnotation('highlight', selectionPopup)}
          >
            <i aria-hidden="true" />高亮
          </button>
          <button
            className="selection-comment-button"
            onClick={() => {
              setAnnotationComposer({ selection: selectionPopup, body: '' });
              setSelectionPopup(null);
              clearNativeSelection();
            }}
          >
            批注
          </button>
        </div>
      )}
      {annotationComposer && (
        <section
          className="annotation-composer-popover"
          style={{ top: annotationComposer.selection.composerTop, left: annotationComposer.selection.left }}
          aria-label="添加批注"
        >
          <header><b>写给自己的批注</b><button aria-label="关闭" onClick={() => setAnnotationComposer(null)}>×</button></header>
          <blockquote>{annotationComposer.selection.text}</blockquote>
          <textarea
            autoFocus
            maxLength={4000}
            value={annotationComposer.body}
            placeholder="记录你的理解、联想或提醒…"
            onChange={(event) => setAnnotationComposer((current) => current ? { ...current, body: event.target.value } : current)}
          />
          <div>
            <small>{annotationComposer.body.length}/4000</small>
            <button
              className="primary-button"
              disabled={!annotationComposer.body.trim() || annotationBusy}
              onClick={() => void saveAnnotation('comment', annotationComposer.selection, annotationComposer.body)}
            >
              {annotationBusy ? '保存中…' : '保存批注'}
            </button>
          </div>
        </section>
      )}
      {annotationsOpen && (
        <div className="annotation-drawer-backdrop" onMouseDown={(event) => {
          if (event.target === event.currentTarget) setAnnotationsOpen(false);
        }}>
          <aside
            ref={annotationsDialogRef}
            className="annotation-drawer"
            role="dialog"
            aria-modal="true"
            aria-label="我的标注"
            tabIndex={-1}
          >
            <header>
              <div><span>阅读标注</span><h2>我的标注</h2></div>
              <button aria-label="关闭标注" onClick={() => setAnnotationsOpen(false)}>×</button>
            </header>
            <p className="annotation-drawer-intro">高亮和批注只记录你的阅读，不会发送给 AI，也不会改变掌握情况。</p>
            {annotations.length === 0 ? (
              <div className="annotation-empty"><i aria-hidden="true" /><b>还没有标注</b><p>在正文中选择文字，就可以高亮或写下批注。</p></div>
            ) : (
              <div className="annotation-list">
                {annotations.map((annotation) => (
                  <article className={`annotation-card is-${annotation.kind} is-${annotation.anchorStatus}`} key={annotation.id}>
                    <header>
                      <span>{annotation.kind === 'comment' ? '批注' : '高亮'}</span>
                      <em>{annotation.anchorStatus === 'old_version'
                        ? `正文旧版${annotation.contentVersion ? ` v${annotation.contentVersion}` : ''}`
                        : annotation.anchorStatus === 'unchanged_in_current'
                          ? '原文未变化'
                          : '当前正文'}</em>
                    </header>
                    <button
                      type="button"
                      className="annotation-quote"
                      disabled={!annotation.displayBlockId}
                      onClick={() => focusAnnotation(annotation)}
                    >“{annotation.anchor.exact.replace(/\s+/g, ' ').trim()}”</button>
                    {annotation.kind === 'comment' && (
                      editingAnnotationId === annotation.id ? (
                        <div className="annotation-edit">
                          <textarea value={editingAnnotationBody} maxLength={4000} onChange={(event) => setEditingAnnotationBody(event.target.value)} />
                          <div><button onClick={() => setEditingAnnotationId('')}>取消</button><button disabled={annotationBusy || !editingAnnotationBody.trim()} onClick={() => void updateAnnotation(annotation.id)}>保存</button></div>
                        </div>
                      ) : <p>{annotation.body}</p>
                    )}
                    {annotation.anchorStatus === 'old_version' && <small>原文已经更新，这条记录仍保留在你当时阅读的版本。</small>}
                    <footer>
                      <time dateTime={annotation.updatedAt}>{new Date(annotation.updatedAt).toLocaleDateString('zh-CN')}</time>
                      <div>
                        {annotation.kind === 'comment' && editingAnnotationId !== annotation.id && (
                          <button onClick={() => { setEditingAnnotationId(annotation.id); setEditingAnnotationBody(annotation.body); }}>编辑</button>
                        )}
                        <button disabled={annotationBusy} onClick={() => void deleteAnnotation(annotation.id)}>删除</button>
                      </div>
                    </footer>
                  </article>
                ))}
              </div>
            )}
            {annotationMessage && <p className="annotation-drawer-message" role="status">{annotationMessage}</p>}
          </aside>
        </div>
      )}
      {annotationMessage && !annotationsOpen && <p className="annotation-toast" role="status">{annotationMessage}</p>}
    </main>
  );
}

function SeriesRoutePreview({ series }: { series: Series }) {
  const taskStatus = series.initializationTask?.status;
  const totalMinutes = series.books.reduce(
    (total, book) => total + book.estimatedMinutes,
    0,
  );
  const firstBook = series.books[0];
  const firstChapter = firstBook?.chapters[0];
  const statusCopy = taskStatus === 'failed'
    ? generationFailureMessage(series.initializationTask, '第一节内容')
    : taskStatus === 'pending'
      ? '排队中'
      : taskStatus === 'running'
        ? '准备中'
        : '选择左侧已经解锁的小节开始学习。';
  return (
    <div className="route-preview-scroll">
      <header className="route-preview-hero">
        <div className={`route-preparation-state ${taskStatus || 'ready'}`}>
          <i aria-hidden="true" />
          <span>{taskStatus === 'failed'
            ? '第一节需要重新准备'
            : taskStatus === 'pending' || taskStatus === 'running'
              ? '第一节正在准备'
              : '学习路线已就绪'}</span>
        </div>
        <p className="eyebrow">你的学习路线</p>
        <h1>{series.title}</h1>
        <p>{series.rationale}</p>
        <div className="route-preview-meta" aria-label="学习路线概览">
          <span><b>{series.books.length}</b> 本书</span>
          <span><b>{series.books.reduce((total, book) => total + book.chapters.length, 0)}</b> 章</span>
          <span><b>{Math.max(1, Math.round(totalMinutes / 60))}</b> 小时预计投入</span>
        </div>
      </header>

      <section className="route-opening-note">
        <span className="route-opening-mark" aria-hidden="true">起</span>
        <div>
          <small>从这里开始</small>
          <h2>{firstChapter?.title || firstBook?.title || '第一节'}</h2>
          <p>{firstChapter?.objective || statusCopy}</p>
        </div>
      </section>

      <section className="route-book-sequence" aria-label="全系列书目与章节">
        {series.books.map((book) => (
          <article className="route-book" key={book.id}>
            <div className="route-book-spine" aria-hidden="true">
              <span>{String(book.position).padStart(2, '0')}</span>
            </div>
            <div className="route-book-body">
              <header>
                <div>
                  <small>第 {book.position} 本 · 约 {Math.max(1, Math.round(book.estimatedMinutes / 60))} 小时</small>
                  <h2>{book.title}</h2>
                </div>
                <span>{book.position === 1 ? '即将开始' : '后续路径'}</span>
              </header>
              <p>{book.description}</p>
              <ol>
                {book.chapters.map((chapter) => (
                  <li key={chapter.id}>
                    <b>第 {chapter.position} 章</b>
                    <span>{chapter.title}</span>
                  </li>
                ))}
              </ol>
            </div>
          </article>
        ))}
      </section>
    </div>
  );
}

function ReaderPanelToggles({
  directoryHidden,
  qaHidden,
  qaAvailable,
  onToggleDirectory,
  onToggleQa,
  onOpenQa,
}: {
  directoryHidden: boolean;
  qaHidden: boolean;
  qaAvailable: boolean;
  onToggleDirectory: () => void;
  onToggleQa: () => void;
  onOpenQa: () => void;
}) {
  return (
    <>
      {directoryHidden && (
        <button
          className="reader-directory-trigger"
          aria-controls="course-directory-panel"
          aria-expanded={false}
          onClick={onToggleDirectory}
        >
          目录
        </button>
      )}
      {!directoryHidden && (
        <button
          className="reader-rail-toggle directory-toggle"
          aria-controls="course-directory-panel"
          aria-expanded={true}
          aria-label="收起目录"
          title="收起目录"
          onPointerDown={(event) => event.stopPropagation()}
          onClick={(event) => {
            event.stopPropagation();
            onToggleDirectory();
          }}
        >
          ‹
        </button>
      )}
      {qaAvailable && qaHidden && (
        <button
          type="button"
          className="reader-qa-trigger"
          aria-controls="section-qa-panel"
          aria-expanded={false}
          onClick={onOpenQa}
        >
          Ask AI
        </button>
      )}
      {qaAvailable && !qaHidden && (
        <button
          className="reader-rail-toggle qa-toggle"
          aria-controls="section-qa-panel"
          aria-expanded={true}
          aria-label="收起答疑"
          title="收起答疑"
          onPointerDown={(event) => event.stopPropagation()}
          onClick={(event) => {
            event.stopPropagation();
            onToggleQa();
          }}
        >
          ›
        </button>
      )}
    </>
  );
}

function selectFastBlocks(blocks: Block[]) {
  const selected: Block[] = [];
  const add = (block?: Block) => {
    if (block && !selected.some((item) => item.id === block.id)) selected.push(block);
  };

  // V3 provides an explicit reader projection. Legacy lessons still fall back
  // to their conclusion/boundary roles, then to authored order.
  blocks.filter((block) => block.readerPriority === 'essential').forEach(add);
  add(blocks.find((block) => block.role === 'core_instruction'));
  add(blocks.find((block) => block.role === 'conclusion'));
  add(blocks.find((block) => block.readerPriority === 'highlight'));
  add(blocks.find((block) => block.role === 'boundary'));
  blocks.forEach((block) => {
    if (selected.length < 2) add(block);
  });
  return selected.slice(0, 2);
}

function LessonContent({
  section,
  dailyMode,
  annotations,
  onOpenAnnotations,
  selectedBlockId,
  reviewTargetBlockId,
  onGenerate,
  onStartQuiz,
  onFeedbackBlock,
  onRestorePersonalPresentation,
  onExplainBlock,
}: {
  section: Section;
  dailyMode: DailyMode;
  annotations: ReadingAnnotation[];
  onOpenAnnotations: () => void;
  selectedBlockId: string;
  reviewTargetBlockId: string;
  onGenerate: () => void;
  onStartQuiz: () => void;
  onFeedbackBlock: (block: Block) => void;
  onRestorePersonalPresentation: (block: Block) => Promise<void>;
  onExplainBlock: (block: Block, style: ExplanationStyle, customQuestion?: string) => void;
}) {
  const [showCompleteFast, setShowCompleteFast] = useState(false);
  const [fastCheck, setFastCheck] = useState<'clear'|'unclear'|null>(null);

  useEffect(() => {
    setShowCompleteFast(false);
    setFastCheck(null);
  }, [section.id, section.content?.id, dailyMode]);

  useEffect(() => {
    if (dailyMode === 'fast' && reviewTargetBlockId) {
      setShowCompleteFast(true);
    }
  }, [dailyMode, reviewTargetBlockId]);

  if (!section.content) {
    return (
      <div className="lesson-intro">
        <p className="eyebrow">本节只解决一个问题</p>
        <h2>{section.question}</h2>
        <div className="objective-list">
          {(section.objectives || []).map((objective, index) => (
            <div key={objective}><span>{index + 1}</span><p>{objective}</p></div>
          ))}
        </div>
        {section.generation?.status === 'failed' && (
          <div className="inline-error">
            {generationFailureMessage(section.generation)}
          </div>
        )}
        <button className="primary-button large" onClick={onGenerate}>
          {section.generation?.status === 'failed' ? '重新准备' : '准备正文并开始学习'}
        </button>
      </div>
    );
  }

  const visibleSources = section.content.sources.flatMap((source, index) => (
    section.content?.sourceVerification[index]?.verificationStatus === 'failed'
      ? []
      : [{ source, index }]
  ));
  const fastBlocks = selectFastBlocks(section.content.blocks);
  const lessonRoles = new Set(section.content.blocks.map((block) => block.role));
  const fastCheckPrompt = lessonRoles.has('primary_source') || lessonRoles.has('evidence_analysis')
    ? '你能概括本节的核心解释，并说出一条支持它的材料吗？'
    : lessonRoles.has('derivation')
      ? '你能复述本节结论，并指出推导中最关键的一步吗？'
      : lessonRoles.has('empirical_case') || lessonRoles.has('comparison')
        ? '你能复述核心机制，并解释两个案例为什么会出现不同结果吗？'
        : '你能用一句话复述本节核心依据，并说出一个适用条件吗？';
  const shownBlocks = dailyMode === 'fast' && !showCompleteFast && !reviewTargetBlockId
    ? fastBlocks
    : section.content.blocks;

  return (
    <article className="lesson-document">
      <section className="lesson-question" aria-label="本节问题">
        <span>本节问题</span>
        <p>{section.question}</p>
      </section>
      <p className="content-trust-note">
        {section.content.generationMode === 'demo'
          ? '演示内容 · 仅用于体验学习流程'
          : section.content.boundaryValidation.status === 'passed'
            ? `${section.content.aiGenerated ? 'AI 生成' : '授权内容'} · 已完成发布检查 · 关键事实请结合参考来源判断`
            : section.content.boundaryValidation.status === 'legacy'
              ? '历史内容 · 尚未按当前标准重新检查 · 关键事实请结合参考来源判断'
              : `${section.content.aiGenerated ? 'AI 生成' : '授权内容'} · 检查状态未确认 · 关键事实请结合参考来源判断`}
      </p>
      <button type="button" className="annotation-ledger-trigger" onClick={onOpenAnnotations}>
        <span><i aria-hidden="true" />我的标注</span>
        <b>{annotations.length}</b>
        <small>{annotations.some((item) => item.anchorStatus === 'old_version') ? '含旧版正文记录' : '高亮与批注只属于你'}</small>
      </button>
      {dailyMode === 'fast' && (
        <aside className="fast-view-notice">
          <div>
            <span>FAST VIEW · 快速浏览</span>
            <b>{showCompleteFast ? '已展开完整正文' : `核心依据 · ${fastBlocks.length} 个关键段落`}</b>
          </div>
          <button type="button" onClick={() => setShowCompleteFast((shown) => !shown)}>
            {showCompleteFast ? '收回快速视图' : '展开完整正文'}
          </button>
        </aside>
      )}
      {shownBlocks.map((block) => (
        <LessonContentBlock
          key={block.id}
          block={block}
          selected={block.id === selectedBlockId}
          reviewTarget={block.id === reviewTargetBlockId}
          explanationOptions={explanationOptionsForBlock(block.kind)}
          annotationCount={annotations.filter((item) => item.displayBlockId === block.id).length}
          onOpenAnnotations={onOpenAnnotations}
          onFeedback={() => onFeedbackBlock(block)}
          onRestorePersonalPresentation={() => onRestorePersonalPresentation(block)}
          onExplain={(style, customQuestion) => onExplainBlock(block, style, customQuestion)}
        />
      ))}
      {dailyMode === 'fast' && !showCompleteFast && (
        <section className="fast-check" aria-label="快速自检">
          <span>30 秒自检</span>
          <h3>{fastCheckPrompt}</h3>
          <div>
            <button className={fastCheck === 'clear' ? 'selected' : ''} onClick={() => setFastCheck('clear')}>可以复述</button>
            <button className={fastCheck === 'unclear' ? 'selected' : ''} onClick={() => setFastCheck('unclear')}>还不能说明依据</button>
          </div>
          {fastCheck && <p>{fastCheck === 'clear'
            ? '很好。若要完成本节，请继续完成下方同一套验证题。'
            : '先展开完整正文，重点阅读相关段落。'}</p>}
        </section>
      )}
      {visibleSources.length > 0 && <details className="source-list">
        <summary>参考来源 · {visibleSources.length}</summary>
        {visibleSources.map(({ source, index }) => (
          <a href={source.url} target="_blank" rel="noreferrer" key={`${source.url}-${index}`}>
            <span>{index + 1}</span>
            <b>{source.title}</b>
            <small>{source.version}</small>
          </a>
        ))}
      </details>}
      <div className={`lesson-complete-action ${section.status === 'completed' ? 'verified' : ''}`}>
        <span>{section.status === 'completed' ? '本节已验证' : '正文阅读完成'}</span>
        <h3>{section.status === 'completed' ? '本节已验证' : '现在，验证你是否真正理解。'}</h3>
        <p>{section.status === 'completed'
          ? '可以随时回看作答结果、错题解析和对应的正文依据。'
          : '完成选择题：及格解锁下一节，满分解锁隐藏关卡。'}</p>
        <button className="primary-button" onClick={onStartQuiz}>
          {section.status === 'completed' ? '查看验证结果' : '开始验证'} <i>→</i>
        </button>
      </div>
    </article>
  );
}

function Quiz({
  series,
  section,
  onUpgrade,
  onSectionChange,
  onRefreshSeries,
  onSelectSection,
  onReviewContent,
  onFeedback,
  onOpenQa,
  onReviewVisibilityChange,
  onSubmissionComplete,
}: {
  series: Series;
  section: Section;
  onUpgrade: () => void;
  onSectionChange: (section: Section) => void;
  onRefreshSeries: () => Promise<void>;
  onSelectSection: (id: string) => Promise<Section>;
  onReviewContent: (blockId?: string) => void;
  onFeedback: () => void;
  onOpenQa: () => void;
  onReviewVisibilityChange: (visible: boolean) => void;
  onSubmissionComplete: () => void;
}) {
  const quizDraftKey = `slow:quiz-draft:${section.id}:${section.quiz?.id || 'none'}`;
  const quizRequestStorageKey = `slow:quiz-request:${section.id}:${section.quiz?.id || 'none'}`;
  const quizResultStorageKey = `slow:quiz-result:${section.id}:${section.quiz?.id || 'none'}`;
  const quizGovernanceBlocked = Boolean(
    section.quiz && (
      !section.quiz.governance?.allowed ||
      !section.quiz.governance?.assessmentEligible
    ),
  );
  const [answers, setAnswers] = useState<number[][]>(() => {
    const empty = section.quiz?.questions.map(() => []) || [];
    try {
      const saved = JSON.parse(localStorage.getItem(quizDraftKey) || 'null');
      if (
        Array.isArray(saved) &&
        saved.length === empty.length &&
        saved.every((answer, questionIndex) => (
          Array.isArray(answer) &&
          new Set(answer).size === answer.length &&
          (
            section.quiz?.questions[questionIndex]?.selectionMode === 'multiple' ||
            answer.length <= 1
          ) &&
          answer.every((optionIndex) => (
            Number.isInteger(optionIndex) &&
            optionIndex >= 0 &&
            optionIndex < (section.quiz?.questions[questionIndex]?.options.length || 0)
          ))
        ))
      ) {
        return saved;
      }
    } catch {
      localStorage.removeItem(quizDraftKey);
    }
    return empty;
  });
  const [result, setResult] = useState<QuizResult | null>(() => {
    try {
      const saved = JSON.parse(sessionStorage.getItem(quizResultStorageKey) || 'null');
      if (
        saved &&
        typeof saved.attemptId === 'string' &&
        typeof saved.passed === 'boolean' &&
        Array.isArray(saved.results) &&
        Array.isArray(saved.questions) &&
        saved.results.length === saved.questions.length
      ) {
        return saved;
      }
    } catch {
      try {
        sessionStorage.removeItem(quizResultStorageKey);
      } catch {
        // The new quiz can still open when browser session storage is unavailable.
      }
    }
    return section.latestAttemptReview;
  });
  const [submissionError, setSubmissionError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [workflowRunning, setWorkflowRunning] = useState(false);
  const [workflowTasks, setWorkflowTasks] = useState<LearningTask[]>(
    section.workflowTasks || [],
  );
  const [retryingTasks, setRetryingTasks] = useState(false);
  const [remediationReady, setRemediationReady] = useState(false);
  const [openingRemediation, setOpeningRemediation] = useState(false);
  const [reassessing, setReassessing] = useState(false);
  const [openingNextSection, setOpeningNextSection] = useState(false);
  const failedTasks = workflowTasks.filter((task) => (
    task.status === 'failed' &&
    (!result || !task.triggerId || task.triggerId === result.attemptId)
  ));
  const remediationTask = result
    ? workflowTasks.find((task) => (
        task.type === 'remediation_generation' &&
        task.triggerId === result.attemptId
      ))
    : null;
  const remediationAvailable = (
    remediationReady || remediationTask?.status === 'succeeded'
  );
  const nextSectionTask = result
    ? workflowTasks.find((task) => (
        task.type === 'next_section_preload' &&
        task.triggerId === result.attemptId
      ))
    : null;
  const nextSectionId = typeof nextSectionTask?.result?.targetSectionId === 'string'
    ? nextSectionTask.result.targetSectionId
    : null;
  const nextSectionLocation = findSectionLocation(series, nextSectionId || undefined);
  const eligibleUnderCurrentPolicy = Boolean(
    result && !result.passed && result.reassessmentEligible,
  );

  useEffect(() => {
    onReviewVisibilityChange(Boolean(result) && !quizGovernanceBlocked);
  }, [onReviewVisibilityChange, quizGovernanceBlocked, result]);

  useEffect(() => {
    localStorage.setItem(quizDraftKey, JSON.stringify(answers));
  }, [answers, quizDraftKey]);

  const monitorTasks = async (
    initialTasks: LearningTask[],
    passed = result?.passed,
    preservedFailures: LearningTask[] = [],
  ) => {
    if (!initialTasks.length) return;
    setWorkflowRunning(true);
    setWorkflowTasks([...preservedFailures, ...initialTasks]);
    let current = initialTasks;
    for (let poll = 0; poll < 900; poll += 1) {
      current = await Promise.all(
        current.map((task) => api.learningTask(task.taskId)),
      );
      setWorkflowTasks([...preservedFailures, ...current]);
      if (current.every((task) => ['succeeded', 'failed'].includes(task.status))) {
        const failures = [
          ...preservedFailures,
          ...current.filter((task) => task.status === 'failed'),
        ];
        setWorkflowRunning(false);
        if (!failures.length && passed === false) {
          setRemediationReady(true);
        }
        await onRefreshSeries();
        return;
      }
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
    }
    setWorkflowRunning(false);
  };

  useEffect(() => {
    const unfinished = (section.workflowTasks || []).filter(
      (task) => task.status !== 'succeeded',
    );
    if (!unfinished.length) return;
    void monitorTasks(unfinished).catch(() => {
      setWorkflowRunning(false);
      setSubmissionError('暂时无法更新后续内容，请稍后再试。');
    });
  }, [section.id]);

  useEffect(() => {
    if (
      !result ||
      result.passed ||
      !remediationTask ||
      remediationTask.status === 'succeeded'
    ) return;
    let cancelled = false;
    const reconcileRemediation = async () => {
      try {
        const latest = await api.learningTask(remediationTask.taskId);
        if (cancelled) return;
        setWorkflowTasks((current) => current.map((task) => (
          task.taskId === latest.taskId ? latest : task
        )));
        if (latest.status === 'failed') return;
        if (latest.status === 'succeeded') {
          setWorkflowRunning(false);
          setRemediationReady(true);
          await onRefreshSeries();
          return;
        }
        if (remediationTask.status === 'failed') {
          void monitorTasks([latest], false).catch(() => {
            setWorkflowRunning(false);
            setSubmissionError('暂时无法更新后续内容，请稍后再试。');
          });
        }
      } catch {
        // A transient refresh failure must not replace the saved quiz result.
      }
    };
    const reconcileOnFocus = () => { void reconcileRemediation(); };
    const reconcileWhenVisible = () => {
      if (document.visibilityState === 'visible') reconcileOnFocus();
    };
    window.addEventListener('focus', reconcileOnFocus);
    document.addEventListener('visibilitychange', reconcileWhenVisible);
    if (remediationTask.status === 'failed') void reconcileRemediation();
    return () => {
      cancelled = true;
      window.removeEventListener('focus', reconcileOnFocus);
      document.removeEventListener('visibilitychange', reconcileWhenVisible);
    };
  }, [
    section.id,
    result?.attemptId,
    result?.passed,
    remediationTask?.taskId,
    remediationTask?.status,
  ]);

  const retryFailedTasks = async () => {
    setSubmissionError('');
    setRetryingTasks(true);
    try {
      const preservedFailures = failedTasks.filter((task) => !task.retryable);
      const retried = await Promise.all(
        failedTasks.filter((task) => task.retryable).map(
          (task) => api.retryLearningTask(task.taskId),
        ),
      );
      await monitorTasks(retried, result?.passed, preservedFailures);
    } catch {
      setSubmissionError('后续内容暂时没有准备完成，请稍后再试。');
    } finally {
      setRetryingTasks(false);
    }
  };

  const openRemediation = async () => {
    setSubmissionError('');
    setOpeningRemediation(true);
    try {
      const next = await api.section(section.id);
      const remediation = result
        ? next.remediations.find((item) => item.attemptId === result.attemptId)
        : null;
      if (!remediation || next.quiz?.id !== remediation.replacementQuizId) {
        throw new Error('补充教学尚未准备完成，请稍后再试。');
      }
      try {
        sessionStorage.removeItem(quizResultStorageKey);
      } catch {
        // Opening the replacement quiz does not depend on browser storage.
      }
      setResult(null);
      onSectionChange(next);
      onSubmissionComplete();
    } catch (reason) {
      setSubmissionError(
        reason instanceof Error ? reason.message : '无法打开补充教学。',
      );
    } finally {
      setOpeningRemediation(false);
    }
  };

  const reassessAttempt = async () => {
    if (!result) return;
    setSubmissionError('');
    setReassessing(true);
    try {
      const value = await api.reassessQuiz(section.id, result.attemptId);
      const reviewValue = {...value, questions: result.questions};
      setResult(reviewValue);
      setWorkflowTasks(value.workflowTasks);
      try {
        sessionStorage.setItem(quizResultStorageKey, JSON.stringify(reviewValue));
      } catch {
        // The promoted result remains available in memory for this render.
      }
      await onRefreshSeries();
      void monitorTasks(value.workflowTasks, true).catch((reason) => {
        setWorkflowRunning(false);
        setSubmissionError(
          reason instanceof Error ? reason.message : '无法读取下一节准备状态。',
        );
      });
    } catch (reason) {
      setSubmissionError(
        reason instanceof Error ? reason.message : '无法按当前规则继续学习。',
      );
    } finally {
      setReassessing(false);
    }
  };

  const openNextSection = async () => {
    if (!nextSectionId) return;
    setSubmissionError('');
    setOpeningNextSection(true);
    try {
      await onSelectSection(nextSectionId);
    } catch (reason) {
      setSubmissionError(
        reason instanceof Error ? reason.message : '无法进入下一节。',
      );
    } finally {
      setOpeningNextSection(false);
    }
  };

  const submit = async () => {
    if (!section.quiz) return;
    if (quizGovernanceBlocked) {
      setSubmissionError('这套验证题暂时不可用，请重新准备后再试。');
      return;
    }
    const firstUnanswered = answers.findIndex((answer) => answer.length === 0);
    if (firstUnanswered >= 0) {
      const unansweredCount = answers.filter((answer) => answer.length === 0).length;
      setResult(null);
      setSubmissionError(`还有 ${unansweredCount} 道题未作答，请先完成第 ${firstUnanswered + 1} 题。`);
      return;
    }

    const answerFingerprint = JSON.stringify(answers);
    let requestId = crypto.randomUUID();
    try {
      const stored = JSON.parse(localStorage.getItem(quizRequestStorageKey) || 'null');
      if (
        stored &&
        typeof stored.id === 'string' &&
        stored.id.length >= 8 &&
        stored.id.length <= 128 &&
        stored.answerFingerprint === answerFingerprint
      ) {
        requestId = stored.id;
      } else {
        localStorage.setItem(
          quizRequestStorageKey,
          JSON.stringify({ id: requestId, answerFingerprint }),
        );
      }
    } catch {
      // Storage may be unavailable or contain a legacy request key. A fresh
      // request id keeps the current answer set independent from stale retries.
    }

    setResult(null);
    try {
      sessionStorage.removeItem(quizResultStorageKey);
    } catch {
      // Submission does not depend on browser session storage.
    }
    setSubmissionError('');
    setSubmitting(true);
    try {
      const value = await api.quiz(
        section.id,
        section.quiz.id,
        answers,
        requestId,
      );
      const reviewValue = {...value, questions: section.quiz.questions};
      setResult(reviewValue);
      requestAnimationFrame(onSubmissionComplete);
      try {
        sessionStorage.setItem(quizResultStorageKey, JSON.stringify(reviewValue));
      } catch {
        // The current in-memory review remains available for this render.
      }
      setRemediationReady(false);
      localStorage.removeItem(quizDraftKey);
      localStorage.removeItem(quizRequestStorageKey);
      void monitorTasks(value.workflowTasks, value.passed).catch(() => {
        setWorkflowRunning(false);
        setSubmissionError('暂时无法更新后续内容，请稍后再试。');
      });
      void (async () => {
        try {
          if (value.passed) {
            const next = await api.section(section.id);
            onSectionChange(next);
          }
          await onRefreshSeries();
        } catch {
          setSubmissionError('目录进度暂未同步，请刷新后重试。');
        }
      })();
    } catch (reason) {
      setSubmissionError(
        reason instanceof Error && reason.name !== 'TypeError'
          ? reason.message
          : '提交失败，请检查服务连接后重试。',
      );
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="quiz-view">
      <p className="eyebrow">完成验证后解锁下一节</p>
      <h2>小节验证</h2>
      <p className="quiz-rule">答对至少 80%，且关键题达到要求即可继续；错题会用于安排重点巩固。</p>
      {quizGovernanceBlocked && (
        <aside className="quiz-governance-notice" role="alert">
          <b>这节内容需要升级后才能验证</b>
          <p>这是旧版本内容，需要升级后才能验证。</p>
          {section.bestScore === 0 && section.totalScore === 0 && (
            <button className="secondary-button" type="button" onClick={onUpgrade}>
              升级本节内容与验证
            </button>
          )}
        </aside>
      )}
      {quizGovernanceBlocked ? null : result ? (
        <QuizReview
          section={section}
          result={result}
          remediationReady={remediationAvailable}
          openingRemediation={openingRemediation}
          eligibleUnderCurrentPolicy={eligibleUnderCurrentPolicy}
          reassessing={reassessing}
          nextSectionTask={nextSectionTask || null}
          nextSectionId={nextSectionId}
          nextSectionNumber={nextSectionLocation
            ? `${nextSectionLocation.chapter.position}.${nextSectionLocation.section.position}`
            : ''}
          nextSectionTitle={nextSectionLocation?.section.title || ''}
          openingNextSection={openingNextSection}
          workflowRunning={workflowRunning}
          workflowTasks={workflowTasks}
          retryingTasks={retryingTasks}
          onReviewContent={onReviewContent}
          onOpenQa={onOpenQa}
          onOpenRemediation={openRemediation}
          onReassess={reassessAttempt}
          onOpenNextSection={openNextSection}
          onRetryTasks={retryFailedTasks}
          onFeedback={onFeedback}
        />
      ) : (
        <>
          {section.remediations.map((item) => (
            <section className="remediation-card" key={item.id}>
              <span>错题补充教学</span>
              {item.blocks.map((block) => (
                <div key={block.id}>
                  <h3>{block.heading}</h3>
                  <LessonBlockBody block={block} />
                </div>
              ))}
            </section>
          ))}
          {section.quiz?.questions.map((question, questionIndex) => (
            <fieldset className="question-card" key={`${section.quiz?.id}-${questionIndex}`}>
              <legend>
                <span className="question-number">{questionIndex + 1}</span>
                <b>{question.prompt}</b>
                <span className="question-badges">
                  <em className={`question-kind ${question.selectionMode}`}>{question.selectionMode === 'multiple' ? '多选' : '单选'}</em>
                  {question.core && <em className="core-question">核心题</em>}
                </span>
              </legend>
              {question.options.map((option, optionIndex) => (
                <label key={optionIndex}>
                  <input
                    type={question.selectionMode === 'multiple' ? 'checkbox' : 'radio'}
                    name={`question-${section.quiz?.id}-${questionIndex}`}
                    checked={answers[questionIndex]?.includes(optionIndex)}
                    onChange={(event) => setAnswers((current) => current.map((value, index) => {
                      if (index !== questionIndex) return value;
                      if (question.selectionMode !== 'multiple') return event.target.checked ? [optionIndex] : [];
                      return event.target.checked
                        ? [...value, optionIndex]
                        : value.filter((item) => item !== optionIndex);
                    }))}
                  />
                  <span className="question-option-letter">
                    {String.fromCharCode(65 + optionIndex)}
                  </span>
                  <span className="question-option-text">{option}</span>
                </label>
              ))}
            </fieldset>
          ))}
          <button
            className="primary-button large"
            disabled={submitting}
            aria-describedby="quiz-submission-feedback"
            onClick={submit}
          >
            {submitting ? '正在评分…' : '提交验证'}
          </button>
        </>
      )}
      <div id="quiz-submission-feedback" aria-live="polite">
        {submissionError && <p className="result failure" role="alert">{submissionError}</p>}
      </div>
      {section.askMeUnlocked && !result && <AskMePanel sectionId={section.id} />}
    </div>
  );
}

function QuizQuestionFeedback({
  sectionId,
  attemptId,
  questionIndex,
  question,
}: {
  sectionId: string;
  attemptId: string;
  questionIndex: number;
  question: Question;
}) {
  const [open, setOpen] = useState(false);
  const [feedbackType, setFeedbackType] = useState<'unclear' | 'inaccurate'>('unclear');
  const [message, setMessage] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [status, setStatus] = useState('');
  const submissionRef = useRef({ payload: '', key: '' });

  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (next) {
      telemetry.track('feedback_opened', {
        view: 'learn',
        entityType: 'section',
        entityId: sectionId,
        properties: {
          scope: 'quiz_question',
          questionIndex,
          questionId: question.id || `${attemptId}:${questionIndex}`,
        },
      });
    }
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setSubmitting(true);
    setStatus('');
    const payload = {
      scope: 'quiz_question',
      feedbackType,
      message,
      pagePath: window.location.pathname,
      view: 'learn',
      sectionId,
      attemptId,
      questionIndex,
    };
    const serializedPayload = JSON.stringify(payload);
    if (submissionRef.current.payload !== serializedPayload) {
      submissionRef.current = { payload: serializedPayload, key: crypto.randomUUID() };
    }
    try {
      await api.submitFeedback(payload, submissionRef.current.key);
      setSubmitted(true);
      setStatus(feedbackType === 'inaccurate'
        ? '已记录为待复核问题。确认前本次成绩保持不变；若确认有误，会更正受影响的学习记录。'
        : '已记录你没理解的地方，供后续改进这道题的解析和讲解。');
    } catch (reason) {
      setStatus(reason instanceof Error ? reason.message : '反馈没有提交成功，请稍后重试。');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <section className={`question-feedback ${open ? 'is-open' : ''} ${submitted ? 'is-received' : ''}`}>
      <button
        type="button"
        className="question-feedback-toggle"
        aria-expanded={open}
        onClick={toggle}
      >
        <span aria-hidden="true">?</span>
        <b>{submitted ? '反馈已收到' : '对这道题有疑问？'}</b>
        <small>{submitted ? '查看处理说明' : '告诉我们是没理解，还是题目可能有误'}</small>
        <i aria-hidden="true">⌄</i>
      </button>
      {open && (
        submitted ? (
          <p className="question-feedback-receipt" role="status">{status}</p>
        ) : (
          <form onSubmit={submit}>
            <fieldset>
              <legend>你的疑问更接近哪一种？</legend>
              <label className={feedbackType === 'unclear' ? 'selected' : ''}>
                <input
                  type="radio"
                  name={`question-feedback-${attemptId}-${questionIndex}`}
                  checked={feedbackType === 'unclear'}
                  onChange={() => setFeedbackType('unclear')}
                />
                <span><b>我没理解解析</b><small>答案可能没错，但解释没有帮我想明白</small></span>
              </label>
              <label className={feedbackType === 'inaccurate' ? 'selected' : ''}>
                <input
                  type="radio"
                  name={`question-feedback-${attemptId}-${questionIndex}`}
                  checked={feedbackType === 'inaccurate'}
                  onChange={() => setFeedbackType('inaccurate')}
                />
                <span><b>题目或答案可能有误</b><small>题干有歧义、选项不成立，或标准答案值得复核</small></span>
              </label>
            </fieldset>
            <label className="question-feedback-note">
              补充说明（可选）
              <textarea
                value={message}
                maxLength={4000}
                placeholder={feedbackType === 'inaccurate'
                  ? '例如：题干没有说明从银行还是模型公司的角度看收入…'
                  : '例如：我不明白为什么我的选项不成立…'}
                onChange={(event) => setMessage(event.target.value)}
                disabled={submitting}
              />
            </label>
            {status && <p className="question-feedback-error" role="alert">{status}</p>}
            <footer>
              <small>{feedbackType === 'inaccurate'
                ? '提交后标记为待复核，不会由模型直接改题或立即改分。'
                : '这条反馈只改进讲解，不会改变本次作答记录。'}</small>
              <button className="secondary-button" disabled={submitting}>
                {submitting ? '正在发送…' : '发送反馈'}
              </button>
            </footer>
          </form>
        )
      )}
    </section>
  );
}

function QuizReview({
  section,
  result,
  remediationReady,
  openingRemediation,
  eligibleUnderCurrentPolicy,
  reassessing,
  nextSectionTask,
  nextSectionId,
  nextSectionNumber,
  nextSectionTitle,
  openingNextSection,
  workflowRunning,
  workflowTasks,
  retryingTasks,
  onReviewContent,
  onOpenQa,
  onOpenRemediation,
  onReassess,
  onOpenNextSection,
  onRetryTasks,
  onFeedback,
}: {
  section: Section;
  result: QuizResult;
  remediationReady: boolean;
  openingRemediation: boolean;
  eligibleUnderCurrentPolicy: boolean;
  reassessing: boolean;
  nextSectionTask: LearningTask | null;
  nextSectionId: string | null;
  nextSectionNumber: string;
  nextSectionTitle: string;
  openingNextSection: boolean;
  workflowRunning: boolean;
  workflowTasks: LearningTask[];
  retryingTasks: boolean;
  onReviewContent: (blockId?: string) => void;
  onOpenQa: () => void;
  onOpenRemediation: () => Promise<void>;
  onReassess: () => Promise<void>;
  onOpenNextSection: () => Promise<void>;
  onRetryTasks: () => Promise<void>;
  onFeedback: () => void;
}) {
  const questions = result.questions || section.quiz?.questions || [];
  const wrongIndexes = result.results
    .map((item, index) => (item.correct ? -1 : index))
    .filter((index) => index >= 0);
  const correctIndexes = result.results
    .map((item, index) => (item.correct ? index : -1))
    .filter((index) => index >= 0);
  const optionSummary = (questionIndex: number, indexes: number[]) => {
    const question = questions[questionIndex];
    if (!question || !indexes.length) return '未选择';
    return indexes.map((index) => (
      `${String.fromCharCode(65 + index)}. ${question.options[index] || '未知选项'}`
    )).join('；');
  };
  const jumpToWrong = (questionIndex: number) => {
    document.getElementById(`quiz-review-${result.attemptId}-${questionIndex}`)
      ?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };
  const relevantWorkflowTasks = workflowTasks.filter((task) => (
    !task.triggerId || task.triggerId === result.attemptId
  ));
  const failedWorkflowTasks = relevantWorkflowTasks.filter((task) => task.status === 'failed');
  const hasRetryableFailure = failedWorkflowTasks.some((task) => task.retryable);
  const workflowPending = workflowRunning || relevantWorkflowTasks.some(
    (task) => task.status === 'pending' || task.status === 'running',
  );
  const noteTask = relevantWorkflowTasks.find((task) => task.type === 'note_generation');
  const remediationTask = relevantWorkflowTasks.find(
    (task) => task.type === 'remediation_generation',
  );
  const failedTaskLabels = Array.from(new Set(failedWorkflowTasks.map((task) => {
    if (task.type === 'note_generation') return '个人笔记';
    if (task.type === 'next_section_preload') return '下一节';
    if (task.type === 'remediation_generation') return '补充教学';
    return '后续内容';
  })));
  const aiFailedTask = failedWorkflowTasks.find(isAiGenerationFailure);
  const failedTaskSummary = failedTaskLabels.length
    ? generationFailureMessage(aiFailedTask, failedTaskLabels.join('和'))
    : '后续内容暂未准备完成。';
  const nextSectionReady = Boolean(
    result.passed && nextSectionTask?.status === 'succeeded' && nextSectionId,
  );
  const hasBlockingWorkflowFailure = (
    failedWorkflowTasks.length > 0 && !nextSectionReady
  );
  const followupReady = (
    eligibleUnderCurrentPolicy ||
    remediationReady ||
    nextSectionReady ||
    (result.passed && !workflowPending && failedWorkflowTasks.length === 0)
  );

  return (
    <section className="quiz-review" aria-labelledby="quiz-review-title">
      <header className={result.passed ? 'passed' : 'failed'}>
        <p className="eyebrow">评分已完成，无需等待 AI</p>
        <h3 id="quiz-review-title">
          {result.passed ? '本次验证已通过' : `有 ${wrongIndexes.length} 道题需要回看`}
        </h3>
        <strong>{result.score}<small> / {result.total}</small></strong>
        <p>
          {result.passed ? '查看解析或回到正文。' : '先查看下面的错题解析。'}
        </p>
        <button type="button" className="quiet-button quiz-review-ask-ai" onClick={onOpenQa}>
          Ask AI 追问解析
        </button>
      </header>

      <KnowledgeSettlementCard settlement={result.knowledgeSettlement} />

      {wrongIndexes.length > 0 && (
        <nav className="wrong-question-nav" aria-label="错题导航">
          <span>跳转错题</span>
          {wrongIndexes.map((questionIndex) => (
            <button key={questionIndex} onClick={() => jumpToWrong(questionIndex)}>
              第 {questionIndex + 1} 题
            </button>
          ))}
        </nav>
      )}

      <div className="wrong-question-list">
        {wrongIndexes.map((questionIndex) => {
          const question = questions[questionIndex];
          const review = result.results[questionIndex];
          if (!question || !review) return null;
          const evidenceBlocks = (question.evidenceBlockIds || [])
            .map((blockId) => section.content?.blocks.find((block) => block.id === blockId))
            .filter((block): block is Block => Boolean(block));
          return (
            <article
              className="wrong-question-card"
              id={`quiz-review-${result.attemptId}-${questionIndex}`}
              key={questionIndex}
            >
              <div className="wrong-question-heading">
                <span>第 {questionIndex + 1} 题</span>
                <em>{question.core ? '核心题 · ' : ''}答错</em>
              </div>
              <h4>{question.prompt}</h4>
              <p className="review-objective">考查目标：{review.objective}</p>
              <div className="answer-comparison">
                <div className="selected-answer">
                  <span>你的答案</span>
                  <p>{optionSummary(questionIndex, review.selectedOptions || [])}</p>
                  {(review.incorrectOptions || []).length > 0 && <small>包含错选项</small>}
                </div>
                <div className="correct-answer">
                  <span>正确答案</span>
                  <p>{optionSummary(questionIndex, review.correctOptions || [])}</p>
                  {(review.missedOptions || []).length > 0 && <small>有 {(review.missedOptions || []).length} 项漏选</small>}
                </div>
              </div>
              <div className="question-explanation">
                <b>基础解析</b>
                <div className="content-markdown">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{review.explanation}</ReactMarkdown>
                </div>
              </div>
              {evidenceBlocks.length > 0 ? (
                <aside className="review-evidence" aria-label={`第 ${questionIndex + 1} 题的正文依据`}>
                  <span>正文依据</span>
                  <div>
                    {evidenceBlocks.map((block) => (
                      <button key={block.id} type="button" onClick={() => onReviewContent(block.id)}>
                        <i aria-hidden="true">§</i>
                        {block.heading}
                      </button>
                    ))}
                  </div>
                </aside>
              ) : (
                <button className="quiet-button" onClick={() => onReviewContent()}>回看本节正文</button>
              )}
              <QuizQuestionFeedback
                sectionId={section.id}
                attemptId={result.attemptId}
                questionIndex={questionIndex}
                question={question}
              />
            </article>
          );
        })}
      </div>

      {correctIndexes.length > 0 && (
        <details className="correct-question-review">
          <summary>查看答对的 {correctIndexes.length} 道题</summary>
          {correctIndexes.map((questionIndex) => {
            const question = questions[questionIndex];
            const review = result.results[questionIndex];
            if (!question || !review) return null;
            return (
              <div key={questionIndex}>
                <b>第 {questionIndex + 1} 题 · {question.prompt}</b>
                <p>{optionSummary(questionIndex, review.correctOptions || [])}</p>
                <small>{review.explanation}</small>
              </div>
            );
          })}
        </details>
      )}

      <div
        className={`remediation-readiness ${nextSectionReady ? 'next-section-ready' : hasBlockingWorkflowFailure ? 'failed' : followupReady ? 'ready' : ''}`}
        aria-live="polite"
        aria-atomic="true"
      >
        {result.passed ? (
          nextSectionReady ? (
            <>
              {noteTask?.status === 'failed' && (
                <aside className="quiz-next-note-warning">
                  <small>{generationFailureMessage(noteTask, '个人笔记')}</small>
                  {noteTask.retryable && (
                    <button
                      type="button"
                      className="quiet-button"
                      disabled={retryingTasks || workflowRunning}
                      onClick={onRetryTasks}
                    >
                      {retryingTasks ? '正在重新准备…' : '重新准备笔记'}
                    </button>
                  )}
                </aside>
              )}
              <div className={`quiz-completion-choices ${section.askMeUnlocked ? 'has-askme' : ''}`}>
                {section.askMeUnlocked && (
                  <AskMePanel sectionId={section.id} compactEntry />
                )}
                <nav className="quiz-next-navigation" aria-label="继续学习">
                  <button
                    className="quiz-next-section-card"
                    disabled={openingNextSection}
                    onClick={onOpenNextSection}
                  >
                    <small>{openingNextSection ? '正在进入' : '继续学习 · 下一节'}</small>
                    <strong>
                      {nextSectionNumber && <span>{nextSectionNumber}</span>}
                      {nextSectionTitle || '继续下一节'}
                      <i aria-hidden="true">→</i>
                    </strong>
                  </button>
                </nav>
              </div>
              <footer className="quiz-review-feedback">
                <p>这节内容帮你解决问题了吗？</p>
                <button type="button" onClick={onFeedback}>
                  告诉我们哪里还不清楚 <span aria-hidden="true">→</span>
                </button>
              </footer>
            </>
          ) : failedWorkflowTasks.length > 0 ? (
            <>
              <span>准备失败</span>
              <small>{failedTaskSummary}</small>
              {hasRetryableFailure && (
                <button
                  type="button"
                  className="secondary-button"
                  disabled={retryingTasks || workflowRunning}
                  onClick={onRetryTasks}
                >
                  {retryingTasks ? '正在重新准备…' : '重新准备'}
                </button>
              )}
            </>
          ) : nextSectionTask ? (
            <>
              <span><i />正在准备下一节</span>
            </>
          ) : noteTask && (workflowPending || noteTask.status !== 'succeeded') ? (
            <>
              <span><i />正在整理个人笔记</span>
            </>
          ) : (
            <>
              <span>本节已验证</span>
              <button className="secondary-button" onClick={() => onReviewContent()}>返回正文</button>
            </>
          )
        ) : eligibleUnderCurrentPolicy ? (
          <>
            <span>当前成绩已达到本节通过标准</span>
            <small>确认后会记录本节通过，并开始准备下一节。</small>
            <button
              className="primary-button"
              disabled={reassessing}
              onClick={onReassess}
            >
              {reassessing ? '正在确认并准备…' : '确认通过并准备下一节'}
            </button>
          </>
        ) : remediationReady ? (
          <>
            <span>补充教学与新题已准备好</span>
            <small>先回看这次错题对应的内容，再用一组新题重新验证。</small>
            <button
              className="primary-button"
              disabled={openingRemediation}
              onClick={onOpenRemediation}
            >
              {openingRemediation ? '正在打开…' : '开始补充教学与新题'}
            </button>
          </>
        ) : remediationTask?.status === 'failed' || failedWorkflowTasks.length > 0 ? (
          <>
            <span>准备失败</span>
            <small>{failedTaskSummary}</small>
            {hasRetryableFailure && (
              <button
                type="button"
                className="secondary-button"
                disabled={retryingTasks || workflowRunning}
                onClick={onRetryTasks}
              >
                {retryingTasks ? '正在重新准备…' : '重新准备'}
              </button>
            )}
          </>
        ) : (
          <>
            <span><i />正在准备补充教学与新题</span>
            <small>准备完成后，这里会自动出现下一步，无需刷新页面。</small>
          </>
        )}
      </div>
    </section>
  );
}

function KnowledgeSettlementCard({
  settlement,
}: {
  settlement?: KnowledgeSettlement | null;
}) {
  if (!settlement?.updates.length) return null;
  const priority = {
    stage_up: 0,
    evidence_added: 1,
    reactivated: 2,
    needs_reactivation: 3,
    confirmed: 4,
  } as const;
  const updates = [...settlement.updates].sort(
    (left, right) => priority[left.change] - priority[right.change],
  );
  const stateLabel = {
    stage_up: '能力进阶',
    evidence_added: '证据增加',
    reactivated: '重新唤醒',
    needs_reactivation: '等待唤醒',
    confirmed: '能力确认',
  } as const;

  return (
    <section className="knowledge-settlement" aria-labelledby="knowledge-settlement-title">
      <div className="knowledge-settlement-heading">
        <div>
          <span>能力画像</span>
          <h3 id="knowledge-settlement-title">本节形成的能力证据</h3>
        </div>
        <small>阶段、证据稳固度与当前可用状态分别计算</small>
      </div>
      <div className="knowledge-settlement-list">
        {updates.map((update) => {
          // Historical/demo evidence predates the display label. Keep a
          // completed verification readable instead of letting that legacy
          // projection blank the entire result page.
          const stageLabel = update.after.stageLabel || '能力记录';
          const beforeStageLabel = update.before.stageLabel || '尚未分级';
          const tier = stageLabel.split(' · ')[0];
          const stageChanged = update.change === 'stage_up';
          return (
            <article
              className={`knowledge-rank-update ${stageChanged ? 'rank-up' : update.change}`}
              data-rank={update.after.stage}
              key={update.capabilityRevisionId}
            >
              <div
                className="knowledge-rank-seal"
                aria-label={`当前能力阶段：${stageLabel}`}
              >
                <small>{stageChanged ? 'NEW STAGE' : 'CAPABILITY'}</small>
                <strong>{tier}</strong>
                <i aria-hidden="true">能</i>
              </div>
              <div className="knowledge-rank-copy">
                <div className="knowledge-rank-meta">
                  <span>{stateLabel[update.change]}</span>
                  <em>{update.label}</em>
                </div>
                <h4>
                  {stageChanged && update.before.stage !== 'unranked' && (
                    <small>{beforeStageLabel}</small>
                  )}
                  {stageChanged && update.before.stage !== 'unranked' && <i>→</i>}
                  {stageLabel}
                </h4>
                <p>{update.message}</p>
                <div className="knowledge-capability-scope">
                  <span>稳定能力</span>
                  <b>{update.after.label}</b>
                  {update.after.nextCriterion && <small>下一阶要求：{update.after.nextCriterion}</small>}
                </div>
                <div
                  className="knowledge-evidence-stars"
                  aria-label={`当前 ${update.after.independentEvidenceCount} 次独立证据`}
                >
                  <span>证据稳固度</span>
                  <b>{update.after.independentEvidenceCount}</b>
                  <small>次独立验证 · {update.after.stabilityDays} 天跨度</small>
                </div>
              </div>
            </article>
          );
        })}
      </div>
    </section>
  );
}

const NOTE_LIST_FIELDS: { key: keyof NoteContent; label: string; hint: string }[] = [
  { key: 'core_mechanism', label: '核心机制', hint: '每行写一个机制' },
  { key: 'personal_gaps', label: '仍需留意', hint: '每行写一个需要继续巩固的点' },
  { key: 'boundaries', label: '适用边界', hint: '每行写一个边界条件' },
  { key: 'practice_checks', label: '自检方法', hint: '每行写一个自检问题或练习' },
  { key: 'sources', label: '参考来源', hint: '每行写一个来源' },
  { key: 'unresolved', label: '尚未解决', hint: '每行写一个待解决问题' },
];

function noteVerificationLabel(annotation: NoteVerificationAnnotation): string {
  switch (annotation.claimStatus) {
    case 'retained':
      return '掌握稳固';
    case 'verified_delayed':
      return annotation.retentionRounds >= 2 ? '掌握稳固' : '复习验证通过';
    case 'verified_immediate':
      return '待复习验证';
    case 'contradicted':
      return '建议重新巩固';
    case 'learning':
      return '还需继续学习';
    case 'unobserved':
      return '尚未完成测验';
    default:
      if (annotation.retentionRounds >= 2) return '掌握稳固';
      if (annotation.retentionRounds >= 1) return '复习验证通过';
      return '学习情况待更新';
  }
}

function noteList(content: NoteContent, key: keyof NoteContent) {
  const value = content[key];
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string' && Boolean(item.trim()))
    : [];
}

function NoteContentView({ content, empty }: { content: NoteContent; empty: string }) {
  const solvedQuestion = typeof content.solved_question === 'string'
    ? content.solved_question.trim()
    : '';
  const populatedFields = NOTE_LIST_FIELDS
    .map((field) => ({ ...field, values: noteList(content, field.key) }))
    .filter((field) => field.values.length > 0);
  if (!solvedQuestion && populatedFields.length === 0) {
    return <p className="note-empty-layer">{empty}</p>;
  }
  return (
    <div className="note-content-grid">
      {solvedQuestion && (
        <section className="note-solved-question">
          <span>本节解决的问题</span>
          <p>{solvedQuestion}</p>
        </section>
      )}
      {populatedFields.map((field) => (
        <section key={String(field.key)}>
          <h4>{field.label}</h4>
          <ul>{field.values.map((item, index) => <li key={`${item}-${index}`}>{item}</li>)}</ul>
        </section>
      ))}
    </div>
  );
}

function Note({
  sectionId,
  note,
  onSaved,
}: {
  sectionId: string;
  note: NoteType;
  onSaved: (section: Section) => void;
}) {
  const initialContent = note.layers.userRevision?.content
    ?? note.layers.learningSummary?.content
    ?? note.aiContent;
  const [editing, setEditing] = useState<NoteContent>(initialContent);
  const [message, setMessage] = useState('');
  const summary = note.layers.learningSummary;
  const revision = note.layers.userRevision;
  useEffect(() => {
    setEditing(
      note.layers.userRevision?.content
      ?? note.layers.learningSummary?.content
      ?? note.aiContent,
    );
  }, [note]);
  const updateList = (key: keyof NoteContent, value: string) => {
    setEditing((current) => ({
      ...current,
      [key]: value.split('\n').map((item) => item.trim()).filter(Boolean),
    }));
  };
  const save = async () => {
    try {
      await api.note(sectionId, editing);
      onSaved(await api.section(sectionId));
      setMessage('我的笔记已保存。');
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : '保存失败');
    }
  };
  return (
    <div className="note-view">
      <p className="eyebrow">本节学习记录</p>
      <h2>学习笔记</h2>

      <article className="note-layer note-summary-layer">
        <header>
          <div><b>学习总结</b></div>
        </header>
        <NoteContentView content={summary?.content ?? note.aiContent} empty="本节还没有学习总结。" />
      </article>

      <section className="note-layer note-review-layer">
        <header>
          <div><b>复习补充</b></div>
          <em>{note.layers.reviewSupplements.length} 条</em>
        </header>
        {note.layers.reviewSupplements.length === 0 ? (
          <p className="note-empty-layer">暂无复习补充。</p>
        ) : (
          <div className="note-review-timeline">
            {note.layers.reviewSupplements.map((supplement, index) => (
              <article key={supplement.id}>
                <div>
                  <b>复习补充 {index + 1}</b>
                  <time dateTime={supplement.createdAt}>{new Date(supplement.createdAt).toLocaleDateString('zh-CN')}</time>
                </div>
                <NoteContentView content={supplement.content} empty="这次复习没有新增正文。" />
              </article>
            ))}
          </div>
        )}
      </section>

      <article className="note-layer note-user-layer">
        <header>
          <div><b>我的笔记</b></div>
          <em>{revision ? '已保存' : '未创建'}</em>
        </header>
        {revision && <NoteContentView content={revision.content} empty="我的笔记目前为空。" />}
        <div className="note-editor">
          <label>
            <span>我如何描述本节解决的问题</span>
            <textarea
              value={typeof editing.solved_question === 'string' ? editing.solved_question : ''}
              onChange={(event) => setEditing((current) => ({ ...current, solved_question: event.target.value }))}
            />
          </label>
          <div className="note-editor-grid">
            {NOTE_LIST_FIELDS.map((field) => (
              <label key={String(field.key)}>
                <span>{field.label}</span>
                <textarea
                  aria-label={field.label}
                  placeholder={field.hint}
                  value={noteList(editing, field.key).join('\n')}
                  onChange={(event) => updateList(field.key, event.target.value)}
                />
              </label>
            ))}
          </div>
          <button className="primary-button" onClick={save}>保存我的笔记</button>
        </div>
      </article>

      <aside className="note-verification" aria-label="本节掌握情况">
        <header><b>本节掌握情况</b><span>会根据测验与复习表现更新</span></header>
        <p>这里只显示 Slow 内的学习验证记录，不会改写上面的笔记。</p>
        {note.verificationAnnotations.length === 0 ? (
          <small>完成本节测验后，这里会显示学习情况。</small>
        ) : (
          <ul>
            {note.verificationAnnotations.map((annotation) => (
              <li key={annotation.assessmentTargetId}>
                <span><b>{annotation.objective}</b></span>
                <em>{noteVerificationLabel(annotation)}</em>
              </li>
            ))}
          </ul>
        )}
      </aside>
      {message && <p className="save-message">{message}</p>}
    </div>
  );
}

function AskMePanel({
  sectionId,
  compactEntry = false,
}: {
  sectionId: string;
  compactEntry?: boolean;
}) {
  const [discussion, setDiscussion] = useState<AskMeDiscussion | null>(null);
  const [answer, setAnswer] = useState('');
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [actioning, setActioning] = useState(false);
  const [confirmingFinish, setConfirmingFinish] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const turnRequest = useRef<{ fingerprint: string; id: string } | null>(null);
  const actionRequest = useRef<{ fingerprint: string; id: string } | null>(null);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError('');
    api.askMeDiscussion(sectionId)
      .then((value) => {
        if (active) setDiscussion(value);
      })
      .catch((reason) => {
        if (active) setError(reason instanceof Error ? reason.message : '无法恢复深入讨论。');
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => { active = false; };
  }, [sectionId]);

  useEffect(() => {
    if (!message || submitting) return;
    const timer = window.setTimeout(() => setMessage(''), 2400);
    return () => window.clearTimeout(timer);
  }, [message, submitting]);

  const start = async () => {
    setError('');
    setActioning(true);
    try {
      setDiscussion(await api.startAskMeDiscussion(sectionId));
      setMessage('主题已准备。');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法开始深入讨论。');
    } finally {
      setActioning(false);
    }
  };

  const activeTopic = discussion?.topics.find((topic) => topic.id === discussion.activeTopicId) || null;
  const activeTurns = discussion?.turns.filter((turn) => turn.topicId === activeTopic?.id) || [];
  const activeTopicIndex = activeTopic ? discussion?.topics.findIndex((topic) => topic.id === activeTopic.id) ?? -1 : -1;
  const isLastTopic = Boolean(discussion && activeTopicIndex === discussion.topics.length - 1);
  const displayedPrompt = activeTopic?.currentPrompt
    .replace(/^继续围绕“[^”]+”(?:说明)?[：:]\s*/, '')
    .trim() || '';

  const submit = async () => {
    if (!discussion || !activeTopic || !answer.trim() || submitting) return;
    const normalizedAnswer = answer.trim();
    const fingerprint = JSON.stringify({
      sessionId: discussion.id,
      topicId: activeTopic.id,
      revision: discussion.revision,
      answer: normalizedAnswer,
    });
    if (!turnRequest.current || turnRequest.current.fingerprint !== fingerprint) {
      turnRequest.current = { fingerprint, id: crypto.randomUUID() };
    }
    setSubmitting(true);
    setError('');
    setMessage('回答已提交，正在评阅…');
    try {
      const next = await api.submitAskMeDiscussionTurn(
        sectionId,
        {
          sessionId: discussion.id,
          topicId: activeTopic.id,
          expectedRevision: discussion.revision,
          answer: normalizedAnswer,
        },
        turnRequest.current.id,
      );
      setDiscussion(next);
      setAnswer('');
      turnRequest.current = null;
      setMessage('回答已记录。');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '回答没有记录成功，请重试。');
      setMessage('');
    } finally {
      setSubmitting(false);
    }
  };

  const applyAction = async (action: 'next_topic' | 'pause' | 'resume' | 'finish') => {
    if (!discussion || actioning || submitting) return;
    const fingerprint = JSON.stringify({
      sessionId: discussion.id,
      revision: discussion.revision,
      action,
    });
    if (!actionRequest.current || actionRequest.current.fingerprint !== fingerprint) {
      actionRequest.current = { fingerprint, id: crypto.randomUUID() };
    }
    setActioning(true);
    setError('');
    try {
      const next = await api.applyAskMeDiscussionAction(
        sectionId,
        {
          sessionId: discussion.id,
          expectedRevision: discussion.revision,
          action,
        },
        actionRequest.current.id,
      );
      setDiscussion(next);
      setAnswer('');
      setConfirmingFinish(false);
      turnRequest.current = null;
      actionRequest.current = null;
      setMessage(action === 'next_topic'
        ? '已切换主题。'
        : action === 'pause'
          ? '已暂停。'
          : action === 'resume'
            ? '已恢复。'
            : '已结束。');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '操作没有完成，请重试。');
    } finally {
      setActioning(false);
    }
  };

  const evaluationLabel = (value: string) => ({
    strong: '理解稳固',
    partial: '基本到位',
    weak: '需要补强',
  }[value] || value);
  return (
    <div className={`askme-view ${compactEntry ? 'askme-choice-view' : ''} ${discussion ? 'has-discussion' : ''}`}>
      {(loading || discussion) && (!compactEntry || discussion) && (
        <header className="askme-intro">
          <div>
            <p className="eyebrow">隐藏关卡</p>
            <h2>Grill Me</h2>
          </div>
          {discussion && (
            <span>
              {discussion.status === 'completed'
                ? '已结束'
                : `主题 ${Math.max(activeTopicIndex + 1, 1)} / ${discussion.topics.length}`}
            </span>
          )}
        </header>
      )}

      {loading ? (
        compactEntry ? (
          <section className="askme-choice-card is-loading" aria-live="polite" aria-busy="true">
            <div>
              <p className="eyebrow">满分已解锁 · 可选挑战</p>
              <h2>Grill Me</h2>
            </div>
            <p>正在确认口试状态…</p>
          </section>
        ) : (
          <div className="askme-loading" aria-live="polite">正在恢复讨论…</div>
        )
      ) : !discussion ? (
        compactEntry ? (
          <section className="askme-choice-card" aria-labelledby="askme-choice-title">
            <div>
              <p className="eyebrow">满分已解锁 · 可选挑战</p>
              <h2 id="askme-choice-title">Grill Me</h2>
            </div>
            <p>用连续追问检验机制、边界与迁移；过程中只评估，不继续教学。</p>
            <button type="button" disabled={actioning} onClick={start}>
              {actioning ? '正在准备口试…' : '开始口试挑战'}
            </button>
          </section>
        ) : (
          <section className="askme-entry-card" aria-labelledby="askme-entry-title">
            <div className="askme-entry-copy">
              <p className="eyebrow">满分已解锁 · 可选挑战</p>
              <h2 id="askme-entry-title">Grill Me</h2>
              <p>不是再做一套题。考官会连续追问，确认你能不能把这一节讲清楚、判断边界，并用到新的情境。</p>
              <div className="askme-entry-actions">
                <button className="primary-button large" disabled={actioning} onClick={start}>
                  {actioning ? '正在准备…' : '开始口试挑战'}
                </button>
                <small>过程中只评估，不继续教学；可以随时暂停。</small>
              </div>
            </div>
            <ol className="askme-entry-probes" aria-label="口试探测顺序">
              <li><span>01</span><div><b>机制</b><small>解释为什么成立</small></div></li>
              <li><span>02</span><div><b>边界</b><small>判断何时不适用</small></div></li>
              <li><span>03</span><div><b>迁移</b><small>用到新的情境</small></div></li>
            </ol>
          </section>
        )
      ) : (
        <div className="askme-discussion">
          <nav className="askme-topic-tabs" aria-label="讨论主题">
            {discussion.topics.map((topic) => (
              <div className={`askme-topic-item is-${topic.status} ${topic.id === discussion.activeTopicId ? 'is-current' : ''}`} key={topic.id}>
                <span>{topic.position + 1}</span>
                <b>{topic.title}</b>
                {topic.status === 'closed' && <small>已完成</small>}
              </div>
            ))}
          </nav>

          <section className="askme-conversation">
            {discussion.status === 'completed' ? (
              <div className="askme-complete-card">
                <h3>本次讨论已结束</h3>
              </div>
            ) : activeTopic ? (
              <>
                <div className="askme-turn-list">
                  {activeTurns.map((turn) => (
                    <article className="askme-turn" key={turn.id}>
                      <div className="askme-question">
                        <span>考官追问</span>
                        <p>{turn.prompt}</p>
                      </div>
                      <div className="askme-answer">
                        <span>你的回答</span>
                        <p>{turn.answer}</p>
                      </div>
                      <div className={`askme-feedback is-${turn.evaluation}`}>
                        <header>
                          <span>本轮反馈</span>
                          <b>{evaluationLabel(turn.evaluation)}</b>
                        </header>
                        {turn.feedback.correctPoints.length > 0 && (
                          <section className="is-correct">
                            <h4>你答对了什么</h4>
                            <ul>{turn.feedback.correctPoints.map((item) => <li key={item}>{item}</li>)}</ul>
                          </section>
                        )}
                        {turn.feedback.issues.length > 0 && (
                          <section className="is-gap">
                            <h4>具体缺口</h4>
                            {turn.feedback.issues.map((issue, index) => (
                              <div key={`${issue.kind}-${index}`}>
                                {issue.answerExcerpt && <blockquote>“{issue.answerExcerpt}”</blockquote>}
                                <p>{issue.explanation}</p>
                              </div>
                            ))}
                          </section>
                        )}
                        <section className="is-suggestion">
                          <h4>改进建议</h4>
                          <ul>{turn.feedback.suggestions.map((item) => <li key={item}>{item}</li>)}</ul>
                        </section>
                      </div>
                    </article>
                  ))}
                </div>

                {discussion.status === 'paused' ? (
                  <div className="askme-paused-card">
                    <strong>讨论已暂停</strong>
                    <button className="primary-button" disabled={actioning} onClick={() => applyAction('resume')}>
                      {actioning ? '正在恢复…' : '继续讨论'}
                    </button>
                  </div>
                ) : (
                  <div className={`askme-composer ${submitting ? 'is-submitting' : ''}`} aria-busy={submitting}>
                    <label htmlFor={`askme-answer-${sectionId}`}>
                      <span>{activeTurns.length ? '下一问' : '当前问题'}</span>
                      <strong>{displayedPrompt}</strong>
                    </label>
                    <textarea
                      id={`askme-answer-${sectionId}`}
                      value={answer}
                      disabled={submitting || actioning}
                      aria-describedby={submitting ? `askme-review-status-${sectionId}` : undefined}
                      onChange={(event) => setAnswer(event.target.value)}
                      placeholder="写下你的判断、依据和不确定的地方…"
                    />
                    {submitting && (
                      <div
                        className="askme-reviewing-status"
                        id={`askme-review-status-${sectionId}`}
                        role="status"
                        aria-live="assertive"
                      >
                        <i aria-hidden="true" />
                        <span>
                          <b>评阅中</b>
                        </span>
                      </div>
                    )}
                    <div className="askme-composer-actions">
                      <button
                        type="button"
                        className="primary-button"
                        disabled={submitting || actioning || !answer.trim()}
                        aria-busy={submitting}
                        onClick={submit}
                      >
                        {submitting ? '正在评阅…' : '提交回答'}
                      </button>
                      {!isLastTopic && (
                        <button disabled={submitting || actioning} onClick={() => applyAction('next_topic')}>换个主题 →</button>
                      )}
                      <button
                        className="askme-finish"
                        disabled={submitting || actioning}
                        aria-expanded={confirmingFinish}
                        onClick={() => setConfirmingFinish((current) => !current)}
                      >
                        结束关卡
                      </button>
                    </div>
                    {confirmingFinish && (
                      <div className="askme-finish-confirm" role="alert">
                        <span>结束后不能继续本次讨论。</span>
                        <button disabled={actioning} onClick={() => setConfirmingFinish(false)}>继续讨论</button>
                        <button disabled={actioning} onClick={() => applyAction('finish')}>
                          {actioning ? '正在结束…' : '确认结束'}
                        </button>
                      </div>
                    )}
                  </div>
                )}
              </>
            ) : null}
          </section>
        </div>
      )}
      <div className="askme-live-status" aria-live="polite">
        {error && <p className="result failure">{error}</p>}
        {!error && message && <p>{message}</p>}
      </div>
    </div>
  );
}

function QaPanel({
  section,
  dailyMode,
  hidden,
  onClose,
  selectedBlockId,
  selectedQuote,
  onAnchor,
  onClearQuote,
  explanationRequest,
  onSectionChange,
  onStreamingChange,
}: {
  section: Section | null;
  dailyMode: DailyMode;
  hidden: boolean;
  onClose: () => void;
  selectedBlockId: string;
  selectedQuote: TextQuote | null;
  onAnchor: (id: string) => void;
  onClearQuote: () => void;
  explanationRequest: ExplanationRequest | null;
  onSectionChange: (section: Section) => void;
  onStreamingChange: (streaming: boolean) => void;
}) {
  const [threadId, setThreadId] = useState<string>();
  const [newQuestion, setNewQuestion] = useState(false);
  const [question, setQuestion] = useState('');
  const [messages, setMessages] = useState<QaExchange[]>([]);
  const [historyView, setHistoryView] = useState<QaHistory | null>(null);
  const [asking, setAsking] = useState(false);
  const [historyStatus, setHistoryStatus] = useState<'idle' | 'loading' | 'loaded' | 'error'>('idle');
  const [historyError, setHistoryError] = useState('');
  const [draftExplanation, setDraftExplanation] = useState<ExplanationRequest | null>(null);
  const [styleFeedback, setStyleFeedback] = useState<Record<string, 'helpful' | 'unclear'>>({});
  const [preferenceError, setPreferenceError] = useState('');
  const [adoptingExchange, setAdoptingExchange] = useState('');
  const [adoptedExchange, setAdoptedExchange] = useState('');
  const [confirmedPreference, setConfirmedPreference] = useState<Record<string, boolean>>({});
  const [latestAnswerWaiting, setLatestAnswerWaiting] = useState(false);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const messagesRef = useRef<HTMLDivElement>(null);
  const followLatestAnswerRef = useRef(true);
  const askingRef = useRef(false);
  const explanationRequestRef = useRef('');
  const draftExplanationRef = useRef<ExplanationRequest | null>(null);
  const selectedBlock =
    section?.content?.blocks.find((block) => block.id === selectedBlockId) ??
    section?.content?.blocks[0];
  const effectiveBlockId = selectedBlock?.id ?? selectedBlockId;

  useEffect(() => {
    onStreamingChange(asking && !hidden);
    return () => onStreamingChange(false);
  }, [asking, hidden, onStreamingChange]);

  useEffect(() => {
    if (selectedQuote) composerRef.current?.focus();
  }, [selectedQuote]);

  useEffect(() => {
    if (!explanationRequest || explanationRequestRef.current === explanationRequest.requestId) return;
    explanationRequestRef.current = explanationRequest.requestId;
    setDraftExplanation(explanationRequest);
    setQuestion(explanationRequest.displayQuestion);
    setNewQuestion(true);
    setPreferenceError('');
    requestAnimationFrame(() => composerRef.current?.focus());
  }, [explanationRequest]);

  useEffect(() => {
    draftExplanationRef.current = draftExplanation;
  }, [draftExplanation]);

  const scrollToLatestAnswer = (behavior: ScrollBehavior = 'smooth') => {
    const node = messagesRef.current;
    if (!node) return;
    followLatestAnswerRef.current = true;
    setLatestAnswerWaiting(false);
    node.scrollTo({ top: node.scrollHeight, behavior });
  };

  useEffect(() => {
    if (!followLatestAnswerRef.current) return;
    const frame = requestAnimationFrame(() => {
      if (followLatestAnswerRef.current) scrollToLatestAnswer('auto');
    });
    return () => cancelAnimationFrame(frame);
  }, [messages]);

  const loadHistory = async (contentVersionId?: string) => {
    if (!section?.content || historyStatus === 'loading') return;
    setHistoryStatus('loading');
    setHistoryError('');
    try {
      const history = await api.qaHistory(section.id, contentVersionId);
      setHistoryView(history);
      setMessages(qaHistoryExchanges(history));
      setThreadId(history.lastThreadId || undefined);
      if (history.readOnly) {
        setQuestion('');
        setNewQuestion(false);
        setDraftExplanation(null);
        onClearQuote();
      }
      if (!history.readOnly && !selectedQuote && !explanationRequest && history.lastThreadId) {
        const lastThread = history.threads.find((item) => item.threadId === history.lastThreadId);
        const lastBlockId = [...(lastThread?.messages || [])]
          .reverse()
          .find((message) => message.blockId)?.blockId;
        if (lastBlockId && section.content.blocks.some((block) => block.id === lastBlockId)) {
          onAnchor(lastBlockId);
        }
      }
      setHistoryStatus('loaded');
    } catch (reason) {
      setHistoryStatus('error');
      setHistoryError(
        reason instanceof Error
          ? reason.message
          : '暂时无法读取答疑。',
      );
    }
  };

  useEffect(() => {
    if (!hidden && section?.content && historyStatus === 'idle') {
      void loadHistory();
    }
  }, [hidden, section?.id, section?.content?.id, historyStatus]);

  const ask = async () => {
    if (askingRef.current || historyStatus === 'loading' || !section || !effectiveBlockId || !question.trim()) return;
    askingRef.current = true;
    const visibleQuestion = question.trim();
    const submittedQuestion = draftExplanation && visibleQuestion === draftExplanation.displayQuestion
      ? draftExplanation.question
      : (selectedQuote
      ? `请基于以下选中的正文回答。\n\n选中内容：${selectedQuote.text}\n\n问题：${visibleQuestion}`
      : visibleQuestion);
    const exchangeId = crypto.randomUUID();
    const explanationStyle = draftExplanation?.style;
    const preferenceRequestEventId = draftExplanation?.evidenceEventId;
    const explanationBlockKind = draftExplanation?.blockKind;
    const preferenceStatus = draftExplanation?.preferenceStatus === 'saved'
      ? 'saved'
      : draftExplanation
        ? 'unsaved'
        : undefined;
    followLatestAnswerRef.current = true;
    setLatestAnswerWaiting(false);
    setMessages((current) => [
      ...current,
      {
        id: exchangeId,
        blockId: effectiveBlockId,
        question: visibleQuestion,
        answer: '',
        relation: 'pending',
        status: 'streaming',
        explanationStyle,
        preferenceRequestEventId,
        explanationBlockKind,
        preferenceStatus,
      },
    ]);
    setQuestion('');
    setDraftExplanation(null);
    setAsking(true);
    try {
      const result = await api.askStream(
        section.id,
        effectiveBlockId,
        submittedQuestion,
        (delta) => setMessages((current) => current.map((message) => (
          message.id === exchangeId ? { ...message, answer: message.answer + delta } : message
        ))),
        newQuestion ? undefined : threadId,
        newQuestion ? 'new_question' : undefined,
        preferenceRequestEventId && explanationStyle && explanationBlockKind
          ? { preferenceRequestEventId, explanationStyle, explanationBlockKind }
          : undefined,
      );
      setThreadId(result.threadId);
      setMessages((current) => current.map((message) => (
        message.id === exchangeId
          ? { ...message, threadId: result.threadId, answerMessageId: result.answerMessageId, relation: result.relation, status: 'done' }
          : message
      )));
      setNewQuestion(false);
    } catch (reason) {
      setMessages((current) => current.map((message) => (
        message.id === exchangeId
          ? {
              ...message,
              answer: message.answer || (
                reason instanceof Error && reason.name !== 'TypeError'
                  ? reason.message
                  : '无法连接 API 服务，请确认后端已启动后重试。'
              ),
              status: 'error',
            }
          : message
      )));
    } finally {
      askingRef.current = false;
      setAsking(false);
    }
  };

  return (
    <aside className="qa-panel" id="section-qa-panel" aria-label="本节答疑" hidden={hidden}>
      <div className="qa-heading">
        <div>
          <span className="panel-label">答疑</span>
          <button className="panel-collapse-button" aria-label="收起答疑" onClick={onClose}>收起</button>
        </div>
        <h2>围绕当前小节追问</h2>
        {historyView && historyView.versions.length > 1 && (
          <label className="qa-version-select">
            <span>答疑所属正文</span>
            <select
              value={historyView.contentVersionId || historyView.currentContentVersionId || ''}
              disabled={historyStatus === 'loading' || asking}
              onChange={(event) => void loadHistory(event.target.value)}
            >
              {historyView.versions.map((version) => (
                <option key={version.contentVersionId} value={version.contentVersionId}>
                  {version.isCurrent ? '当前正文' : `旧版正文${version.contentVersion ? ` v${version.contentVersion}` : ''}`}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>
      {!section?.content ? (
        <div className="qa-empty">
          <span>?</span>
          <b>暂不可提问</b>
        </div>
      ) : (
        <>
          {historyView?.readOnly && (
            <div className="qa-version-notice">
              <b>这是旧版正文上的答疑</b>
              <p>记录会一直保留，但不能在旧版会话中继续追问。</p>
            </div>
          )}
          {!historyView?.readOnly && <div className="qa-context-bar">
            <span>当前段落</span>
            <select
              aria-label="当前答疑段落"
              value={effectiveBlockId}
              onChange={(event) => {
                setDraftExplanation(null);
                onAnchor(event.target.value);
              }}
            >
              {section.content.blocks.map((block, index) => (
                <option value={block.id} key={block.id}>{index + 1}. {block.heading}</option>
              ))}
            </select>
          </div>}
          {!historyView?.readOnly && selectedQuote && (
            <div className="selected-quote-card">
              <div>
                <span>已选内容</span>
                <button aria-label="移除已选内容" onClick={onClearQuote}>×</button>
              </div>
              <blockquote>{selectedQuote.text}</blockquote>
            </div>
          )}
          {!historyView?.readOnly && draftExplanation && (
            <div className="qa-explanation-request" role="status">
              <span aria-hidden="true">另解</span>
              <div>
                <b>{draftExplanation.label}</b>
                {draftExplanation.preferenceStatus !== 'saved' && (
                  <small>偏好未保存</small>
                )}
              </div>
              {draftExplanation.preferenceStatus !== 'saved' && (
                <button type="button" disabled={draftExplanation.preferenceStatus === 'saving'} onClick={async () => {
                  if (!section.content || draftExplanation.preferenceStatus === 'saving') return;
                  const retriedDraft = draftExplanation;
                  const retriedRequestId = retriedDraft.requestId;
                  setDraftExplanation((current) => current?.requestId === retriedRequestId
                    ? { ...current, preferenceStatus: 'saving' }
                    : current);
                  setPreferenceError('');
                  try {
                    await api.recordPreferenceEvidence({
                      eventId: retriedRequestId,
                      sectionId: section.id,
                      contentVersionId: section.content.id,
                      blockId: retriedDraft.blockId,
                      blockKind: retriedDraft.blockKind,
                      style: retriedDraft.style,
                      signal: 'requested',
                      customInstruction: retriedDraft.customInstruction,
                    });
                    setDraftExplanation((current) => current?.requestId === retriedRequestId ? {
                      ...current,
                      evidenceEventId: retriedRequestId,
                      preferenceStatus: 'saved',
                    } : current);
                  } catch (reason) {
                    setDraftExplanation((current) => current?.requestId === retriedRequestId
                      ? { ...current, preferenceStatus: 'unsaved' }
                      : current);
                    if (draftExplanationRef.current?.requestId === retriedRequestId) {
                      setPreferenceError(reason instanceof Error ? reason.message : '偏好未保存，请重试。');
                    }
                  }
                }}>{draftExplanation.preferenceStatus === 'saving' ? '保存中…' : '重试保存'}</button>
              )}
            </div>
          )}
          <div className="qa-message-stage">
            <div
              className="qa-messages"
              ref={messagesRef}
              role="region"
              aria-label="答疑记录"
              tabIndex={0}
              onScroll={(event) => {
                const node = event.currentTarget;
                const nearLatest = node.scrollHeight - node.scrollTop - node.clientHeight <= 56;
                if (nearLatest) {
                  followLatestAnswerRef.current = true;
                  setLatestAnswerWaiting(false);
                } else if (askingRef.current) {
                  followLatestAnswerRef.current = false;
                  setLatestAnswerWaiting(true);
                }
              }}
            >
            {historyStatus === 'loading' && (
              <div className="qa-history-state" role="status" aria-live="polite">
                <span className="streaming-dots" aria-hidden="true"><i /><i /><i /></span>
                <b>读取中</b>
              </div>
            )}
            {historyStatus === 'error' && messages.length === 0 && (
              <div className="qa-history-state error" role="alert">
                <b>暂时没有读到历史答疑</b>
                <p>{historyError}</p>
                <button type="button" onClick={() => void loadHistory()}>重新读取</button>
              </div>
            )}
            {historyStatus === 'loaded' && !historyView?.readOnly && messages.length === 0 && !draftExplanation && !selectedQuote && (
              <div className="qa-suggestion">
                <span>可以这样问</span>
                <button onClick={() => { setDraftExplanation(null); setQuestion(dailyMode === 'fast' ? '用一句结论和三个要点解释这段。' : '这个机制最容易被误解的地方是什么？'); }}>{dailyMode === 'fast' ? '用一句结论和三个要点解释这段。' : '这个机制最容易被误解的地方是什么？'}</button>
                <button onClick={() => { setDraftExplanation(null); setQuestion('它在什么边界条件下会失效？'); }}>它在什么边界条件下会失效？</button>
              </div>
            )}
            {messages.map((message) => (
              <div className={`qa-exchange ${message.status}`} key={message.id}>
                <div className="user-message"><span>你</span><p>{message.question}</p></div>
                <div className="assistant-message">
                  <span>S</span>
                  <div className="markdown-answer">
                    {message.answer ? (
                      <ReactMarkdown
                        remarkPlugins={[remarkGfm]}
                        components={{
                          a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noreferrer" />,
                          code: ({ node: _node, className, children, ...props }) => {
                            const language = /(?:^|\s)language-([^\s]+)/.exec(className || '')?.[1]?.toLowerCase();
                            const source = String(children).replace(/\n$/, '');
                            if (language === 'mermaid' && message.status === 'done') {
                              return <MermaidDiagram source={source} />;
                            }
                            return <code className={className} {...props}>{children}</code>;
                          },
                        }}
                      >
                        {message.answer}
                      </ReactMarkdown>
                    ) : (
                      <span className="qa-answer-pending" role="status">
                        <span className="streaming-dots" aria-hidden="true"><i /><i /><i /></span>
                        <small>已发送，正在回答，无需重复点击</small>
                      </span>
                    )}
                    {message.status === 'streaming' && message.answer && <span className="stream-caret" />}
                  </div>
                </div>
                {message.status === 'error' && !historyView?.readOnly && (
                  <div className="qa-error-actions">
                    <span>这次回答没有完成，问题不会重复提交。</span>
                    <button type="button" disabled={asking} onClick={() => {
                      setQuestion(message.question);
                      requestAnimationFrame(() => composerRef.current?.focus());
                    }}>重新填写</button>
                  </div>
                )}
                {message.status === 'done' && message.explanationStyle && !historyView?.readOnly && (
                  <div className="explanation-style-feedback">
                    <span>{message.preferenceRequestEventId ? '这次讲法怎么样？' : '偏好未保存'}</span>
                    {!message.preferenceRequestEventId && <p>本次回答不会计入长期偏好。</p>}
                    <div className="explanation-style-actions">
                      <button
                        className={styleFeedback[message.id] === 'helpful' ? 'selected' : ''}
                        disabled={!message.preferenceRequestEventId || Boolean(styleFeedback[message.id]) || adoptedExchange === message.id}
                        onClick={async () => {
                          if (message.preferenceRequestEventId && message.blockId && section.content) {
                            try {
                              await api.recordPreferenceEvidence({
                                eventId: crypto.randomUUID(), requestEventId: message.preferenceRequestEventId,
                                sectionId: section.id, contentVersionId: section.content.id,
                                blockId: message.blockId, blockKind: message.explanationBlockKind || 'text',
                                style: message.explanationStyle, signal: 'helpful',
                              });
                              setStyleFeedback((current) => ({ ...current, [message.id]: 'helpful' }));
                              telemetry.track('explanation_style_feedback', {
                                view: 'learn', entityType: 'section', entityId: section.id,
                                properties: { style: message.explanationStyle!, helpful: true },
                              });
                            } catch (reason) {
                              setPreferenceError(reason instanceof Error ? reason.message : '操作暂未保存，请重试。');
                            }
                          }
                        }}
                      >有帮助</button>
                      <button
                        className={styleFeedback[message.id] === 'unclear' ? 'selected' : ''}
                        disabled={!message.preferenceRequestEventId || Boolean(styleFeedback[message.id]) || adoptedExchange === message.id}
                        onClick={async () => {
                          if (message.preferenceRequestEventId && message.blockId && section.content) {
                            try {
                              await api.recordPreferenceEvidence({
                                eventId: crypto.randomUUID(), requestEventId: message.preferenceRequestEventId,
                                sectionId: section.id, contentVersionId: section.content.id,
                                blockId: message.blockId, blockKind: message.explanationBlockKind || 'text',
                                style: message.explanationStyle, signal: 'unclear',
                              });
                              setStyleFeedback((current) => ({ ...current, [message.id]: 'unclear' }));
                              telemetry.track('explanation_style_feedback', {
                                view: 'learn', entityType: 'section', entityId: section.id,
                                properties: { style: message.explanationStyle!, helpful: false },
                              });
                            } catch (reason) {
                              setPreferenceError(reason instanceof Error ? reason.message : '操作暂未保存，请重试。');
                            }
                          }
                        }}
                      >还是不清楚</button>
                      {message.explanationStyle !== 'custom' && (
                        <button
                          className={confirmedPreference[message.id] ? 'selected' : ''}
                          disabled={!message.preferenceRequestEventId || confirmedPreference[message.id]}
                          onClick={async () => {
                            if (!message.preferenceRequestEventId || !message.explanationStyle) return;
                            const dimensions = {
                              worked_example: 'example', diagram: 'diagram', analogy: 'analogy',
                              derivation: 'derivation', precise: 'precision', concise: 'concise',
                            } as const;
                            const dimension = dimensions[
                              message.explanationStyle as PresetExplanationStyle
                            ];
                            setPreferenceError('');
                            try {
                              await api.decideLearningPreference({
                                decisionKey: crypto.randomUUID(),
                                requestEventId: message.preferenceRequestEventId,
                                dimension,
                                scopeKind: 'global',
                                state: 'confirmed',
                              });
                              setConfirmedPreference((current) => ({ ...current, [message.id]: true }));
                              telemetry.track('explanation_style_remembered', {
                                view: 'learn', entityType: 'section', entityId: section.id,
                                properties: { style: message.explanationStyle },
                              });
                            } catch (reason) {
                              setPreferenceError(reason instanceof Error ? reason.message : '偏好暂未保存，请重试。');
                            }
                          }}
                        >
                          {confirmedPreference[message.id] ? '以后会优先这样讲' : '以后优先这样讲'}
                        </button>
                      )}
                      <button
                        className="replace"
                        disabled={adoptingExchange === message.id || adoptedExchange === message.id || Boolean(styleFeedback[message.id]) || !message.threadId || !message.answerMessageId || !message.preferenceRequestEventId || !section.content}
                        onClick={async () => {
                          const explanationStyle = message.explanationStyle;
                          if (!explanationStyle || !message.threadId || !message.answerMessageId || !message.preferenceRequestEventId || !message.blockId || !section.content) return;
                          setAdoptingExchange(message.id);
                          setPreferenceError('');
                          try {
                            await api.adoptPersonalPresentation(section.id, {
                              eventId: crypto.randomUUID(), requestEventId: message.preferenceRequestEventId,
                              contentVersionId: section.content.id, blockId: message.blockId,
                              blockKind: message.explanationBlockKind || 'text', style: explanationStyle,
                              threadId: message.threadId, answerMessageId: message.answerMessageId,
                            });
                            onSectionChange(await api.section(section.id));
                            setAdoptedExchange(message.id);
                          } catch (reason) {
                            setPreferenceError(reason instanceof Error ? reason.message : '暂时无法保存，请重试。');
                          } finally {
                            setAdoptingExchange('');
                          }
                        }}
                      >
                        {adoptingExchange === message.id ? '正在保存…' : adoptedExchange === message.id ? '已保留' : '保留为另一种讲法'}
                      </button>
                    </div>
                    {preferenceError && <p className="explanation-style-error" role="alert">{preferenceError}</p>}
                  </div>
                )}
              </div>
            ))}
            </div>
            {latestAnswerWaiting && (
              <button
                type="button"
                className="qa-latest-answer"
                onClick={() => scrollToLatestAnswer()}
              >
                回到最新回答 <span aria-hidden="true">↓</span>
              </button>
            )}
          </div>
          {!historyView?.readOnly ? <div className="qa-composer">
            <textarea
              ref={composerRef}
              value={question}
              disabled={historyStatus === 'loading'}
              onChange={(event) => setQuestion(event.target.value)}
              onKeyDown={(event) => {
                if (event.key !== 'Enter' || event.nativeEvent.isComposing) return;
                if (event.metaKey || event.ctrlKey || event.shiftKey) return;
                event.preventDefault();
                void ask();
              }}
              placeholder={selectedQuote ? '针对选中的内容输入问题…' : '基于当前段落继续追问…'}
            />
            <div>
              <div className="qa-composer-meta">
                <label><input type="checkbox" checked={newQuestion} onChange={(event) => setNewQuestion(event.target.checked)} /> 新问题</label>
                <span>Enter 发送 · ⌘/Ctrl + Enter 换行</span>
              </div>
              <button disabled={asking || historyStatus === 'loading' || !question.trim()} aria-busy={asking} onClick={() => void ask()}>
                {asking ? '回答中…' : '发送 ↑'}
              </button>
            </div>
          </div> : <div className="qa-archive-footer">切回“当前正文”后可以继续 Ask AI。</div>}
        </>
      )}
    </aside>
  );
}
