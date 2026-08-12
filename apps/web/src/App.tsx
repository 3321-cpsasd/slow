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
import { api, ApiError } from './api/client';
import { telemetry } from './telemetry';
import { ProfileOnboardingFlow } from './ProfileOnboardingFlow';
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
  Bootstrap,
  AccountExitReceipt,
  PrivacyState,
  RegistrationResult,
  Chapter,
  DueReviews,
  DailyMode,
  DailyModeDuration,
  DailyModeSource,
  LearningTask,
  LearningProfile,
  LearningPreferences,
  Note as NoteType,
  NoteContent,
  NoteVerificationAnnotation,
  QaHistory,
  QuizResult,
  ReviewResult,
  ReviewSession,
  Section,
  SectionSummary,
  Series,
  Shelf,
  ShelfCreateInput,
} from './model/types';

type View = 'home' | 'shelf' | 'learn' | 'profile';
type AppRoute =
  | { view: 'home' }
  | { view: 'profile'; section: 'profile' | 'account' }
  | { view: 'shelf'; shelfId: string }
  | { view: 'learn'; seriesId: string; sectionId: string | null };
type TextQuote = { text: string; blockId: string };
type SelectionPopup = TextQuote & { top: number; left: number };
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
  const [shelf, setShelf] = useState<Shelf | null>(null);
  const [series, setSeries] = useState<Series | null>(null);
  const [section, setSection] = useState<Section | null>(null);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [showAiSettings, setShowAiSettings] = useState(false);
  const [feedbackTarget, setFeedbackTarget] = useState<FeedbackTarget | null>(null);
  const [bookReplan, setBookReplan] = useState<{ book: Book; proposal: BookReplanProposal } | null>(null);
  const [learningQaOpen, setLearningQaOpen] = useState(false);
  const [showUserMenu, setShowUserMenu] = useState(false);
  const [exitReceipt, setExitReceipt] = useState<AccountExitReceipt | null>(null);
  const [profileSection, setProfileSection] = useState<'profile' | 'account'>(() => (
    new URLSearchParams(window.location.search).get('section') === 'account' ? 'account' : 'profile'
  ));
  const [preparingInitialSection, setPreparingInitialSection] = useState(false);
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

  const hasActiveDailyMode = () => Boolean(
    data?.dailyMode?.active
    && data.dailyMode.expiresAt
    && new Date(data.dailyMode.expiresAt).getTime() > Date.now(),
  );

  const dailyModePromptEnabled = data?.profile.preferences.dailyModePromptEnabled ?? true;

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
      setView('home');
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
    } else if (view === 'learn' && series) {
      telemetry.track('learning_viewed', { view: 'learn', entityType: 'series', entityId: series.id });
    } else if (view === 'profile') {
      telemetry.track('profile_viewed', { view: 'profile' });
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
      if (dailyModePromptEnabled && !(view === 'learn' && section && activityDailyMode)) {
        setDailyModeDialogOpen(true);
      }
      return;
    }
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
      } else if (dailyModePromptEnabled) {
        setDailyModeDialogOpen(true);
      }
    };
    const remaining = new Date(state.expiresAt).getTime() - Date.now();
    if (remaining <= 0) {
      expire();
      return;
    }
    const timer = window.setTimeout(expire, Math.min(remaining, 2_147_000_000));
    return () => window.clearTimeout(timer);
  }, [data?.dailyMode?.version, data?.dailyMode?.active, data?.dailyMode?.expiresAt, dailyModePromptEnabled, view, section?.id, activityDailyMode]);

  const run = async <T,>(label: string, action: () => Promise<T>) => {
    setBusy(label);
    setError('');
    try {
      return await action();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '操作失败');
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
    setView('shelf');
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
    if (dailyModePromptEnabled && !hasActiveDailyMode()) setDailyModeDialogOpen(true);
    void api.bootstrap()
      .then(setData)
      .catch((reason) => setError(reason instanceof Error ? reason.message : '主页刷新失败'));
  };

  const goHome = () => showHome('push');

  const returnToShelf = () => {
    if (!shelf) {
      goHome();
      return;
    }
    routeRequestVersion.current += 1;
    updateBrowserLocation(shelfPath(shelf.id), 'push');
    setShowUserMenu(false);
    setSeries(null);
    setSection(null);
    setActivityDailyMode(null);
    setDailyModeExpiredDuringActivity(false);
    setView('shelf');
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
  ) => {
    if (series) updateBrowserLocation(seriesPath(series.id, sectionId), historyMode);
    if (dailyModePromptEnabled && !hasActiveDailyMode()) {
      setPendingSectionId(sectionId);
      setDailyModeDialogOpen(true);
      if (section) return section;
    }
    const value = await run(
      '正在读取小节…',
      () => openAndTrackSection(sectionId),
    );
    setSection(value);
    return value;
  };

  const activateDailyMode = async (
    mode: DailyMode,
    duration: DailyModeDuration,
    source: DailyModeSource,
  ) => {
    setDailyModeBusy(true);
    setError('');
    try {
      const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
      const updated = await api.updateDailyMode(
        { dailyMode: mode, duration, timezone, source },
        `daily-mode-${crypto.randomUUID()}`,
      );
      setData((current) => current ? { ...current, dailyMode: updated } : current);
      if (source === 'header_toggle') {
        if (section) setActivityDailyMode(mode);
        setNotice(mode === 'fast'
          ? '已切换为快速阅读：当前只展示关键段落和 30 秒自检。'
          : '已切换为完整阅读：当前展示本节全部正文。');
      }
      setDailyModeExpiredDuringActivity(false);
      setDailyModeDialogOpen(false);
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
          (item) => item.status !== 'locked' && item.status !== 'completed',
        );
        if (match) return match.id;
        completedFallback ||= chapter.sections.find(
          (item) => item.status === 'completed',
        )?.id || null;
      }
    }
    return completedFallback;
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
          const targetSectionId = typeof task.result?.targetSectionId === 'string'
            ? task.result.targetSectionId
            : firstUsableSection(refreshed);
          let openedSection: Section | null = null;
          if (targetSectionId) {
            openedSection = await api.openSection(targetSectionId);
            if (!isCurrent()) return false;
          }
          setSeries(refreshed);
          if (targetSectionId && openedSection) {
            updateBrowserLocation(seriesPath(refreshed.id, targetSectionId), 'replace');
            applyOpenedSection(openedSection, targetSectionId);
            setSection(openedSection);
          }
          return true;
        }
        if (task.status === 'failed') {
          const refreshed = await api.series(value.id);
          if (!isCurrent()) return false;
          const fallbackSectionId = firstUsableSection(refreshed);
          let openedSection: Section | null = null;
          if (fallbackSectionId) {
            openedSection = await api.openSection(fallbackSectionId);
            if (!isCurrent()) return false;
          }
          setSeries(refreshed);
          if (fallbackSectionId && openedSection) {
            updateBrowserLocation(seriesPath(refreshed.id, fallbackSectionId), 'replace');
            applyOpenedSection(openedSection, fallbackSectionId);
            setSection(openedSection);
          }
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
  ) => {
    const requestVersion = historyMode === 'none'
      ? routeRequestVersion.current
      : ++routeRequestVersion.current;
    const value = await run('正在进入学习空间…', () => api.series(seriesId));
    if (requestVersion !== routeRequestVersion.current) return;
    updateBrowserLocation(seriesPath(seriesId, requestedSectionId), historyMode);
    setShelf(data?.shelves.find((item) => (
      item.series.some((candidate) => candidate.id === seriesId)
    )) || null);
    setSeries(value);
    setView('learn');
    if (
      value.initializationTask
      && !['failed', 'succeeded'].includes(value.initializationTask.status)
    ) {
      void monitorInitialSection(value);
      return;
    }
    const resumeSection = data?.resume?.sectionId;
    const resumeBelongsToSeries = resumeSection
      ? value.books.some((book) => book.chapters.some(
        (chapter) => chapter.sections.some(
          (item) => item.id === resumeSection && item.status !== 'locked',
        ),
      ))
      : false;
    const requestedBelongsToSeries = requestedSectionId
      ? value.books.some((book) => book.chapters.some(
        (chapter) => chapter.sections.some(
          (item) => item.id === requestedSectionId && item.status !== 'locked',
        ),
      ))
      : false;
    const initial = requestedBelongsToSeries
      ? requestedSectionId!
      : resumeBelongsToSeries
        ? resumeSection!
        : firstUsableSection(value);
    if (initial) {
      updateBrowserLocation(
        seriesPath(value.id, initial),
        historyMode === 'none' ? 'none' : 'replace',
      );
      await loadSection(initial, 'none');
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
      const first = updated.sections.find((item) => item.status !== 'locked');
      if (first) await loadSection(first.id);
    } finally {
      chapterGenerationRequests.current.delete(chapter.id);
      setGeneratingChapterId('');
    }
  };

  const startNextBook = async () => {
    if (!series) return;
    const nextBook = series.books.find(
      (book, index) => index > 0 && book.status !== 'locked' && book.status !== 'completed',
    );
    const firstChapter = nextBook?.chapters[0];
    if (!firstChapter) return;
    const availableSection = firstChapter.sections.find(
      (item) => item.status !== 'locked' && item.status !== 'completed',
    );
    if (availableSection) await loadSection(availableSection.id);
    else await openChapter(firstChapter);
  };

  const activateBook = async (book: Book) => {
    const proposal = await run(
      '正在根据你最近的学习情况调整下一本书…',
      () => api.replanBook(book.id),
    );
    setBookReplan({ book, proposal });
  };

  const generateSection = async (sectionId: string) => {
    const value = await run('正在准备并检查本节内容…', () => api.prepareSection(sectionId));
    setSection(value);
    await refreshSeries();
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
    if (route.view === 'shelf') {
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
      openShelf(targetShelf, 'none');
      return;
    }

    setBusy('正在恢复上次浏览位置…');
    try {
      const restoredSeries = await api.series(route.seriesId);
      if (requestVersion !== routeRequestVersion.current) return;
      const restoredShelf = data.shelves.find((item) => (
        item.series.some((candidate) => candidate.id === restoredSeries.id)
      )) || null;
      setShelf(restoredShelf);
      setSeries(restoredSeries);
      setView('learn');
      if (!route.sectionId) {
        setSection(null);
        if (
          restoredSeries.initializationTask
          && !['failed', 'succeeded'].includes(restoredSeries.initializationTask.status)
        ) {
          void monitorInitialSection(restoredSeries);
        }
        return;
      }
      const sectionCanOpen = restoredSeries.books.some((book) => book.chapters.some(
        (chapter) => chapter.sections.some(
          (item) => item.id === route.sectionId && item.status !== 'locked',
        ),
      ));
      if (!sectionCanOpen) {
        updateBrowserLocation(seriesPath(restoredSeries.id), 'replace');
        setSection(null);
        setError('这个小节尚未解锁，已返回当前系列目录。');
        return;
      }
      const restoredSection = await openAndTrackSection(route.sectionId);
      if (requestVersion === routeRequestVersion.current) setSection(restoredSection);
    } catch (reason) {
      if (requestVersion !== routeRequestVersion.current) return;
      updateBrowserLocation('/', 'replace');
      setView('home');
      setShelf(null);
      setSeries(null);
      setSection(null);
      setError(reason instanceof Error ? reason.message : '无法恢复这个浏览位置。');
    } finally {
      if (requestVersion === routeRequestVersion.current) setBusy('');
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
        onComplete={loadAuthenticatedState}
        onLogout={logout}
      />
    );
  }

  const showDailyModeDialog = Boolean(
    data?.dailyMode
    && dailyModePromptEnabled
    && (
      dailyModeDialogOpen
      || (
        !data.dailyMode.active
        && !(view === 'learn' && section && activityDailyMode)
      )
    ),
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
    <div className="app-shell">
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
        ) : (
          <small>一步一步，学成自己的书</small>
        )}
        <div className="header-actions">
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
              className="quiet-button"
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
        onDismissError={() => setError('')}
        onDismissNotice={() => setNotice('')}
      />
      <main className={view === 'learn' ? 'learn-main' : view === 'profile' ? 'profile-main' : 'marketing-main'}>
        {view === 'home' && (
          <Home
            data={data}
            dailyMode={data?.dailyMode?.dailyMode || data?.dailyMode?.lastDailyMode || 'slow'}
            onOpen={openShelf}
            onContinue={openSeries}
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
            profile={data!.profile}
            onBack={goHome}
            onCreate={async (body, idempotencyKey) => {
              const value = await run('AI 正在规划系列…', () => api.createPlan({ ...body, shelfId: shelf.id }, idempotencyKey));
              updateBrowserLocation(seriesPath(value.id), 'push');
              setSeries(value);
              setSection(null);
              setView('learn');
              void monitorInitialSection(value);
            }}
            onOpen={openSeries}
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
                      await run('正在确认学习路径…', () => api.confirmMilestonePath(series.id));
                      setData(await api.bootstrap());
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
              dailyMode={activityDailyMode || data?.dailyMode?.dailyMode || 'slow'}
              onSelectSection={loadSection}
              onGenerateSection={generateSection}
              onRegenerateSection={regenerateSection}
              onGenerateChapter={openChapter}
              onStartNextBook={startNextBook}
              onActivateBook={activateBook}
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
                  if (initial) await loadSection(initial);
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
              onQaVisibilityChange={setLearningQaOpen}
            />
          </>
        )}
      </main>
      {bookReplan && (
        <BookReplanDialog
          book={bookReplan.book}
          proposal={bookReplan.proposal}
          onClose={() => setBookReplan(null)}
          onConfirm={async () => {
            await run(
              '正在确认新章节目录…',
              () => api.confirmBookReplan(bookReplan.book.id, bookReplan.proposal.proposalId),
            );
            await refreshSeries();
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
          onSectionChange={setSection}
          onRefreshSeries={refreshSeries}
          onClose={() => setFeedbackTarget(null)}
        />
      )}
    </div>
  );
}

function BookReplanDialog({
  book,
  proposal,
  onClose,
  onConfirm,
}: {
  book: Book;
  proposal: BookReplanProposal;
  onClose: () => void;
  onConfirm: () => Promise<void>;
}) {
  const [confirming, setConfirming] = useState(false);
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
            <p className="eyebrow">下一本书 · 目录预览</p>
            <h2 id="book-replan-title">开始《{book.title}》前，先看一眼新目录</h2>
          </div>
          <button className="dialog-close" type="button" aria-label="关闭目录预览" disabled={confirming} onClick={onClose}>×</button>
        </header>

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

        <footer className="dialog-actions">
          <button className="quiet-button" type="button" disabled={confirming} onClick={onClose}>稍后再说</button>
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
  onClose,
}: {
  target: FeedbackTarget;
  view: View;
  onSectionChange: (section: Section) => void;
  onRefreshSeries: () => Promise<void>;
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
  const [submitted, setSubmitted] = useState(false);
  const [repairText, setRepairText] = useState('');
  const [repairFeedbackId, setRepairFeedbackId] = useState('');
  const [repairFailed, setRepairFailed] = useState(false);
  const [status, setStatus] = useState('');
  const dialogRef = useRef<HTMLElement | null>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const submittingRef = useRef(submitting);
  const onCloseRef = useRef(onClose);
  const closeTimerRef = useRef<number | undefined>(undefined);
  const submissionRef = useRef({ payload: '', key: '' });
  submittingRef.current = submitting;
  onCloseRef.current = onClose;

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
        if (!submittingRef.current) onCloseRef.current();
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
    setSubmitting(true);
    setSubmitted(true);
    setRepairFailed(false);
    setRepairText('');
    setStatus('');
    try {
      await api.streamFeedbackRepair(
        feedbackId,
        (delta) => setRepairText((current) => current + delta),
      );
      if (target.scope === 'content_block') {
        const updated = await api.section(target.sectionId);
        onSectionChange(updated);
        await onRefreshSeries();
        setStatus('已替换');
      }
    } catch (reason) {
      setRepairFailed(true);
      setStatus(reason instanceof Error ? reason.message : '补救没有完成，请重试。');
    } finally {
      setSubmitting(false);
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
      if (target.scope === 'global') {
        setStatus('已收到。');
        closeTimerRef.current = window.setTimeout(() => onCloseRef.current(), 900);
        return;
      }
      const regeneration = receipt.regeneration;
      if (regeneration.status === 'stream_ready') {
        setRepairFeedbackId(receipt.id);
        await streamRepair(receipt.id);
        return;
      }
      const blockedMessages: Record<string, string> = {
        FEEDBACK_CONTENT_VERSION_STALE: '当前正文已更新；请刷新后再反馈一次。',
        SECTION_CONTENT_MISSING: '这段正文已不可用；请刷新后再试。',
      };
      setStatus(
        blockedMessages[regeneration.reasonCode || '']
        || '这段正文暂时无法补救。',
      );
      setSubmitting(false);
    } catch (reason) {
      setStatus(reason instanceof Error ? reason.message : '反馈没有提交成功，请稍后重试。');
      setSubmitting(false);
    }
  };

  return (
    <div
      className="confirm-backdrop feedback-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !submitting) onClose();
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
            <p className="eyebrow">{target.scope === 'content_block' ? '正文页边批注' : '告诉我们你的感受'}</p>
            <h2 id="feedback-title">{target.scope === 'content_block' ? '反馈这一段' : '全局反馈'}</h2>
          </div>
          <button className="dialog-close" type="button" aria-label="关闭反馈" disabled={submitting} onClick={onClose}>×</button>
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
          {submitted && target.scope === 'content_block' && (
            <div className={`feedback-repair-answer ${repairFailed ? 'failed' : ''}`} aria-live="polite">
              {repairText ? (
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{repairText}</ReactMarkdown>
              ) : submitting ? (
                <span className="feedback-repair-listening">正在回应<span aria-hidden="true">…</span></span>
              ) : null}
              {submitting && repairText && <i className="stream-caret" aria-hidden="true" />}
            </div>
          )}
          {status && <p className="feedback-status" role="status">{status}</p>}
          <div className="dialog-actions">
            <button type="button" className="quiet-button" disabled={submitting} onClick={onClose}>{submitted ? '关闭' : '取消'}</button>
            {repairFailed && repairFeedbackId ? (
              <button type="button" className="primary-button" disabled={submitting} onClick={() => streamRepair(repairFeedbackId)}>
                {submitting ? '正在回应…' : '重试补救'}
              </button>
            ) : (
              <button className="primary-button" disabled={submitting || submitted || ((target.scope === 'global' || feedbackType === 'other') && message.trim().length < 2)}>
                {submitting ? '正在送出…' : submitted ? '已提交' : '发送反馈'}
              </button>
            )}
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

function bookProgressLabel(book: Book, isCurrent: boolean) {
  if (book.status === 'completed') return '已完成';
  if (book.status === 'locked') return '未解锁';
  if (book.outlineStatus === 'draft') return '待确认';
  if (isCurrent || book.progress > 0) return '学习中';
  return '待开始';
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

function Home({
  data,
  dailyMode,
  onOpen,
  onContinue,
  onCreate,
}: {
  data: Bootstrap | null;
  dailyMode: DailyMode;
  onOpen: (shelf: Shelf) => void;
  onContinue: (seriesId: string) => Promise<void>;
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
  const [dueReviews, setDueReviews] = useState<DueReviews | null>(null);
  const [reviewSession, setReviewSession] = useState<ReviewSession | null>(null);
  const [reviewResult, setReviewResult] = useState<ReviewResult | null>(null);
  const [reviewAnswers, setReviewAnswers] = useState<number[][]>([]);
  const [reviewBusy, setReviewBusy] = useState('正在读取到期复习…');
  const [reviewError, setReviewError] = useState('');
  const pendingReviews = dueReviews?.items.filter(
    (item) => item.status === 'presented' || item.status === 'started',
  ) || [];
  const currentReview = pendingReviews[0] || null;

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
  }, []);

  const startDueReview = async () => {
    if (!currentReview) return;
    setReviewBusy('正在准备一道新的复习题…');
    setReviewError('');
    try {
      const value = await api.startReview(currentReview.assignmentId);
      setReviewSession(value);
      setReviewAnswers(value.quiz.questions.map(() => []));
      setReviewResult(null);
      setDueReviews((current) => current ? {
        ...current,
        items: current.items.map((item) => item.assignmentId === value.assignmentId
          ? {...item, status: 'started', quizSetId: value.quiz.id}
          : item),
      } : current);
    } catch (reason) {
      setReviewError(reason instanceof Error ? reason.message : '复习题准备失败。');
    } finally {
      setReviewBusy('');
    }
  };

  const chooseReviewAnswer = (
    questionIndex: number,
    optionIndex: number,
    mode: 'single' | 'multiple',
  ) => {
    setReviewAnswers((current) => current.map((answer, index) => {
      if (index !== questionIndex) return answer;
      if (mode === 'single') return [optionIndex];
      return answer.includes(optionIndex)
        ? answer.filter((item) => item !== optionIndex)
        : [...answer, optionIndex].sort((left, right) => left - right);
    }));
  };

  const submitDueReview = async () => {
    if (!reviewSession || reviewAnswers.some((answer) => answer.length === 0)) return;
    setReviewBusy('正在保存复习结果…');
    setReviewError('');
    try {
      const value = await api.submitReview(
        reviewSession.assignmentId,
        reviewAnswers,
        `review-${reviewSession.assignmentId}`,
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
  };

  return (
    <section className={`library-dashboard mode-${dailyMode}`}>
      <header className="library-hero">
        <div className="library-hero-copy">
          <h1>把正在学的，<br /><em>放回眼前。</em></h1>
        </div>
        <div className="library-hero-aside">
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

        <article className="library-focus-card review-focus-card">
          <header>
            <span className="focus-card-label">{dailyMode === 'fast' && currentReview ? 'Fast 模式优先 · 待复习' : '待复习'}</span>
            <small>跨书架</small>
          </header>
          {reviewBusy ? (
            <div className="review-empty-state" aria-live="polite">
              <span>正在同步</span>
              <h2>{reviewBusy}</h2>
            </div>
          ) : reviewError ? (
            <div className="review-empty-state review-error-state" role="alert">
              <span>读取失败</span>
              <h2>暂时无法打开复习</h2>
              <p>{reviewError}</p>
              <button onClick={() => void loadDueReviews()}>重新读取</button>
            </div>
          ) : reviewResult ? (
            <div className="review-result-state" aria-live="polite">
              <span>{reviewResult.passed ? '已完成' : '完成本次复习'}</span>
              <h2>{reviewResult.score} / {reviewResult.total}</h2>
              <button onClick={continueReviewQueue}>
                {pendingReviews.length ? '继续下一项' : '完成今日复习'} <span aria-hidden="true">→</span>
              </button>
            </div>
          ) : reviewSession ? (
            <div className="review-session-state">
              <div className="review-objective-label">到期概念</div>
              <p>{currentReview?.objective || reviewSession.quiz.questions[0]?.objective}</p>
              {reviewSession.quiz.questions.map((question, questionIndex) => (
                <fieldset key={`${reviewSession.quiz.id}-${questionIndex}`}>
                  <legend>{question.prompt}</legend>
                  {question.options.map((option, optionIndex) => (
                    <label key={optionIndex}>
                      <input
                        type={question.selectionMode === 'multiple' ? 'checkbox' : 'radio'}
                        name={`review-${reviewSession.quiz.id}-${questionIndex}`}
                        checked={reviewAnswers[questionIndex]?.includes(optionIndex) || false}
                        onChange={() => chooseReviewAnswer(
                          questionIndex,
                          optionIndex,
                          question.selectionMode,
                        )}
                      />
                      <span>{option}</span>
                    </label>
                  ))}
                </fieldset>
              ))}
              <button
                className="review-submit-button"
                disabled={reviewAnswers.some((answer) => answer.length === 0)}
                onClick={() => void submitDueReview()}
              >
                保存复习结果 <span aria-hidden="true">→</span>
              </button>
            </div>
          ) : currentReview ? (
            <div className="review-ready-state">
              <span>{pendingReviews.length} 项到期</span>
              <h2>{currentReview.objective}</h2>
              <div className="review-ready-actions">
                <button onClick={() => void startDueReview()}>
                  {currentReview.status === 'started' ? '继续复习' : '开始复习'} <span aria-hidden="true">→</span>
                </button>
                {currentReview.status !== 'started' && (
                  <button onClick={() => void skipDueReview()}>今天跳过</button>
                )}
              </div>
            </div>
          ) : (
            <div className="review-empty-state review-clear-state">
              <span>今日已清空</span>
              <h2>没有到期的复习</h2>
            </div>
          )}
          <div className="review-cadence" aria-label="复习间隔">
            <span>复习节奏</span>
            <ol>
              <li>1 天</li>
              <li>3 天</li>
              <li>7 天</li>
              <li>14 天</li>
            </ol>
          </div>
        </article>
      </div>

      <section className="library-catalog" aria-labelledby="library-catalog-title">
        <header className="catalog-heading">
          <div>
            <p>按领域归档</p>
            <h2 id="library-catalog-title">我的书架</h2>
          </div>
        </header>

        <div className="library-shelf-grid">
        {data && data.shelves.length === 0 && (
          <div className="empty-library-message">
            <span>还没有书架</span>
            <small>从一个明确的领域开始，把教材、练习和学习进度放在一起。</small>
            <button className="primary-button" onClick={() => setShowCreate(true)}>创建第一个书架</button>
          </div>
        )}
        {data?.shelves.map((item, shelfIndex) => {
          const itemBookCount = item.series.reduce((total, itemSeries) => total + itemSeries.books.length, 0);
          const featuredSeries = item.series.find((itemSeries) => itemSeries.id === dashboard?.today?.seriesId)
            || item.series.find((itemSeries) => itemSeries.progress > 0 && itemSeries.progress < 100)
            || item.series[0]
            || null;
          const featuredBook = featuredSeries?.books.find((book) => (
            featuredSeries.id === dashboard?.today?.seriesId
            && bookContainsSection(book, dashboard.today.sectionId)
          ))
            || featuredSeries?.books.find((book) => book.progress > 0 && book.status !== 'completed')
            || featuredSeries?.books.find((book) => book.status !== 'locked' && book.status !== 'completed')
            || featuredSeries?.books[0]
            || null;
          const featuredBookDetails = featuredBook ? bookProgressDetails(featuredBook) : null;
          const featuredNextSection = featuredBook ? nextBookSection(featuredBook) : null;
          const isTodayBook = Boolean(
            featuredSeries
            && featuredBook
            && featuredSeries.id === dashboard?.today?.seriesId
            && bookContainsSection(featuredBook, dashboard.today.sectionId),
          );
          return (
          <button
            className="library-shelf-card"
            key={item.id}
            aria-label={`进入${item.name}书架，共 ${item.series.length} 个学习系列`}
            onClick={() => onOpen(item)}
          >
            <span className="shelf-index-rail" aria-hidden="true">
              <b>{String(shelfIndex + 1).padStart(2, '0')}</b>
              <i>领域书架</i>
            </span>
            <span className="shelf-card-content">
              <span className="shelf-card-heading">
                <span>
                  <small>{shelfDescriptor(item) || '等待第一套教材归纳'}</small>
                  <strong>{item.name}</strong>
                </span>
                <em>{item.series.length} 个系列 · {itemBookCount} 本教材</em>
              </span>

              {item.tags.length > 0 && (
                <span className="shelf-tags" aria-label="书架标签">
                  {item.tags.slice(0, 4).map((tag) => <i key={tag}>{tag}</i>)}
                </span>
              )}

              {featuredSeries && featuredBook && featuredBookDetails ? (
                <span className="shelf-current-book">
                  <span className="current-book-topline">
                    <span>
                      <b>{isTodayBook ? '正在学习' : '当前教材'}</b>
                      <small>
                        系列 {String(item.series.findIndex((candidate) => candidate.id === featuredSeries.id) + 1).padStart(2, '0')}
                        {' · '}第 {featuredBook.position} 本
                      </small>
                    </span>
                    <em className={`current-book-status is-${featuredBook.status}`}>
                      {bookProgressLabel(featuredBook, isTodayBook)}
                    </em>
                  </span>

                  <strong className="current-book-title">{featuredBook.title}</strong>
                  <span className="current-book-series">
                    <small>所属系列</small>
                    <span>{featuredSeries.title}</span>
                  </span>

                  <span className="current-book-progress-row">
                    <span>
                      {featuredBookDetails.totalChapters > 0
                        ? `${featuredBookDetails.completedChapters}/${featuredBookDetails.totalChapters} 章`
                        : '章节待确认'}
                      {featuredBookDetails.totalSections > 0
                        ? ` · ${featuredBookDetails.completedSections}/${featuredBookDetails.totalSections} 节完成`
                        : ''}
                    </span>
                    <b>{featuredBook.progress}%</b>
                  </span>
                  <span
                    className="current-book-progress-track"
                    role="progressbar"
                    aria-label={`第 ${featuredBook.position} 本《${featuredBook.title}》进度`}
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={featuredBook.progress}
                  >
                    <i style={{ width: `${featuredBook.progress}%` }} />
                  </span>
                  {featuredBookDetails.totalChapters > 0 && (
                    <span className="current-book-chapters" aria-hidden="true">
                      {featuredBook.chapters.map((chapter) => (
                        <i
                          className={chapter.status === 'completed'
                            ? 'is-complete'
                            : chapter.status === 'locked'
                              ? 'is-locked'
                              : 'is-current'}
                          key={chapter.id}
                        />
                      ))}
                    </span>
                  )}

                  {(isTodayBook || featuredNextSection) && (
                    <span className="current-book-next">
                      <small>{isTodayBook ? '下一节' : '从这里开始'}</small>
                      <span>
                        <b>{isTodayBook ? dashboard!.today!.chapterTitle : featuredNextSection!.chapter.title}</b>
                        <i aria-hidden="true">/</i>
                        <strong>{isTodayBook ? dashboard!.today!.sectionTitle : featuredNextSection!.section.title}</strong>
                      </span>
                    </span>
                  )}
                </span>
              ) : (
                <span className="shelf-current-empty">
                  <b>还没有正在学习的教材</b>
                </span>
              )}

              <span className="shelf-card-action">
                {featuredBook ? '继续这本' : '打开书架'} <i aria-hidden="true">→</i>
              </span>
            </span>
          </button>
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
  dailyModePromptEnabled: true,
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
    initialPreferences.dailyModePromptEnabled ?? true,
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
                <b>进入学习前询问 Fast / Slow 模式</b>
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

function ShelfPage({
  shelf,
  profile,
  onBack,
  onCreate,
  onOpen,
  onDelete,
}: {
  shelf: Shelf;
  profile: LearningProfile;
  onBack: () => void;
  onCreate: (body: object, idempotencyKey: string) => Promise<void>;
  onOpen: (id: string) => void;
  onDelete: (id: string) => Promise<void>;
}) {
  const [showPlan, setShowPlan] = useState(false);
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
        <div>
          <h1>{shelf.name}</h1>
        </div>
        <button
          className={showPlan ? 'secondary-button' : 'primary-button'}
          aria-expanded={showPlan}
          aria-controls="create-series-form"
          onClick={() => setShowPlan(!showPlan)}
        >
          {showPlan ? '取消创建' : '＋ 创建学习系列'}
        </button>
      </div>
      {showPlan && <PlanForm profile={profile} submit={onCreate} onCancel={() => setShowPlan(false)} />}
      <div className="series-shelf-heading">
        <span>学习系列</span>
      </div>
      <div className="series-bookshelf">
        <div className="series-volume-row">
          <span className="bookend left" aria-hidden="true" />
          {shelf.series.map((item, index) => (
            <article
              className={`series-volume book-tone-${index % 6}`}
              style={{ height: `${238 + ((index * 17) % 34)}px` }}
              key={item.id}
            >
              <button
                className="series-volume-main"
                aria-label={`进入学习 ${item.title}`}
                onClick={() => onOpen(item.id)}
              >
                <span className="series-volume-number">{String(index + 1).padStart(2, '0')}</span>
                <span className="series-volume-kicker">slow learning series</span>
                <h2>{item.title}</h2>
                <span className="series-volume-rule" />
                <span className="series-volume-progress">
                  <i><b style={{ width: `${item.progress}%` }} /></i>
                  <small>{item.progress}%</small>
                </span>
                <span className="series-volume-action">
                  {item.progress > 0 ? '继续学习' : '进入系列'} <i aria-hidden="true">→</i>
                </span>
              </button>
              <button
                className="series-delete-button"
                aria-label={`删除 ${item.title}`}
                title="删除系列"
                onClick={() => setDeleteTarget(item)}
              >
                <TrashIcon />
              </button>
            </article>
          ))}
          {shelf.series.length === 0 && (
            <div className="empty-shelf-message">
              <span>这里还没有书</span>
              <small>点击“创建学习系列”，从一个学习主题开始。</small>
            </div>
          )}
          <span className="bookend right" aria-hidden="true" />
        </div>
        <span className="series-shelf-board" aria-hidden="true"><i /></span>
      </div>
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
            aria-labelledby="delete-series-title"
            aria-describedby="delete-series-description"
            tabIndex={-1}
          >
            <span className="delete-confirm-icon"><TrashIcon size={20} /></span>
            <p className="eyebrow">删除学习系列</p>
            <h2 id="delete-series-title">{deleteTarget.title}</h2>
            <p id="delete-series-description">该系列及其书、章节会从书架和学习入口中移除。已有学习记录会保留，但当前界面暂不支持恢复。</p>
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
                {deleting ? '正在删除…' : '确认删除'}
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

function PlanForm({
  profile,
  submit,
  onCancel,
}: {
  profile: LearningProfile;
  submit: (body: object, idempotencyKey: string) => Promise<void>;
  onCancel: () => void;
}) {
  const depthOptions = [
    { value: 'overview', label: '快速了解', description: '建立基本认识，抓住核心概念' },
    { value: 'deep', label: '深入理解', description: '理解原理、边界和典型应用' },
    { value: 'mastery', label: '掌握运用', description: '能够独立迁移，并通过复习巩固' },
  ];
  const [topic, setTopic] = useState('');
  const [background, setBackground] = useState(profile.profession);
  const [experience, setExperience] = useState(profile.experience || '暂无直接经验，希望从当前基础开始建立理解。');
  const [purpose, setPurpose] = useState(profile.purpose);
  const [depth, setDepth] = useState('');
  const [formError, setFormError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const idempotencyKey = useRef(crypto.randomUUID());
  const send = async (event: FormEvent) => {
    event.preventDefault();
    if (submitting) return;
    if (!depth) {
      setFormError('请选择目标深度');
      return;
    }
    setFormError('');
    setSubmitting(true);
    try {
      await submit(
        { topic, role: background, experience, purpose, depth, details: '' },
        idempotencyKey.current,
      );
    } catch {
      setSubmitting(false);
    }
  };
  return (
    <form className="plan-form" id="create-series-form" onSubmit={send}>
      <label>
        学习内容
        <input required disabled={submitting} value={topic} onChange={(event) => setTopic(event.target.value)} placeholder="输入你想学习的内容" />
      </label>
      <label>
        你的学习背景
        <input required disabled={submitting} value={background} onChange={(event) => setBackground(event.target.value)} placeholder="输入你的专业、身份或当前背景" />
      </label>
      <label>
        相关经验
        <textarea required disabled={submitting} value={experience} onChange={(event) => setExperience(event.target.value)} placeholder="描述你已经了解或实践过的内容" />
      </label>
      <label>
        学习目的（可选）
        <textarea disabled={submitting} value={purpose} onChange={(event) => setPurpose(event.target.value)} placeholder="描述你希望解决的问题或达到的目标" />
      </label>
      <fieldset disabled={submitting} aria-describedby={formError ? 'plan-depth-error' : undefined}>
        <legend>目标深度</legend>
        {depthOptions.map(({ value, label, description }) => (
          <button
            type="button"
            className={depth === value ? 'selected' : ''}
            aria-pressed={depth === value}
            onClick={() => {
              setDepth(value);
              setFormError('');
            }}
            key={value}
          >
            <span>{label}</span>
            <small>{description}</small>
          </button>
        ))}
        {formError && <p className="plan-form-error" id="plan-depth-error" role="alert">{formError}</p>}
      </fieldset>
      <div className="plan-form-actions">
        <button type="button" className="quiet-button" disabled={submitting} onClick={onCancel}>取消</button>
        <button className="primary-button" disabled={submitting}>{submitting ? '正在生成，请稍候…' : '生成目录方案'}</button>
      </div>
    </form>
  );
}

type WorkspacePanel = 'directory' | 'qa';
type WorkspaceLayoutRatios = {
  threeDirectory: number;
  threeQa: number;
  directoryOnly: number;
  qaOnly: number;
};
type WorkspaceLayoutRatioKey = keyof WorkspaceLayoutRatios;

const workspacePanelSizing = {
  directory: { defaultWidth: 240, minWidth: 220, maxWidth: 360 },
  qa: { defaultWidth: 380, minWidth: 300, maxWidth: 480 },
} as const;
const workspaceReaderMinWidth = 512;
const workspaceRatioMigrationReferenceWidth = 1440;
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
      workspaceWidth - workspaceReaderMinWidth,
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
      workspaceWidth - workspaceReaderMinWidth,
    );
  } else if (!qaHidden) {
    nextQaWidth = clampWorkspacePanelWidth('qa', nextQaWidth, workspaceWidth - workspaceReaderMinWidth);
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
  dailyMode,
  onSelectSection,
  onGenerateSection,
  onRegenerateSection,
  onGenerateChapter,
  onStartNextBook,
  onActivateBook,
  chapterGenerationDisabled,
  generatingChapterId,
  onSectionChange,
  onRefreshSeries,
  onDeleteBook,
  onFeedbackBlock,
  onGlobalFeedback,
  onQaVisibilityChange,
}: {
  userId: string;
  series: Series;
  section: Section | null;
  dailyMode: DailyMode;
  onSelectSection: (id: string) => Promise<Section>;
  onGenerateSection: (id: string) => Promise<void>;
  onRegenerateSection: (id: string) => Promise<void>;
  onGenerateChapter: (chapter: Chapter) => Promise<void>;
  onStartNextBook: () => Promise<void>;
  onActivateBook: (book: Book) => Promise<void>;
  chapterGenerationDisabled: boolean;
  generatingChapterId: string;
  onSectionChange: (section: Section) => void;
  onRefreshSeries: () => Promise<void>;
  onDeleteBook: (bookId: string) => Promise<void>;
  onFeedbackBlock: (block: Block) => void;
  onGlobalFeedback: () => void;
  onQaVisibilityChange: (open: boolean) => void;
}) {
  const [selectedBlockId, setSelectedBlockId] = useState('');
  const [selectedQuote, setSelectedQuote] = useState<TextQuote | null>(null);
  const [explanationRequest, setExplanationRequest] = useState<ExplanationRequest | null>(null);
  const [compactLayout, setCompactLayout] = useState(() => window.matchMedia('(max-width: 900px)').matches);
  const [auxiliaryExclusive, setAuxiliaryExclusive] = useState(() => window.matchMedia('(max-width: 1180px)').matches);
  const [directoryHidden, setDirectoryHidden] = useState(() => window.matchMedia('(max-width: 900px)').matches);
  const [qaHidden, setQaHidden] = useState(true);
  const [readerTab, setReaderTab] = useState<ReaderTab>('content');
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

  const qaAvailable = readerTab !== 'quiz';
  const effectiveQaHidden = qaHidden || !qaAvailable;

  useEffect(() => {
    onQaVisibilityChange(!effectiveQaHidden);
  }, [onQaVisibilityChange, effectiveQaHidden]);

  useEffect(() => () => onQaVisibilityChange(false), [onQaVisibilityChange]);

  useEffect(() => {
    const compactMedia = window.matchMedia('(max-width: 900px)');
    const exclusiveMedia = window.matchMedia('(max-width: 1180px)');
    const adaptPanels = () => {
      const compact = compactMedia.matches;
      const exclusive = exclusiveMedia.matches;
      setCompactLayout(compact);
      setAuxiliaryExclusive(exclusive);
      if (compact) {
        setDirectoryHidden(true);
        setQaHidden(true);
      } else if (exclusive) {
        setDirectoryHidden(true);
      }
    };
    compactMedia.addEventListener('change', adaptPanels);
    exclusiveMedia.addEventListener('change', adaptPanels);
    return () => {
      compactMedia.removeEventListener('change', adaptPanels);
      exclusiveMedia.removeEventListener('change', adaptPanels);
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
  }, [section?.id, section?.content?.id]);

  const location = useMemo(() => findSectionLocation(series, section?.id), [series, section?.id]);
  const activeBlockId = selectedBlockId || section?.content?.blocks[0]?.id || '';
  const selectBlock = (blockId: string) => {
    setSelectedBlockId(blockId);
    setSelectedQuote(null);
  };
  const toggleDirectory = () => {
    if ((compactLayout || auxiliaryExclusive) && directoryHidden) setQaHidden(true);
    setDirectoryHidden((hidden) => !hidden);
  };
  const toggleQa = () => {
    if (!qaAvailable) return;
    if ((compactLayout || auxiliaryExclusive) && qaHidden) setDirectoryHidden(true);
    setQaHidden((hidden) => !hidden);
  };

  const setPanelWidth = (panel: WorkspacePanel, width: number) => {
    if (panel === 'directory') setDirectoryWidth(width);
    else setQaWidth(width);
  };
  const openPanel = (panel: WorkspacePanel) => {
    if (panel === 'directory') {
      if (compactLayout || auxiliaryExclusive) setQaHidden(true);
      setDirectoryHidden(false);
    } else {
      if (compactLayout || auxiliaryExclusive) setDirectoryHidden(true);
      setQaHidden(false);
    }
  };
  const panelAvailableWidth = (panel: WorkspacePanel, startedCollapsed = false) => {
    const workspaceWidth = workspaceRef.current?.clientWidth || window.innerWidth;
    const otherPanelWillClose = startedCollapsed && auxiliaryExclusive;
    const otherWidth = panel === 'directory'
      ? (!qaHidden && !otherPanelWillClose ? qaWidth : 0)
      : (!directoryHidden && !otherPanelWillClose ? directoryWidth : 0);
    return workspaceWidth - workspaceReaderMinWidth - otherWidth;
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
    if (compactLayout || (event.pointerType === 'mouse' && event.button !== 0)) return;
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
    else if (event.key === 'End') nextWidth = workspacePanelSizing[panel].maxWidth;
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
  } as CSSProperties;

  return (
    <div
      ref={workspaceRef}
      style={workspaceStyle}
      className={`learning-workspace mode-${dailyMode} ${directoryHidden ? 'directory-collapsed' : ''} ${effectiveQaHidden ? 'qa-collapsed' : ''} ${qaAvailable ? '' : 'assessment-focus'} ${resizingPanel ? 'is-resizing' : ''}`}
    >
      {compactLayout && (!directoryHidden || !effectiveQaHidden) && (
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
        hidden={directoryHidden}
        onClose={() => setDirectoryHidden(true)}
        currentSectionId={section?.id}
        onSelectSection={onSelectSection}
        onGenerateChapter={onGenerateChapter}
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
        dailyMode={dailyMode}
        directoryHidden={directoryHidden}
        qaHidden={effectiveQaHidden}
        qaAvailable={qaAvailable}
        onToggleDirectory={toggleDirectory}
        onToggleQa={toggleQa}
        onTabChange={(nextTab) => {
          setReaderTab(nextTab);
          if (nextTab === 'quiz') setQaHidden(true);
        }}
        location={location}
        selectedBlockId={activeBlockId}
        onSelectBlock={selectBlock}
        onQuote={(quote) => {
          setSelectedBlockId(quote.blockId);
          setSelectedQuote(quote);
          if (compactLayout || auxiliaryExclusive) setDirectoryHidden(true);
          setQaHidden(false);
        }}
        onGenerate={() => section && onGenerateSection(section.id)}
        onRegenerate={() => (section ? onRegenerateSection(section.id) : Promise.resolve())}
        onSelectSection={onSelectSection}
        onSectionChange={onSectionChange}
        onRefreshSeries={onRefreshSeries}
        onFeedbackBlock={onFeedbackBlock}
        onGlobalFeedback={onGlobalFeedback}
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
          if (compactLayout || auxiliaryExclusive) setDirectoryHidden(true);
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
        key={section?.id || 'empty'}
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
      />
      {(['directory', ...(qaAvailable ? ['qa' as const] : [])] as const).map((panel) => {
        const hidden = panel === 'directory' ? directoryHidden : qaHidden;
        const width = panel === 'directory' ? directoryWidth : qaWidth;
        const sizing = workspacePanelSizing[panel];
        const panelName = panel === 'directory' ? '目录' : '答疑';
        return (
          <div
            key={panel}
            className={`workspace-resize-handle ${panel}-resize-handle ${hidden ? 'is-collapsed' : ''}`}
            role="separator"
            aria-label={hidden ? `展开${panelName}` : `调整${panelName}宽度`}
            aria-orientation="vertical"
            aria-valuemin={sizing.minWidth}
            aria-valuemax={sizing.maxWidth}
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
  hidden,
  onClose,
  currentSectionId,
  onSelectSection,
  onGenerateChapter,
  onStartNextBook,
  onActivateBook,
  chapterGenerationDisabled,
  generatingChapterId,
  onRefreshSeries,
  onDeleteBook,
}: {
  series: Series;
  hidden: boolean;
  onClose: () => void;
  currentSectionId?: string;
  onSelectSection: (id: string) => Promise<Section>;
  onGenerateChapter: (chapter: Chapter) => Promise<void>;
  onStartNextBook: () => Promise<void>;
  onActivateBook: (book: Book) => Promise<void>;
  chapterGenerationDisabled: boolean;
  generatingChapterId: string;
  onRefreshSeries: () => Promise<void>;
  onDeleteBook: (bookId: string) => Promise<void>;
}) {
  const [deleteTarget, setDeleteTarget] = useState<Book | null>(null);
  const [deleting, setDeleting] = useState(false);
  const deleteDialogRef = useModalFocus<HTMLElement>({
    open: Boolean(deleteTarget),
    canClose: !deleting,
    onRequestClose: () => setDeleteTarget(null),
  });

  return (
    <aside className="directory-panel" id="course-directory-panel" aria-label="课程目录" hidden={hidden}>
      <div className="directory-heading">
        <button className="panel-collapse-button" aria-label="收起目录" onClick={onClose}>收起</button>
        <span className="panel-label">目录</span>
        <h2>{series.title}</h2>
        <div className="series-progress">
          <span><i style={{ width: `${series.progress}%` }} /></span>
          <b>{series.progress}%</b>
        </div>
      </div>
      {series.books[0]?.status === 'completed'
        && series.books[1]
        && series.books[1].status !== 'locked' && (
          <div className="next-book-callout" role="status">
            <b>第一册已完成</b>
            <span>第二册《{series.books[1].title}》已经解锁。</span>
            <button className="secondary-button" onClick={onStartNextBook}>开始第二册</button>
          </div>
      )}
      <nav className="book-tree">
        {series.books.map((book, index) => (
          <BookTree
            key={book.id}
            book={book}
            currentSectionId={currentSectionId}
            onSelectSection={onSelectSection}
            onGenerateChapter={onGenerateChapter}
            canActivate={
              book.outlineStatus === 'draft'
              && (index === 0 || series.books[index - 1].status === 'completed')
            }
            onActivate={onActivateBook}
            chapterGenerationDisabled={chapterGenerationDisabled}
            generatingChapterId={generatingChapterId}
            onRefreshSeries={onRefreshSeries}
            onRequestDelete={setDeleteTarget}
          />
        ))}
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
    </aside>
  );
}

function BookTree({
  book,
  currentSectionId,
  onSelectSection,
  onGenerateChapter,
  canActivate,
  onActivate,
  chapterGenerationDisabled,
  generatingChapterId,
  onRefreshSeries,
  onRequestDelete,
}: {
  book: Book;
  currentSectionId?: string;
  onSelectSection: (id: string) => Promise<Section>;
  onGenerateChapter: (chapter: Chapter) => Promise<void>;
  canActivate: boolean;
  onActivate: (book: Book) => Promise<void>;
  chapterGenerationDisabled: boolean;
  generatingChapterId: string;
  onRefreshSeries: () => Promise<void>;
  onRequestDelete: (book: Book) => void;
}) {
  const containsCurrent = book.chapters.some((chapter) => chapter.sections.some((item) => item.id === currentSectionId));
  return (
    <details className="book-node" open={containsCurrent || book.status !== 'locked'}>
      <button
        className="book-delete-button"
        aria-label={`删除书籍 ${book.title}`}
        title="删除书籍"
        onClick={() => onRequestDelete(book)}
      >
        <TrashIcon size={14} />
      </button>
      <summary>
        <span className="book-number">书 {book.position}</span>
        <span>
          <b>{book.title}</b>
          <small>
            {book.outlineStatus === 'draft'
              ? '待确认'
              : book.status === 'completed'
                ? '已完成'
                : book.status === 'locked'
                  ? '未解锁'
                  : '已解锁'}
            {' · '}{book.progress}% · {Math.round(book.estimatedMinutes / 60)} 小时
          </small>
        </span>
        <i>{book.status === 'locked' ? <LockIcon /> : <ChevronIcon />}</i>
      </summary>
      {book.outlineStatus === 'draft' && (
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
      <div className="chapter-tree">
        {book.chapters.map((chapter) => {
          const chapterLocked = chapter.status === 'locked';
          return (
            <div className="chapter-node" key={chapter.id}>
              {chapter.generated || chapterLocked ? (
                <div
                  className={`chapter-title ${chapterLocked ? 'locked' : ''}`}
                  aria-label={chapterLocked
                    ? `第 ${chapter.position} 章 ${chapter.title}，未解锁`
                    : `第 ${chapter.position} 章 ${chapter.title}`}
                >
                  <span>第 {chapter.position} 章</span>
                  <b>{chapter.title}</b>
                  {chapterLocked && <LockIcon size={13} />}
                </div>
              ) : (
                <button
                  className="chapter-title chapter-entry"
                  aria-label={`生成第 ${chapter.position} 章 ${chapter.title} 的小节并进入`}
                  disabled={chapterGenerationDisabled || generatingChapterId === chapter.id}
                  onClick={() => onGenerateChapter(chapter)}
                >
                  <span>第 {chapter.position} 章</span>
                  <b>
                    {generatingChapterId === chapter.id
                      ? '正在规划本章小节…'
                      : chapter.title}
                  </b>
                  <GenerateIcon />
                </button>
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
                  {chapterLocked ? '完成上一章后解锁' : '点击章名开始'}
                </small>
              </div>
            )}
            </div>
          );
        })}
        {book.capstone && (
          <ArtifactSubmission
            id={book.id}
            status={book.capstone.status}
            attachmentCount={book.capstone.attachments.length}
            onSubmit={async (action) => {
              await action();
              await onRefreshSeries();
            }}
          />
        )}
      </div>
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
    : item.status === 'locked'
      ? <LockIcon size={11} />
      : preparing
        ? '…'
        : '•';
  const sectionNumber = `${chapterPosition}.${item.position}`;
  return (
    <button
      className={`section-tree-button ${active ? 'active' : ''} ${item.status}`}
      disabled={item.status === 'locked' || preparing}
      aria-label={`${sectionNumber} ${item.title}`}
      onClick={onClick}
    >
      <span>{state}</span>
      <small className="section-number">{sectionNumber}</small>
      <b>{item.title}{preparing ? ' · 准备中' : ''}</b>
    </button>
  );
}

function ReaderPanel({
  series,
  section,
  dailyMode,
  directoryHidden,
  qaHidden,
  qaAvailable,
  onToggleDirectory,
  onToggleQa,
  onTabChange,
  location,
  selectedBlockId,
  onSelectBlock,
  onQuote,
  onGenerate,
  onRegenerate,
  onSelectSection,
  onSectionChange,
  onRefreshSeries,
  onFeedbackBlock,
  onGlobalFeedback,
  onRestorePersonalPresentation,
  onExplainBlock,
}: {
  series: Series;
  section: Section | null;
  dailyMode: DailyMode;
  directoryHidden: boolean;
  qaHidden: boolean;
  qaAvailable: boolean;
  onToggleDirectory: () => void;
  onToggleQa: () => void;
  onTabChange: (tab: ReaderTab) => void;
  location: ReturnType<typeof findSectionLocation>;
  selectedBlockId: string;
  onSelectBlock: (blockId: string) => void;
  onQuote: (quote: TextQuote) => void;
  onGenerate: () => void;
  onRegenerate: () => Promise<void>;
  onSelectSection: (id: string) => Promise<Section>;
  onSectionChange: (section: Section) => void;
  onRefreshSeries: () => Promise<void>;
  onFeedbackBlock: (block: Block) => void;
  onGlobalFeedback: () => void;
  onRestorePersonalPresentation: (block: Block) => Promise<void>;
  onExplainBlock: (block: Block, style: ExplanationStyle, customQuestion?: string) => void;
}) {
  const [tab, setTab] = useState<ReaderTab>('content');
  const [selectionPopup, setSelectionPopup] = useState<SelectionPopup | null>(null);
  const [regenerationConfirmOpen, setRegenerationConfirmOpen] = useState(false);
  const [regenerating, setRegenerating] = useState(false);
  const [regenerationStartedAt, setRegenerationStartedAt] = useState(0);
  const [regenerationClock, setRegenerationClock] = useState(Date.now());
  const [reviewTargetBlockId, setReviewTargetBlockId] = useState('');
  const readerScrollRef = useRef<HTMLDivElement>(null);
  const reviewHighlightTimerRef = useRef<number | null>(null);
  const regenerationDialogRef = useModalFocus<HTMLElement>({
    open: regenerationConfirmOpen,
    canClose: !regenerating,
    onRequestClose: () => setRegenerationConfirmOpen(false),
  });

  useEffect(() => {
    const initialTab = section?.status === 'completed' && section.note ? 'note' : 'content';
    setTab(initialTab);
    onTabChange(initialTab);
    setSelectionPopup(null);
    setRegenerationConfirmOpen(false);
    setReviewTargetBlockId('');
    if (reviewHighlightTimerRef.current !== null) {
      window.clearTimeout(reviewHighlightTimerRef.current);
      reviewHighlightTimerRef.current = null;
    }
    if (readerScrollRef.current) readerScrollRef.current.scrollTop = 0;
  }, [section?.id, section?.content?.id]);

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
      if (readerScrollRef.current) readerScrollRef.current.scrollTop = 0;
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
    const blockElement = anchorElement?.closest<HTMLElement>('[data-block-id]');
    const text = selection?.toString().replace(/\s+/g, ' ').trim() || '';

    if (!range || range.collapsed || !blockElement || text.length < 2) {
      setSelectionPopup(null);
      return;
    }

    const rect = range.getBoundingClientRect();
    setSelectionPopup({
      text: text.slice(0, 600),
      blockId: blockElement.dataset.blockId || '',
      top: Math.min(rect.bottom + 9, window.innerHeight - 48),
      left: Math.min(Math.max(rect.left + rect.width / 2, 54), window.innerWidth - 54),
    });
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
        />
        <SeriesRoutePreview series={series} />
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
      />
      <LessonReaderHeader
        bookPosition={location?.book.position}
        chapterPosition={location?.chapter.position}
        chapterTitle={location?.chapter.title}
        sectionPosition={section.position}
        title={section.title}
        status={section.status}
        canRegenerate={Boolean(section.content && section.bestScore === 0 && section.totalScore === 0)}
        regenerating={regenerating}
        onRequestRegenerate={() => setRegenerationConfirmOpen(true)}
        onFeedback={onGlobalFeedback}
      />

      <LessonReaderTabs
        active={tab}
        quizAvailable={Boolean(section.quiz)}
        noteAvailable={Boolean(section.note)}
        completed={section.status === 'completed'}
        onChange={switchTab}
      />

      <div
        className="reader-scroll"
        id="reader-tabpanel"
        role="tabpanel"
        aria-labelledby={`reader-tab-${tab}`}
        ref={readerScrollRef}
        onMouseUp={captureTextSelection}
        onKeyUp={captureTextSelection}
        onScroll={() => setSelectionPopup(null)}
      >
        {tab === 'content' && (
          <LessonContent
            section={section}
            dailyMode={dailyMode}
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
            section={section}
            onUpgrade={() => setRegenerationConfirmOpen(true)}
            onSectionChange={onSectionChange}
            onRefreshSeries={onRefreshSeries}
            onSelectSection={onSelectSection}
            onReviewContent={reviewContent}
            onSubmissionComplete={() => {
              if (readerScrollRef.current) readerScrollRef.current.scrollTop = 0;
            }}
          />
        )}
        {tab === 'note' && section.note && (
          <Note sectionId={section.id} note={section.note} onSaved={onSectionChange} />
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
        <button
          className="selection-qa-button"
          style={{ top: selectionPopup.top, left: selectionPopup.left }}
          onMouseDown={(event) => event.preventDefault()}
          onClick={() => {
            onQuote({ text: selectionPopup.text, blockId: selectionPopup.blockId });
            setSelectionPopup(null);
            const currentSelection = window.getSelection();
            if (typeof currentSelection?.removeAllRanges === 'function') {
              currentSelection.removeAllRanges();
            }
          }}
        >
          <span>?</span>
          答疑
        </button>
      )}
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
}: {
  directoryHidden: boolean;
  qaHidden: boolean;
  qaAvailable: boolean;
  onToggleDirectory: () => void;
  onToggleQa: () => void;
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
          onClick={onToggleDirectory}
        >
          ‹
        </button>
      )}
      {qaAvailable && qaHidden && (
        <button
          className="reader-qa-trigger"
          aria-controls="section-qa-panel"
          aria-expanded={false}
          onClick={onToggleQa}
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
          onClick={onToggleQa}
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
      <div className="lesson-question">
        <span>本节问题</span>
        <h2>{section.question}</h2>
      </div>
      <p className="content-trust-note">
        {section.content.generationMode === 'demo'
          ? '演示内容 · 仅用于体验学习流程'
          : section.content.boundaryValidation.status === 'passed'
            ? `${section.content.aiGenerated ? 'AI 生成' : '授权内容'} · 已完成发布检查 · 关键事实请结合参考来源判断`
            : section.content.boundaryValidation.status === 'legacy'
              ? '历史内容 · 尚未按当前标准重新检查 · 关键事实请结合参考来源判断'
              : `${section.content.aiGenerated ? 'AI 生成' : '授权内容'} · 检查状态未确认 · 关键事实请结合参考来源判断`}
      </p>
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
  section,
  onUpgrade,
  onSectionChange,
  onRefreshSeries,
  onSelectSection,
  onReviewContent,
  onSubmissionComplete,
}: {
  section: Section;
  onUpgrade: () => void;
  onSectionChange: (section: Section) => void;
  onRefreshSeries: () => Promise<void>;
  onSelectSection: (id: string) => Promise<Section>;
  onReviewContent: (blockId?: string) => void;
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
  const hasMultipleChoice = Boolean(
    section.quiz?.questions.some((question) => question.selectionMode === 'multiple'),
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
  const eligibleUnderCurrentPolicy = Boolean(
    result && !result.passed && result.total > 0 && result.score / result.total >= 0.6,
  );

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
    if (!result || result.passed || remediationTask?.status !== 'failed') return;
    let cancelled = false;
    const reconcileFailedRemediation = async () => {
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
        void monitorTasks([latest], false).catch(() => {
          setWorkflowRunning(false);
          setSubmissionError('暂时无法更新后续内容，请稍后再试。');
        });
      } catch {
        // A transient refresh failure must not replace the saved quiz result.
      }
    };
    const reconcileOnFocus = () => { void reconcileFailedRemediation(); };
    const reconcileWhenVisible = () => {
      if (document.visibilityState === 'visible') reconcileOnFocus();
    };
    window.addEventListener('focus', reconcileOnFocus);
    document.addEventListener('visibilitychange', reconcileWhenVisible);
    void reconcileFailedRemediation();
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
      <p className="quiz-draft-note">
        {hasMultipleChoice
          ? '标为“多选”的题目可以选择多个答案，其余题目只能选择一个答案。'
          : '每道题只有一个答案，选择最符合本节内容的一项。'}
      </p>
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
          openingNextSection={openingNextSection}
          workflowRunning={workflowRunning}
          workflowTasks={workflowTasks}
          retryingTasks={retryingTasks}
          onReviewContent={onReviewContent}
          onOpenRemediation={openRemediation}
          onReassess={reassessAttempt}
          onOpenNextSection={openNextSection}
          onRetryTasks={retryFailedTasks}
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
      {section.askMeUnlocked && <AskMePanel sectionId={section.id} />}
    </div>
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
  openingNextSection,
  workflowRunning,
  workflowTasks,
  retryingTasks,
  onReviewContent,
  onOpenRemediation,
  onReassess,
  onOpenNextSection,
  onRetryTasks,
}: {
  section: Section;
  result: QuizResult;
  remediationReady: boolean;
  openingRemediation: boolean;
  eligibleUnderCurrentPolicy: boolean;
  reassessing: boolean;
  nextSectionTask: LearningTask | null;
  nextSectionId: string | null;
  openingNextSection: boolean;
  workflowRunning: boolean;
  workflowTasks: LearningTask[];
  retryingTasks: boolean;
  onReviewContent: (blockId?: string) => void;
  onOpenRemediation: () => Promise<void>;
  onReassess: () => Promise<void>;
  onOpenNextSection: () => Promise<void>;
  onRetryTasks: () => Promise<void>;
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
      </header>

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
        className={`remediation-readiness ${hasBlockingWorkflowFailure ? 'failed' : followupReady ? 'ready' : ''}`}
        aria-live="polite"
        aria-atomic="true"
      >
        {result.passed ? (
          nextSectionReady ? (
            <>
              <span>下一节已准备好</span>
              {noteTask?.status === 'failed' && (
                <small>{generationFailureMessage(noteTask, '个人笔记')}</small>
              )}
              <div className="remediation-readiness-actions">
                {noteTask?.status === 'failed' && noteTask.retryable && (
                  <button
                    type="button"
                    className="secondary-button"
                    disabled={retryingTasks || workflowRunning}
                    onClick={onRetryTasks}
                  >
                    {retryingTasks ? '正在重新准备…' : '重新准备笔记'}
                  </button>
                )}
                <button
                  className="primary-button"
                  disabled={openingNextSection}
                  onClick={onOpenNextSection}
                >
                  {openingNextSection ? '正在进入…' : '进入下一节'}
                </button>
              </div>
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
            <span>可以继续学习</span>
            <button
              className="primary-button"
              disabled={reassessing}
              onClick={onReassess}
            >
              {reassessing ? '正在更新进度…' : '按当前规则继续'}
            </button>
          </>
        ) : remediationReady ? (
          <>
            <span>补充教学已准备好</span>
            <button
              className="primary-button"
              disabled={openingRemediation}
              onClick={onOpenRemediation}
            >
              {openingRemediation ? '正在打开…' : '开始补充教学与变式题'}
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
            <span><i />正在准备补充教学</span>
          </>
        )}
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

function AskMePanel({ sectionId }: { sectionId: string }) {
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
    <div className="askme-view">
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

      {loading ? (
        <div className="askme-loading" aria-live="polite">正在恢复讨论…</div>
      ) : !discussion ? (
        <div className="askme-start-card">
          <button className="primary-button large" disabled={actioning} onClick={start}>
            {actioning ? '正在准备…' : '进入关卡'}
          </button>
        </div>
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
}) {
  const [threadId, setThreadId] = useState<string>();
  const [newQuestion, setNewQuestion] = useState(false);
  const [question, setQuestion] = useState('');
  const [messages, setMessages] = useState<QaExchange[]>([]);
  const [asking, setAsking] = useState(false);
  const [historyStatus, setHistoryStatus] = useState<'idle' | 'loading' | 'loaded' | 'error'>('idle');
  const [historyError, setHistoryError] = useState('');
  const [draftExplanation, setDraftExplanation] = useState<ExplanationRequest | null>(null);
  const [styleFeedback, setStyleFeedback] = useState<Record<string, 'helpful' | 'unclear'>>({});
  const [preferenceError, setPreferenceError] = useState('');
  const [adoptingExchange, setAdoptingExchange] = useState('');
  const [adoptedExchange, setAdoptedExchange] = useState('');
  const [confirmedPreference, setConfirmedPreference] = useState<Record<string, boolean>>({});
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const messagesRef = useRef<HTMLDivElement>(null);
  const explanationRequestRef = useRef('');
  const draftExplanationRef = useRef<ExplanationRequest | null>(null);
  const selectedBlock =
    section?.content?.blocks.find((block) => block.id === selectedBlockId) ??
    section?.content?.blocks[0];
  const effectiveBlockId = selectedBlock?.id ?? selectedBlockId;

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

  useEffect(() => {
    const node = messagesRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [messages]);

  const loadHistory = async () => {
    if (!section?.content || historyStatus === 'loading') return;
    setHistoryStatus('loading');
    setHistoryError('');
    try {
      const history = await api.qaHistory(section.id);
      setMessages(qaHistoryExchanges(history));
      setThreadId(history.lastThreadId || undefined);
      if (!selectedQuote && !explanationRequest && history.lastThreadId) {
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
    if (asking || historyStatus === 'loading' || !section || !effectiveBlockId || !question.trim()) return;
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
      </div>
      {!section?.content ? (
        <div className="qa-empty">
          <span>?</span>
          <b>暂不可提问</b>
        </div>
      ) : (
        <>
          <div className="qa-context-bar">
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
          </div>
          {selectedQuote && (
            <div className="selected-quote-card">
              <div>
                <span>已选内容</span>
                <button aria-label="移除已选内容" onClick={onClearQuote}>×</button>
              </div>
              <blockquote>{selectedQuote.text}</blockquote>
            </div>
          )}
          {draftExplanation && (
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
          <div className="qa-messages" ref={messagesRef}>
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
            {historyStatus === 'loaded' && messages.length === 0 && !draftExplanation && (
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
                        }}
                      >
                        {message.answer}
                      </ReactMarkdown>
                    ) : (
                      <span className="streaming-dots"><i /><i /><i /></span>
                    )}
                    {message.status === 'streaming' && message.answer && <span className="stream-caret" />}
                  </div>
                </div>
                {message.status === 'done' && message.explanationStyle && (
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
          <div className="qa-composer">
            <textarea
              ref={composerRef}
              value={question}
              disabled={historyStatus === 'loading'}
              onChange={(event) => setQuestion(event.target.value)}
              onKeyDown={(event) => {
                if (event.key !== 'Enter' || event.nativeEvent.isComposing) return;
                if (event.metaKey || event.ctrlKey || event.shiftKey) return;
                event.preventDefault();
                ask();
              }}
              placeholder={selectedQuote ? '针对选中的内容输入问题…' : '基于当前段落继续追问…'}
            />
            <div>
              <div className="qa-composer-meta">
                <label><input type="checkbox" checked={newQuestion} onChange={(event) => setNewQuestion(event.target.checked)} /> 新问题</label>
                <span>Enter 发送 · ⌘/Ctrl + Enter 换行</span>
              </div>
              <button disabled={asking || historyStatus === 'loading' || !question.trim()} onClick={ask}>
                {asking ? '回答中…' : '发送 ↑'}
              </button>
            </div>
          </div>
        </>
      )}
    </aside>
  );
}

function ArtifactSubmission({
  id,
  status,
  attachmentCount,
  onSubmit,
}: {
  id: string;
  status: string;
  attachmentCount: number;
  onSubmit: (action: () => Promise<unknown>) => Promise<void>;
}) {
  const needsLegacyFile = status === 'completed' && attachmentCount === 0;
  const enabled = status === 'available' || needsLegacyFile;
  const upload = async (file: File) => {
    const attachment = await api.uploadCapstone(id, file);
    return api.capstone(id, { artifact: '全书综合成果', verification: '学习者复核记录' }, [attachment.id]);
  };
  return (
    <label className={`artifact-submit capstone ${enabled ? 'enabled' : ''}`}>
      <span className="artifact-icon">
        {status === 'locked' ? <LockIcon size={12} /> : '◆'}
      </span>
      <span>全书大作业</span>
      {status !== 'locked' && (
        <small>· {needsLegacyFile ? '补充附件' : status === 'completed' ? '已完成' : '提交成果'}</small>
      )}
      <input
        type="file"
        hidden
        disabled={!enabled}
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) onSubmit(() => upload(file));
        }}
      />
    </label>
  );
}
