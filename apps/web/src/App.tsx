import { FormEvent, useEffect, useMemo, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { api } from './api/client';
import { telemetry } from './telemetry';
import { ProfileOnboardingFlow } from './ProfileOnboardingFlow';
import { DailyModeDialog, DailyModeHeader } from './DailyMode';
import type {
  AskMeDiscussion,
  AiRuntime,
  AuthConfig,
  AuthState,
  Block,
  Book,
  Bootstrap,
  AccountExitReceipt,
  PrivacyState,
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
type ReaderTab = 'content' | 'quiz' | 'note';
type TextQuote = { text: string; blockId: string };
type SelectionPopup = TextQuote & { top: number; left: number };
type FeedbackTarget =
  | { scope: 'global' }
  | {
      scope: 'content_block';
      sectionId: string;
      contentVersionId: string;
      block: Block;
    };
const AI_RUNTIME_SETTINGS_ENABLED = import.meta.env.VITE_INTERNAL_AI_SETTINGS === 'true';
const GENERATION_STAGE_LABELS: Record<string, string> = {
  queued: '正在排队',
  teaching_blueprint: '正在规划改写方案',
  content_generation: '正在生成正文',
  combined_generation: '正在生成正文与测验',
  source_verification: '正在核验来源',
  source_verification_degraded: '正在记录来源核验结果',
  source_repair: '正在替换无法核验的来源',
  source_repair_rejected: '正在重新检查来源',
  quiz_generation: '正在生成测验',
  semantic_alignment_review: '正在检查正文与测验是否一致',
  semantic_alignment_rejected: '正文与测验检查未通过',
  semantic_claim_verification: '正在核验关键结论',
  persistence: '正在保存新版本',
  persisted: '已经完成',
  failed: '生成未完成',
};

const formatElapsed = (milliseconds: number) => {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000));
  if (seconds < 60) return `${seconds} 秒`;
  return `${Math.floor(seconds / 60)} 分 ${String(seconds % 60).padStart(2, '0')} 秒`;
};

type QaExchange = {
  id: string;
  question: string;
  answer: string;
  relation: string;
  status: 'streaming' | 'done' | 'error';
};

export default function App() {
  const [auth, setAuth] = useState<AuthState | null>(null);
  const [authConfig, setAuthConfig] = useState<AuthConfig | null>(null);
  const [authChecked, setAuthChecked] = useState(false);
  const [localUsername, setLocalUsername] = useState('');
  const [localPassword, setLocalPassword] = useState('');
  const [showLocalPassword, setShowLocalPassword] = useState(false);
  const [data, setData] = useState<Bootstrap | null>(null);
  const [view, setView] = useState<View>(() => window.location.pathname === '/profile' ? 'profile' : 'home');
  const [shelf, setShelf] = useState<Shelf | null>(null);
  const [series, setSeries] = useState<Series | null>(null);
  const [section, setSection] = useState<Section | null>(null);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [showAiSettings, setShowAiSettings] = useState(false);
  const [feedbackTarget, setFeedbackTarget] = useState<FeedbackTarget | null>(null);
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
  const chapterGenerationRequests = useRef(new Set<string>());
  const userMenuRef = useRef<HTMLDivElement | null>(null);
  const lastViewedSection = useRef('');

  const hasActiveDailyMode = () => Boolean(
    data?.dailyMode?.active
    && data.dailyMode.expiresAt
    && new Date(data.dailyMode.expiresAt).getTime() > Date.now(),
  );

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
    const clearUserState = () => {
      setAuth(null);
      setData(null);
      setShelf(null);
      setSeries(null);
      setSection(null);
      setView('home');
      setShowUserMenu(false);
      window.history.replaceState({}, '', '/');
      setAuthChecked(true);
    };
    api.setUnauthorizedHandler(clearUserState);
    telemetry.start();
    void initializeAuth();
    const handleHistory = () => {
      const nextView: View = window.location.pathname === '/profile' ? 'profile' : 'home';
      setView(nextView);
      setProfileSection(new URLSearchParams(window.location.search).get('section') === 'account' ? 'account' : 'profile');
      setShowUserMenu(false);
      setShelf(null);
      setSeries(null);
      setSection(null);
    };
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
      if (!(view === 'learn' && section && activityDailyMode)) setDailyModeDialogOpen(true);
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
      } else {
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
  }, [data?.dailyMode?.version, data?.dailyMode?.active, data?.dailyMode?.expiresAt, view, section?.id, activityDailyMode]);

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

  const openShelf = (value: Shelf) => {
    setShelf(value);
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

  const goHome = () => {
    if (window.location.pathname !== '/') window.history.pushState({}, '', '/');
    setShowUserMenu(false);
    setView('home');
    setSeries(null);
    setSection(null);
    setActivityDailyMode(null);
    setDailyModeExpiredDuringActivity(false);
    if (!hasActiveDailyMode()) setDailyModeDialogOpen(true);
    void api.bootstrap()
      .then(setData)
      .catch((reason) => setError(reason instanceof Error ? reason.message : '主页刷新失败'));
  };

  const openProfileCenter = (nextSection: 'profile' | 'account' = 'profile') => {
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

  const openAndTrackSection = async (sectionId: string) => {
    const value = await api.openSection(sectionId);
    setActivityDailyMode(value.dailyModeAtStart || data?.dailyMode?.dailyMode || 'slow');
    setDailyModeExpiredDuringActivity(false);
    void api.updateResume(sectionId).catch(() => undefined);
    return value;
  };

  const loadSection = async (sectionId: string) => {
    if (!hasActiveDailyMode()) {
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
      if (source === 'header_toggle' && section) setActivityDailyMode(mode);
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
    setPreparingInitialSection(true);
    setBusy('正在准备第一节，完成后自动打开…');
    setError('');
    try {
      let task = initialTask;
      for (let poll = 0; poll < 360; poll += 1) {
        if (!['succeeded', 'failed'].includes(task.status)) {
          task = await api.learningTask(task.taskId);
        }
        if (task.status === 'succeeded') {
          const refreshed = await api.series(value.id);
          setSeries(refreshed);
          const targetSectionId = typeof task.result?.targetSectionId === 'string'
            ? task.result.targetSectionId
            : firstUsableSection(refreshed);
          if (targetSectionId) {
            setSection(await openAndTrackSection(targetSectionId));
          }
          return true;
        }
        if (task.status === 'failed') {
          const refreshed = await api.series(value.id);
          setSeries(refreshed);
          const fallbackSectionId = firstUsableSection(refreshed);
          if (fallbackSectionId) {
            setSection(await openAndTrackSection(fallbackSectionId));
          }
          setError('第一节后台准备失败。目录已经保存，可以从第一章安全重试。');
          return true;
        }
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
      }
      setError('第一节仍在后台准备，可以稍后重新进入本书查看。');
      return true;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法读取第一节准备状态。');
      return true;
    } finally {
      setPreparingInitialSection(false);
      setBusy('');
    }
  };

  const openSeries = async (seriesId: string) => {
    const value = await run('正在进入学习空间…', () => api.series(seriesId));
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
    const initial = resumeBelongsToSeries ? resumeSection! : firstUsableSection(value);
    if (initial) await loadSection(initial);
    else setSection(null);
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
      '正在依据最新学习证据校准下一本书…',
      () => api.replanBook(book.id),
    );
    const outline = proposal.chapters
      .map((chapter, index) => `${index + 1}. ${chapter.title}：${chapter.objective}`)
      .join('\n');
    const confirmed = window.confirm(
      `校准说明：${proposal.rationale}\n\n${outline}\n\n确认后将冻结这本书的新章节目录。`,
    );
    if (!confirmed) return;
    await run(
      '正在确认新章节目录…',
      () => api.confirmBookReplan(book.id, proposal.proposalId),
    );
    await refreshSeries();
  };

  const generateSection = async (sectionId: string) => {
    const value = await run('正在核查来源并生成本节…', () => api.generateSection(sectionId));
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
      const value = await run('正在重新生成并核验本节…', () => api.regenerateSection(sectionId));
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

  if (exitReceipt) {
    return <AccountExitReceiptPage receipt={exitReceipt} onClose={() => setExitReceipt(null)} />;
  }

  if(!auth) {
    const isDemo = authConfig?.mode === 'demo';
    const isLocal = authConfig?.mode === 'local';
    const isPassword = authConfig?.mode === 'password';
    const usesCredentials = isLocal || isPassword;
    const providerName = authConfig?.providerName || '统一身份账户';
    return (
      <div className="app-shell auth-shell">
        <header className="auth-header">
          <span className="brand"><span className="brand-mark"><i /></span><b>slow</b></span>
          <span>AI 原生个人学习系统</span>
        </header>
        <main className="auth-main">
          <section className="auth-story">
            <p className="eyebrow">YOUR PERSONAL LEARNING LIBRARY</p>
            <h1>把想学的，<br />变成真正学会的。</h1>
            <p className="auth-lead">
              Slow 把学习目标写成一套可以逐节阅读、验证和持续积累记忆的个人教材。
            </p>
            <div className="auth-journey" aria-label="Slow 学习闭环">
              <article><span>01</span><div><b>生成你的书</b><small>从目标到章节与小节</small></div></article>
              <article><span>02</span><div><b>逐节学习验证</b><small>通过测验才继续前进</small></div></article>
              <article><span>03</span><div><b>积累长期记忆</b><small>让下一本书真正了解你</small></div></article>
            </div>
          </section>

          <section className="auth-card" aria-busy={!authChecked}>
            <div className={`auth-mode-badge ${isDemo || isLocal ? 'demo' : ''}`}>
              <i />{isDemo ? '固定体验环境' : isLocal ? '本地多账号环境' : '受邀用户空间'}
            </div>
            <h2>{isDemo ? '进入体验书架' : usesCredentials ? '登录学习账号' : '欢迎回来'}</h2>
            <p>
              {isDemo
                ? '无需配置第三方账号，使用本机固定体验身份查看完整学习闭环。'
                : usesCredentials
                  ? '输入邀请时收到的账号和密码，进入独立的个人学习书架。'
                  : '登录后继续你的书架、学习记录与掌握画像。'}
            </p>

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
            ) : usesCredentials ? (
              <form className="local-auth-form" onSubmit={(event) => void loginWithLocalAccount(event)}>
                <label>
                  账号
                  <input
                    autoComplete="username"
                    value={localUsername}
                    onChange={(event) => setLocalUsername(event.target.value)}
                    placeholder="输入分配给你的账号"
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
                    <button
                      type="button"
                      aria-label={showLocalPassword ? '隐藏密码' : '显示密码'}
                      aria-pressed={showLocalPassword}
                      onClick={() => setShowLocalPassword((visible) => !visible)}
                    >
                      {showLocalPassword ? (
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
                  </span>
                </label>
                <button className="auth-submit" type="submit" disabled={Boolean(busy)}>
                  登录独立书架 <span>→</span>
                </button>
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

            <div className="auth-trust-list">
              <div><i>✓</i><span><b>服务端安全会话</b><small>{usesCredentials ? '密码使用 Argon2id 哈希，浏览器只保留会话 Cookie' : '浏览器不保存身份提供商密码'}</small></span></div>
              <div><i>✓</i><span><b>学习数据按用户隔离</b><small>书架、证据和画像仅属于你的账户</small></span></div>
              <div><i>✓</i><span><b>随时安全退出</b><small>退出后服务端会话立即撤销</small></span></div>
            </div>

            <small className="auth-disclaimer">
              {isDemo
                ? '体验模式会被明确标记，不作为真实账号或真实认证证据。'
                : isLocal
                  ? '本地账号仅用于开发和场景验证，生产环境会拒绝启用。'
                  : isPassword
                    ? '账号仅由内测管理员创建，不开放公开注册。'
                  : `登录将在${providerName}页面完成，Slow 不接收你的密码。`}
            </small>
            {authConfig?.privacyNotice && (
              <details className="auth-privacy-brief">
                <summary>内测隐私与数据说明</summary>
                <p>{authConfig.privacyNotice.summary}</p>
                <small>登录后、开始填写学习画像前，需要分别确认隐私告知与自愿参加内测。版本 {authConfig.privacyNotice.noticeVersion}</small>
              </details>
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
    && (
      dailyModeDialogOpen
      || (
        !data.dailyMode.active
        && !(view === 'learn' && section && activityDailyMode)
      )
    ),
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
              effectiveMode={activityDailyMode || data.dailyMode.dailyMode}
              expiredInActivity={dailyModeExpiredDuringActivity}
              busy={dailyModeBusy}
              onActivate={activateDailyMode}
            />
          )}
          {busy && <span className="busy-indicator"><i />{busy}</span>}
          {AI_RUNTIME_SETTINGS_ENABLED && (
            <button className="quiet-button ai-settings-trigger" onClick={() => setShowAiSettings(true)}>
              <span aria-hidden="true" />
              AI 设置
            </button>
          )}
          {view === 'learn' && (
            <button className="quiet-button" onClick={goHome}>返回书架</button>
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

      <main className={view === 'learn' ? 'learn-main' : view === 'profile' ? 'profile-main' : 'marketing-main'}>
        {error && <div className="global-error">{error}</div>}
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
            onCreate={async (body, idempotencyKey) => {
              const value = await run('AI 正在规划系列…', () => api.createPlan({ ...body, shelfId: shelf.id }, idempotencyKey));
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
                if (!refreshedShelf) setView('home');
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
            privacy={auth.privacy}
            onRequestExit={requestAccountExit}
          />
        )}
        {view === 'learn' && series && (
          <>
            {((data?.milestoneDashboard.path?.seriesId === series.id
              && (data.milestoneDashboard.path.status === 'proposed' || !data.milestoneDashboard.path.goalAligned))
              || (series.initializationTask && series.initializationTask.status !== 'succeeded')) && (
              <div className="workspace-alert-stack">
                {data?.milestoneDashboard.path?.seriesId === series.id
                  && (data.milestoneDashboard.path.status === 'proposed' || !data.milestoneDashboard.path.goalAligned) && (
                  <div className="path-confirm-alert" role="status">
                    <span>
                      <b>{data.milestoneDashboard.path.goalAligned ? '确认这条学习路径' : '学习画像已更新'}</b>
                      <small>{data.milestoneDashboard.path.goalAligned
                        ? '确认后，这个系列会按当前目标记录里程碑。'
                        : '请检查这个系列是否仍然符合你的最新目标。'}</small>
                    </span>
                    <button
                      className="secondary-button"
                      onClick={async () => {
                        await run('正在确认里程碑路径…', () => api.confirmMilestonePath(series.id));
                        setData(await api.bootstrap());
                      }}
                    >
                      {data.milestoneDashboard.path.goalAligned ? '确认路径' : '按新目标重新确认'}
                    </button>
                  </div>
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
                    ? series.initializationTask.errorMessage || series.initializationTask.errorCode || '未知错误'
                    : '完成后会自动打开，不需要重复点击生成。'}
                  {' '}（尝试 {series.initializationTask.attemptCount || 0}/{series.initializationTask.maxAttempts || 0}）
                </span>
                {series.initializationTask.retryable && (
                  <button
                    className="secondary-button"
                    disabled={preparingInitialSection}
                    onClick={retryInitialSection}
                  >
                    {preparingInitialSection ? '正在重试…' : '安全重试第一节'}
                  </button>
                )}
              </div>
                )}
              </div>
            )}
            <LearningWorkspace
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
            />
          </>
        )}
      </main>
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
      <button
        className="global-feedback-tab"
        aria-label="反馈产品问题或建议"
        onClick={() => setFeedbackTarget({ scope: 'global' })}
      >
        <span aria-hidden="true">✦</span> 反馈
      </button>
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
      : dialog?.querySelector<HTMLElement>('input[type="radio"]:checked');
    (initialFocus || dialog)?.focus();

    const handleDialogKeys = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        if (!submittingRef.current) onCloseRef.current();
        return;
      }
      if (event.key !== 'Tab' || !dialog) return;
      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(
        'button:not(:disabled), input:not(:disabled), textarea:not(:disabled), [href], [tabindex]:not([tabindex="-1"])',
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
      const result = await api.streamFeedbackRepair(
        feedbackId,
        (delta) => setRepairText((current) => current + delta),
      );
      if (target.scope === 'content_block') {
        const updated = await api.section(target.sectionId);
        onSectionChange(updated);
        await onRefreshSeries();
        setStatus(`已应用为第 ${result.contentVersion} 版正文。`);
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
        setStatus('已收到。我们会把它放进下一次反馈整理。');
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
        FEEDBACK_CONTENT_VERSION_STALE: '反馈已保存，但当前正文已有更新版本；请刷新后在新版本上再次反馈。',
        SECTION_CONTENT_MISSING: '反馈已保存，但这段正文已经不可用；请刷新后再试。',
      };
      setStatus(
        blockedMessages[regeneration.reasonCode || '']
        || '反馈已保存，但这段正文现在无法补救。',
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
          {!submitted && <fieldset disabled={submitting}>
            <legend>这次想反馈什么？</legend>
            <div className="feedback-type-grid">
              {options.map(([value, label]) => (
                <label className={feedbackType === value ? 'selected' : ''} key={value}>
                  <input
                    type="radio"
                    name="feedback-type"
                    value={value}
                    checked={feedbackType === value}
                    onChange={() => setFeedbackType(value)}
                  />
                  {label}
                </label>
              ))}
            </div>
          </fieldset>}
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
          : `已保存并切换到 ${value.model}，重启后会自动恢复。`,
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
              : '配置仅保存在本机服务端，浏览器无法读取 API Key；API 重启后会自动恢复。'}
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

function shelfDescriptor(shelf: Pick<Shelf, 'domain' | 'specialty'>) {
  return [shelf.domain, shelf.specialty].filter(Boolean).join(' · ');
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
  if (book.outlineStatus === 'draft') return '待校准';
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
          <p className="library-kicker">知行书架 · Personal library</p>
          <h1>把正在学的，<br /><em>放回眼前。</em></h1>
          <p>{dailyMode === 'fast'
            ? '先用几分钟抓住一个关键判断；正式验证仍沿用同一份教材与标准。'
            : '这里不是藏书统计，而是你的学习入口。先继续今天的一节，再回到所属领域查看完整路径。'}</p>
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
              <p>创建书架与教材后，下一节会固定出现在这里。</p>
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
              <p>复习分配与学习画像都由服务端确认。</p>
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
              <span>{reviewResult.passed ? '保持验证完成' : '已记录本次表现'}</span>
              <h2>{reviewResult.score} / {reviewResult.total}</h2>
              <p>这次结果已作为延迟复习证据候选保存，不会覆盖原小节测验。</p>
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
              <p>系统会生成一道与原题实质不同的题，检查间隔一段时间后是否仍能独立判断。</p>
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
            <div className="review-empty-state">
              <span>今日已清空</span>
              <h2>没有到期的复习</h2>
              <p>完成测验后，薄弱概念会按间隔自动回到这里。到期时可直接开始，不需要先打开队列。</p>
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
          <p>每个书架保存该领域的教材、学习记录与掌握证据。</p>
        </header>

        <div className="library-shelf-grid">
        {data && data.shelves.length === 0 && (
          <div className="empty-library-message">
            <span>还没有书架</span>
            <small>从一个明确的领域开始，把教材、测验与掌握证据放在一起。</small>
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
            featuredSeries.id === dashboard?.today?.seriesId && book.title === dashboard.today.bookTitle
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
            && featuredBook.title === dashboard.today.bookTitle,
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
                  <small>{shelfDescriptor(item) || '未设置领域说明'}</small>
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
                  <small>进入书架创建学习系列后，当前教材会固定显示在这里。</small>
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
            <small>同意会按版本单独留痕；未来内容变化时会重新询问。</small>
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
        <p>Slow 已停止这个账号的新写入。运营者应在 <b>{due}</b> 前完成活动数据库中的删除或去标识化。</p>
        <dl>
          <div><dt>申请编号</dt><dd>{receipt.requestId}</dd></div>
          <div><dt>处理状态</dt><dd>待运营者处理</dd></div>
          <div><dt>备份副本</dt><dd>受限轮转，14 日清除目标</dd></div>
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
};

const PROFILE_PREFERENCE_OPTIONS = {
  openingStyle: [
    ['auto', '由内容决定', '根据本节问题自动选择'],
    ['problem_first', '问题先行', '先抛出需要解决的问题'],
    ['example_first', '例子先行', '先从一个具体场景进入'],
    ['concept_first', '概念先行', '先建立准确的定义与框架'],
  ],
  explanationDensity: [
    ['auto', '由内容决定', '按知识难度自动调整'],
    ['concise', '更精炼', '减少铺垫，保留关键推理'],
    ['balanced', '适中', '解释与节奏保持平衡'],
    ['thorough', '更充分', '多展开机制、边界与反例'],
  ],
  interactionRhythm: [
    ['auto', '由内容决定', '按学习任务自动安排'],
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
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [showExitRequest, setShowExitRequest] = useState(false);
  const [exitConfirmation, setExitConfirmation] = useState('');
  const [exitReason, setExitReason] = useState('');
  const [exitSubmitting, setExitSubmitting] = useState(false);

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
        },
      });
      setMessage(`已保存为学习画像 V${profile.version + 1}。已有测验与掌握证据保持不变。`);
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
          <span>当前画像版本</span><b>V{profile.version}</b>
          <small>每次自述修改都会留下新版本。</small>
        </div>
        <button type="button" className="profile-back-button" onClick={onBack}><span aria-hidden="true">←</span> 返回书架</button>
      </aside>

      {section === 'profile' ? (
        <form className="profile-center-form" onSubmit={(event) => void submit(event)}>
          <header className="profile-page-heading">
            <p className="eyebrow">学习画像</p>
            <h1 id="profile-center-title">让教材始终认识<br />现在的你。</h1>
            <p>这是所有书架共用的学习设置。修改会形成新版本，但不会改写已经产生的测验、笔记与掌握证据。</p>
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
              <small>当前识别：{domains.length ? domains.join(' · ') : '尚未填写'}</small>
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
            <p className="profile-preference-intro">这些选项只在多个正确、有效的教学方案之间排序。若图表或类比并不适合当前知识，教材仍会选择更清楚的文字或其他形式。</p>

            <div className="profile-preference-section">
              <span className="profile-preference-label">怎样进入一个新问题</span>
              <div className="profile-choice-grid">
                {PROFILE_PREFERENCE_OPTIONS.openingStyle.map(([value, label, note]) => (
                  <label className={openingStyle === value ? 'selected' : ''} key={value}>
                    <input type="radio" name="opening-style" value={value} checked={openingStyle === value} onChange={() => setOpeningStyle(value)} />
                    <span><b>{label}</b><small>{note}</small></span>
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
                    <span><b>{label}</b><small>{note}</small></span>
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
                    <span><b>{label}</b><small>{note}</small></span>
                  </label>
                ))}
              </div>
            </div>
          </fieldset>

          <fieldset className="profile-field-group">
            <legend>学习节奏</legend>
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

          <div className="profile-version-note"><span>版本化自述</span><p>保存后，相关学习系列会在各自页面提示你重新确认路径。</p></div>
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
            <p>书架、阅读位置、测验证据和画像版本都绑定到当前账号，不会与其他用户混合。</p>
          </header>

          <div className="account-summary-card">
            <span className="profile-avatar" aria-hidden="true">{user.name.trim().slice(0, 1).toUpperCase() || '我'}</span>
            <div><small>当前登录账号</small><h2>{user.name}</h2><p>{modeLabel}</p></div>
            <dl>
              <div><dt>书架</dt><dd>{stats.shelves}</dd></div>
              <div><dt>学习系列</dt><dd>{stats.series}</dd></div>
              <div><dt>画像版本</dt><dd>V{profile.version}</dd></div>
            </dl>
          </div>

          <div className="account-policy-grid">
            <article>
              <span>数据归属</span>
              <h3>学习事实属于当前账号</h3>
              <p>书架、答题记录、复习状态和掌握证据均由服务端按用户隔离。</p>
            </article>
            <article>
              <span>登录安全</span>
              <h3>浏览器只保存安全会话</h3>
              <p>{mode === 'local' || mode === 'password'
                ? '密码不会写入浏览器存储，退出后服务端会话立即撤销。'
                : '身份由登录服务确认，Slow 不接收身份提供商密码。'}</p>
            </article>
            <article>
              <span>内测同意</span>
              <h3>当前告知已留下版本记录</h3>
              <p>版本 {privacy.noticeVersion} · {privacy.acceptedAt
                ? new Date(privacy.acceptedAt).toLocaleDateString('zh-CN')
                : '当前环境无需同意'}</p>
            </article>
          </div>

          <section className="account-exit-panel">
            <div><h3>退出当前账号</h3><p>退出不会删除任何书架或学习记录，下次登录后可继续。</p></div>
            <button type="button" disabled={saving} onClick={() => void onLogout()}>退出账号</button>
          </section>

          <section className="account-deletion-panel">
            <div className="account-deletion-copy">
              <span>不可撤销操作</span>
              <h3>退出试点并申请删除数据</h3>
              <p>提交后会立即撤销全部会话、停用账号并停止新写入。运营者应在 7 日内处理活动数据库；备份副本进入受限轮转，当前试点以 14 日为清除目标。</p>
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
  const [domain, setDomain] = useState('');
  const [specialty, setSpecialty] = useState('');
  const [tags, setTags] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState('');

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !submitting) onClose();
    };
    document.addEventListener('keydown', closeOnEscape);
    return () => document.removeEventListener('keydown', closeOnEscape);
  }, [onClose, submitting]);

  const parsedTags = Array.from(new Set(
    tags
      .split(/[,，]/)
      .map((item) => item.trim())
      .filter(Boolean),
  ));

  const send = async (event: FormEvent) => {
    event.preventDefault();
    if (submitting) return;
    if (parsedTags.length > 12) {
      setFormError('主题标签最多填写 12 个');
      return;
    }
    setSubmitting(true);
    setFormError('');
    try {
      await onCreate({
        name: name.trim(),
        domain: domain.trim(),
        specialty: specialty.trim(),
        tags: parsedTags,
      });
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
            <p className="eyebrow">建立一个学习领域</p>
            <h2 id="shelf-create-title">创建书架</h2>
            <p>书架用于归拢同一领域的书、学习记录和概念掌握证据。</p>
          </div>
          <button className="dialog-close" aria-label="关闭创建书架" disabled={submitting} onClick={onClose}>×</button>
        </div>

        <div className="shelf-create-nameplate" aria-label="书架铭牌预览">
          <span>{(name.trim() || '新').slice(0, 1)}</span>
          <div>
            <small>书架铭牌预览</small>
            <b>{name.trim() || '新书架'}</b>
            {(domain.trim() || specialty.trim()) && (
              <em>{[domain.trim(), specialty.trim()].filter(Boolean).join(' · ')}</em>
            )}
          </div>
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
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          <label>
            学习领域（可选）
            <input
              maxLength={100}
              disabled={submitting}
              value={domain}
              onChange={(event) => setDomain(event.target.value)}
            />
          </label>
          <label>
            细分方向（可选）
            <input
              maxLength={120}
              disabled={submitting}
              value={specialty}
              onChange={(event) => setSpecialty(event.target.value)}
            />
          </label>
          <label>
            主题标签（可选）
            <input
              maxLength={240}
              disabled={submitting}
              value={tags}
              onChange={(event) => {
                setTags(event.target.value);
                setFormError('');
              }}
            />
            <small>最多 12 个，用于后续检索和画像归类。</small>
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
  onCreate,
  onOpen,
  onDelete,
}: {
  shelf: Shelf;
  profile: LearningProfile;
  onCreate: (body: object, idempotencyKey: string) => Promise<void>;
  onOpen: (id: string) => void;
  onDelete: (id: string) => Promise<void>;
}) {
  const [showPlan, setShowPlan] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Series | null>(null);
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    if (!deleteTarget) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !deleting) setDeleteTarget(null);
    };
    document.addEventListener('keydown', closeOnEscape);
    return () => document.removeEventListener('keydown', closeOnEscape);
  }, [deleteTarget, deleting]);

  return (
    <section className="landing-section">
      {shelfDescriptor(shelf) && <p className="eyebrow">{shelfDescriptor(shelf)}</p>}
      <div className="title-row">
        <div>
          <h1>{shelf.name}</h1>
          <p className="lead">选择一个系列继续学习，或规划新的学习主题。</p>
        </div>
        <button className="primary-button" onClick={() => setShowPlan(!showPlan)}>＋ 创建学习系列</button>
      </div>
      {showPlan && <PlanForm profile={profile} submit={onCreate} />}
      <div className="series-shelf-heading">
        <span>技术书架 · 第 1 层</span>
        <small>{shelf.series.length} 册在架</small>
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
                <p>{item.rationale}</p>
                <span className="series-volume-progress">
                  <i><b style={{ width: `${item.progress}%` }} /></i>
                  <small>{item.progress}%</small>
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
            className="delete-confirm"
            role="dialog"
            aria-modal="true"
            aria-labelledby="delete-series-title"
          >
            <span className="delete-confirm-icon"><TrashIcon size={20} /></span>
            <p className="eyebrow">删除学习系列</p>
            <h2 id="delete-series-title">{deleteTarget.title}</h2>
            <p>该系列及其书、章节会从书架和学习入口中移除。历史学习证据会保留用于审计，当前界面暂不支持恢复。</p>
            <div>
              <button className="quiet-button" disabled={deleting} onClick={() => setDeleteTarget(null)}>取消</button>
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

function PlanForm({ profile, submit }: { profile: LearningProfile; submit: (body: object, idempotencyKey: string) => Promise<void> }) {
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
    <form className="plan-form" onSubmit={send}>
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
        {[['overview', '简单了解'], ['deep', '深度学习'], ['mastery', '掌握路径']].map(([value, label]) => (
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
            {label}
          </button>
        ))}
        {formError && <p className="plan-form-error" id="plan-depth-error" role="alert">{formError}</p>}
      </fieldset>
      <button className="primary-button" disabled={submitting}>{submitting ? '正在生成，请稍候…' : '生成目录方案'}</button>
    </form>
  );
}

function LearningWorkspace({
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
}: {
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
}) {
  const [selectedBlockId, setSelectedBlockId] = useState('');
  const [selectedQuote, setSelectedQuote] = useState<TextQuote | null>(null);
  const [compactLayout, setCompactLayout] = useState(() => window.matchMedia('(max-width: 900px)').matches);
  const [directoryHidden, setDirectoryHidden] = useState(() => window.matchMedia('(max-width: 900px)').matches);
  const [qaHidden, setQaHidden] = useState(() => window.matchMedia('(max-width: 900px)').matches);

  useEffect(() => {
    const media = window.matchMedia('(max-width: 900px)');
    const adaptPanels = (event: MediaQueryListEvent) => {
      setCompactLayout(event.matches);
      setDirectoryHidden(event.matches);
      setQaHidden(event.matches);
    };
    media.addEventListener('change', adaptPanels);
    return () => media.removeEventListener('change', adaptPanels);
  }, []);

  useEffect(() => {
    setSelectedBlockId(section?.content?.blocks[0]?.id || '');
    setSelectedQuote(null);
  }, [section?.id, section?.content?.id]);

  const location = useMemo(() => findSectionLocation(series, section?.id), [series, section?.id]);
  const activeBlockId = selectedBlockId || section?.content?.blocks[0]?.id || '';
  const selectBlock = (blockId: string) => {
    setSelectedBlockId(blockId);
    setSelectedQuote(null);
  };
  const toggleDirectory = () => {
    if (compactLayout && directoryHidden) setQaHidden(true);
    setDirectoryHidden((hidden) => !hidden);
  };
  const toggleQa = () => {
    if (compactLayout && qaHidden) setDirectoryHidden(true);
    setQaHidden((hidden) => !hidden);
  };

  return (
    <div className={`learning-workspace mode-${dailyMode} ${directoryHidden ? 'directory-collapsed' : ''} ${qaHidden ? 'qa-collapsed' : ''}`}>
      {compactLayout && (!directoryHidden || !qaHidden) && (
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
        section={section}
        dailyMode={dailyMode}
        directoryHidden={directoryHidden}
        qaHidden={qaHidden}
        onToggleDirectory={toggleDirectory}
        onToggleQa={toggleQa}
        location={location}
        selectedBlockId={activeBlockId}
        onQuote={(quote) => {
          setSelectedBlockId(quote.blockId);
          setSelectedQuote(quote);
        }}
        onGenerate={() => section && onGenerateSection(section.id)}
        onRegenerate={() => (section ? onRegenerateSection(section.id) : Promise.resolve())}
        onSelectSection={onSelectSection}
        onSectionChange={onSectionChange}
        onRefreshSeries={onRefreshSeries}
        onFeedbackBlock={onFeedbackBlock}
      />
      <QaPanel
        key={section?.id || 'empty'}
        section={section}
        dailyMode={dailyMode}
        hidden={qaHidden}
        onClose={() => setQaHidden(true)}
        selectedBlockId={activeBlockId}
        selectedQuote={selectedQuote}
        onAnchor={selectBlock}
        onClearQuote={() => setSelectedQuote(null)}
      />
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

  useEffect(() => {
    if (!deleteTarget) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !deleting) setDeleteTarget(null);
    };
    document.addEventListener('keydown', closeOnEscape);
    return () => document.removeEventListener('keydown', closeOnEscape);
  }, [deleteTarget, deleting]);

  return (
    <aside className="directory-panel" id="course-directory-panel" aria-label="课程目录" hidden={hidden}>
      <div className="directory-heading">
        <button className="panel-drawer-close" aria-label="关闭目录" onClick={onClose}>×</button>
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
            className="delete-confirm"
            role="dialog"
            aria-modal="true"
            aria-labelledby="delete-book-title"
          >
            <span className="delete-confirm-icon"><TrashIcon size={20} /></span>
            <p className="eyebrow">删除书籍</p>
            <h2 id="delete-book-title">{deleteTarget.title}</h2>
            <p>
              书籍及其章节会从学习入口中移除，历史学习证据和审计记录仍会保留。
              {series.books.length === 1 ? '这是系列中的最后一本书，删除后该系列也会从书架隐藏。' : ''}
              当前界面暂不支持恢复。
            </p>
            <div>
              <button className="quiet-button" disabled={deleting} onClick={() => setDeleteTarget(null)}>取消</button>
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
              ? '章节草案'
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
            <b>{canActivate ? '可以校准下一本书' : '章节目录为初始草案'}</b>
            <small>
              {canActivate
                ? '将使用刚完成的测验、Ask Me 与复习证据重新规划；确认后才解锁。'
                : '完成上一本书后，系统会按届时的学习画像重新规划。'}
            </small>
          </span>
          {canActivate && (
            <button className="secondary-button" onClick={() => onActivate(book)}>
              校准并确认章节
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
                    ? `第 ${chapter.position} 章 ${chapter.title}，未解锁；完成上一章后按学习需要生成小节`
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
                  {chapterLocked
                    ? '完成上一章后解锁，并按学习需要生成小节'
                    : '点击章名，按学习需要生成本章小节'}
                </small>
              </div>
            )}
            {chapter.practice && (
              <ArtifactSubmission
                kind="practice"
                id={chapter.id}
                status={chapter.practice.status}
                attachmentCount={chapter.practice.attachments.length}
                onSubmit={async (action) => {
                  await action();
                  await onRefreshSeries();
                }}
              />
            )}
            </div>
          );
        })}
        {book.capstone && (
          <ArtifactSubmission
            kind="capstone"
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
      title={preparing ? '正文和验证题准备完成后即可进入' : undefined}
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
  section,
  dailyMode,
  directoryHidden,
  qaHidden,
  onToggleDirectory,
  onToggleQa,
  location,
  selectedBlockId,
  onQuote,
  onGenerate,
  onRegenerate,
  onSelectSection,
  onSectionChange,
  onRefreshSeries,
  onFeedbackBlock,
}: {
  section: Section | null;
  dailyMode: DailyMode;
  directoryHidden: boolean;
  qaHidden: boolean;
  onToggleDirectory: () => void;
  onToggleQa: () => void;
  location: ReturnType<typeof findSectionLocation>;
  selectedBlockId: string;
  onQuote: (quote: TextQuote) => void;
  onGenerate: () => void;
  onRegenerate: () => Promise<void>;
  onSelectSection: (id: string) => Promise<Section>;
  onSectionChange: (section: Section) => void;
  onRefreshSeries: () => Promise<void>;
  onFeedbackBlock: (block: Block) => void;
}) {
  const [tab, setTab] = useState<ReaderTab>('content');
  const [selectionPopup, setSelectionPopup] = useState<SelectionPopup | null>(null);
  const [regenerationConfirmOpen, setRegenerationConfirmOpen] = useState(false);
  const [regenerating, setRegenerating] = useState(false);
  const [regenerationStartedAt, setRegenerationStartedAt] = useState(0);
  const [regenerationClock, setRegenerationClock] = useState(Date.now());
  const readerScrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setTab('content');
    setSelectionPopup(null);
    setRegenerationConfirmOpen(false);
    if (readerScrollRef.current) readerScrollRef.current.scrollTop = 0;
  }, [section?.id]);

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
    setTab(nextTab);
    requestAnimationFrame(() => {
      if (readerScrollRef.current) readerScrollRef.current.scrollTop = 0;
    });
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
  const maxSourceAttempts =
    typeof generationTrace.maxSourceAttempts === 'number'
      ? generationTrace.maxSourceAttempts
      : 4;
  const maxQuizAttempts =
    typeof generationTrace.maxQuizAttempts === 'number'
      ? generationTrace.maxQuizAttempts
      : 4;
  const generationRound =
    typeof generationTrace.sourceAttempt === 'number'
      ? `来源第 ${generationTrace.sourceAttempt}/${maxSourceAttempts} 轮`
      : typeof generationTrace.quizAttempt === 'number'
        ? `测验第 ${generationTrace.quizAttempt}/${maxQuizAttempts} 轮`
        : '';
  const generationElapsed = Math.max(
    activeGeneration?.durationMs || 0,
    regenerationStartedAt ? regenerationClock - regenerationStartedAt : 0,
  );

  if (!section) {
    return (
      <main className="reader-panel empty-reader">
        <ReaderPanelToggles
          directoryHidden={directoryHidden}
          qaHidden={qaHidden}
          onToggleDirectory={onToggleDirectory}
          onToggleQa={onToggleQa}
        />
        <span className="empty-symbol">S</span>
        <p className="eyebrow">选择左侧目录开始</p>
        <h1>今天，学清楚一个问题。</h1>
        <p>完成一节、通过验证、留下笔记。下一节会在掌握后自动解锁。</p>
      </main>
    );
  }

  return (
    <main className="reader-panel">
      <ReaderPanelToggles
        directoryHidden={directoryHidden}
        qaHidden={qaHidden}
        onToggleDirectory={onToggleDirectory}
        onToggleQa={onToggleQa}
      />
      <div className="reader-toolbar">
        <div>
          <p className="breadcrumb">
            第 {location?.book.position} 本
            <i>›</i>
            第 {location?.chapter.position} 章 · {location?.chapter.title}
            <i>›</i>
            {location?.chapter.position}.{section.position}
          </p>
          <h1>{section.title}</h1>
        </div>
        <div className="reader-toolbar-actions">
          <span className={`lesson-status ${section.status}`}>
            {section.status === 'completed'
              ? '已完成'
              : section.status === 'available'
                ? '学习中'
                : section.status === 'preparing'
                  ? '准备中'
                  : '未解锁'}
          </span>
          {section.content && section.bestScore === 0 && section.totalScore === 0 && (
            <button
              className="quiet-button regenerate-trigger"
              disabled={regenerating}
              onClick={() => setRegenerationConfirmOpen(true)}
            >
              重新生成本节
            </button>
          )}
        </div>
      </div>

      <div className="reader-tabs" role="tablist">
        <button className={tab === 'content' ? 'active' : ''} onClick={() => switchTab('content')}>正文</button>
        <button className={tab === 'quiz' ? 'active' : ''} disabled={!section.quiz} onClick={() => switchTab('quiz')}>验证</button>
        <button className={tab === 'note' ? 'active' : ''} disabled={!section.note} onClick={() => switchTab('note')}>笔记</button>
      </div>

      <div
        className="reader-scroll"
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
            onGenerate={onGenerate}
            onStartQuiz={() => switchTab('quiz')}
            onFeedbackBlock={onFeedbackBlock}
          />
        )}
        {tab === 'quiz' && section.quiz && (
          <Quiz
            key={section.quiz.id}
            section={section}
            onSectionChange={onSectionChange}
            onRefreshSeries={onRefreshSeries}
            onSelectSection={onSelectSection}
            onReviewContent={() => switchTab('content')}
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
            className="delete-confirm regenerate-confirm"
            role="dialog"
            aria-modal="true"
            aria-labelledby="regenerate-section-title"
          >
            <p className="eyebrow">重新生成本节</p>
            <h2 id="regenerate-section-title">{section.title}</h2>
            <p>系统会生成新的正文与测验版本，旧版本会保留在审计记录中。已经提交过测验的内容不能重新生成，以免改写学习证据。</p>
            {regenerating && (
              <div className="regeneration-progress" aria-live="polite">
                <span><i />{GENERATION_STAGE_LABELS[generationStage] || '正在处理'}</span>
                <b>{generationRound || '准备中'} · 已用 {formatElapsed(generationElapsed)}</b>
                <small>正文来源核验通过后才会生成测验。</small>
              </div>
            )}
            <div>
              <button className="quiet-button" disabled={regenerating} onClick={() => setRegenerationConfirmOpen(false)}>取消</button>
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

function ReaderPanelToggles({
  directoryHidden,
  qaHidden,
  onToggleDirectory,
  onToggleQa,
}: {
  directoryHidden: boolean;
  qaHidden: boolean;
  onToggleDirectory: () => void;
  onToggleQa: () => void;
}) {
  return (
    <>
      <button
        className={`reader-rail-toggle directory-toggle ${directoryHidden ? 'is-collapsed' : ''}`}
        aria-controls="course-directory-panel"
        aria-expanded={!directoryHidden}
        aria-label={directoryHidden ? '显示目录' : '隐藏目录'}
        title={directoryHidden ? '显示目录' : '隐藏目录'}
        onClick={onToggleDirectory}
      >
        {directoryHidden ? '›' : '‹'}
      </button>
      <button
        className={`reader-rail-toggle qa-toggle ${qaHidden ? 'is-collapsed' : ''}`}
        aria-controls="section-qa-panel"
        aria-expanded={!qaHidden}
        aria-label={qaHidden ? '显示答疑' : '隐藏答疑'}
        title={qaHidden ? '显示答疑' : '隐藏答疑'}
        onClick={onToggleQa}
      >
        {qaHidden ? '‹' : '›'}
      </button>
    </>
  );
}

function selectFastBlocks(blocks: Block[]) {
  const selected: Block[] = [];
  const add = (block?: Block) => {
    if (block && !selected.some((item) => item.id === block.id)) selected.push(block);
  };
  add(blocks.find((block) => block.role === 'conclusion'));
  add(blocks.find((block) => block.role === 'mechanism'));
  add(blocks.find((block) => block.role === 'example'));
  add(blocks.find((block) => block.role === 'boundary'));
  blocks.forEach((block) => {
    if (selected.length < 4) add(block);
  });
  return selected.slice(0, 4);
}

function LessonContent({
  section,
  dailyMode,
  selectedBlockId,
  onGenerate,
  onStartQuiz,
  onFeedbackBlock,
}: {
  section: Section;
  dailyMode: DailyMode;
  selectedBlockId: string;
  onGenerate: () => void;
  onStartQuiz: () => void;
  onFeedbackBlock: (block: Block) => void;
}) {
  const [showCompleteFast, setShowCompleteFast] = useState(false);
  const [fastCheck, setFastCheck] = useState<'clear'|'unclear'|null>(null);

  useEffect(() => {
    setShowCompleteFast(false);
    setFastCheck(null);
  }, [section.id, section.content?.id, dailyMode]);

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
          <div className="inline-error">{section.generation.error || '上次生成失败，可安全重试。'}</div>
        )}
        <button className="primary-button large" onClick={onGenerate}>
          {section.generation?.status === 'failed' ? '安全重试' : '生成正文并开始学习'}
        </button>
        <small className="generation-note">正文与测验将一次生成，并在发布前校验 Learning Contract、目标绑定和证据引用。</small>
      </div>
    );
  }

  const visibleSources = section.content.sources.flatMap((source, index) => (
    section.content?.sourceVerification[index]?.verificationStatus === 'failed'
      ? []
      : [{ source, index }]
  ));
  const fastBlocks = selectFastBlocks(section.content.blocks);
  const shownBlocks = dailyMode === 'fast' && !showCompleteFast
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
          ? '演示内容 · 不代表真实 AI 生成或事实核验'
          : section.content.boundaryValidation.status === 'passed'
            ? `${section.content.aiGenerated ? 'AI 生成' : '授权内容'} · 已通过结构、契约和证据边界校验 · 未经逐项事实核验`
            : section.content.boundaryValidation.status === 'legacy'
              ? '历史内容 · 未确认通过当前结构、契约和证据边界校验 · 未经逐项事实核验'
              : `${section.content.aiGenerated ? 'AI 生成' : '授权内容'} · 未确认通过结构、契约和证据边界校验 · 未经逐项事实核验`}
      </p>
      {dailyMode === 'fast' && (
        <aside className="fast-view-notice">
          <div>
            <span>FAST VIEW · 快速浏览</span>
            <b>{showCompleteFast ? '已展开完整正文' : `保留 ${fastBlocks.length} 个关键段落`}</b>
            <p>这里只调整呈现密度。快速浏览不会计为完成；通过验证后才会记录进度。</p>
          </div>
          <button type="button" onClick={() => setShowCompleteFast((shown) => !shown)}>
            {showCompleteFast ? '收回快速视图' : '展开完整正文'}
          </button>
        </aside>
      )}
      {shownBlocks.map((block) => (
        <ContentBlock
          key={block.id}
          block={block}
          selected={block.id === selectedBlockId}
          onFeedback={() => onFeedbackBlock(block)}
        />
      ))}
      {dailyMode === 'fast' && !showCompleteFast && (
        <section className="fast-check" aria-label="快速自检">
          <span>30 秒自检 · 不影响进度</span>
          <h3>你能用一句话复述本节结论，并说出一个失效边界吗？</h3>
          <div>
            <button className={fastCheck === 'clear' ? 'selected' : ''} onClick={() => setFastCheck('clear')}>可以复述</button>
            <button className={fastCheck === 'unclear' ? 'selected' : ''} onClick={() => setFastCheck('unclear')}>边界还不清楚</button>
          </div>
          {fastCheck && <p>{fastCheck === 'clear'
            ? '很好。若要完成本节，请继续完成下方同一套验证题。'
            : '先展开完整正文，重点阅读边界段落；这次选择不会影响进度。'}</p>}
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
      <div className="lesson-complete-action">
        <span>正文阅读完成</span>
        <h3>现在，验证你是否真正理解。</h3>
        <p>完成选择题并达到及格线，才会解锁下一节；满分后还会开放“深入讨论”。</p>
        <button className="primary-button" onClick={onStartQuiz}>开始验证 <i>→</i></button>
      </div>
    </article>
  );
}

function ContentBlock({
  block,
  selected,
  onFeedback,
}: {
  block: Block;
  selected: boolean;
  onFeedback: () => void;
}) {
  const labels: Record<string, string> = {
    text: '阅读',
    diagram: '图解',
    table: '对照',
    code: '演练',
    formula: '推导',
  };
  return (
    <section
      className={`content-block role-${block.role} ${selected ? 'selected' : ''}`}
      data-block-id={block.id}
    >
      <div className="block-meta"><b>{labels[block.kind] || '阅读'}</b></div>
      <button
        className="block-feedback-button"
        type="button"
        aria-label={`反馈“${block.heading}”这一段`}
        onClick={onFeedback}
      >
        <span aria-hidden="true">↳</span> 反馈这段
      </button>
      <h2>{block.heading}</h2>
      <BlockBody block={block} />
    </section>
  );
}

function BlockBody({ block }: { block: Block }) {
  if (block.kind === 'code') {
    return <pre className="code-block"><code>{block.content}</code></pre>;
  }
  const markdown = block.kind === 'table'
    ? normalizeTableMarkdown(block.content)
    : block.kind === 'text'
      ? normalizeLessonTextMarkdown(block.content)
      : block.content;
  return (
    <div className={`content-markdown kind-${block.kind}`}>
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{markdown}</ReactMarkdown>
    </div>
  );
}

function normalizeLessonTextMarkdown(content: string): string {
  const normalized = content.replace(/\r\n?/g, '\n').trim();
  const hasAuthoredStructure = /\n\s*\n/.test(normalized)
    || /(^|\n)\s*(?:#{1,6}\s|[-+*]\s+|\d+[.)]\s+|>\s+|```)/m.test(normalized);
  if (normalized.length < 200 || hasAuthoredStructure) return normalized;

  const sentences = normalized
    .match(/[^。！？]+[。！？]+|[^。！？]+$/g)
    ?.map((sentence) => sentence.trim())
    .filter(Boolean) || [];
  if (sentences.length < 4) return normalized;

  const paragraphCount = Math.min(
    3,
    Math.floor(sentences.length / 2),
    Math.max(2, Math.ceil(normalized.length / 150)),
  );
  if (paragraphCount < 2) return normalized;

  const paragraphs: string[] = [];
  let cursor = 0;
  for (let index = 0; index < paragraphCount; index += 1) {
    const remainingSentences = sentences.length - cursor;
    const remainingParagraphs = paragraphCount - index;
    const take = Math.ceil(remainingSentences / remainingParagraphs);
    paragraphs.push(sentences.slice(cursor, cursor + take).join(''));
    cursor += take;
  }
  return paragraphs.join('\n\n');
}

function normalizeTableMarkdown(content: string): string {
  const lines = content.trim().split(/\r?\n/).filter((line) => line.trim());
  if (lines.length < 2) return content;

  const cells = (line: string) => line
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((cell) => cell.trim());
  const columnCount = cells(lines[0]).length;
  if (columnCount < 2) return content;

  const possibleDivider = cells(lines[1]);
  const hasDivider = possibleDivider.length === columnCount
    && possibleDivider.every((cell) => /^:?-{3,}:?$/.test(cell));
  if (!hasDivider) {
    lines.splice(1, 0, Array.from({ length: columnCount }, () => '---').join(' | '));
  }
  return lines.join('\n');
}

function Quiz({
  section,
  onSectionChange,
  onRefreshSeries,
  onSelectSection,
  onReviewContent,
  onSubmissionComplete,
}: {
  section: Section;
  onSectionChange: (section: Section) => void;
  onRefreshSeries: () => Promise<void>;
  onSelectSection: (id: string) => Promise<Section>;
  onReviewContent: () => void;
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
  const [workflowMessage, setWorkflowMessage] = useState('');
  const [failedTasks, setFailedTasks] = useState<LearningTask[]>([]);
  const [workflowTasks, setWorkflowTasks] = useState<LearningTask[]>(
    section.workflowTasks || [],
  );
  const [retryingTasks, setRetryingTasks] = useState(false);
  const [remediationReady, setRemediationReady] = useState(false);
  const [openingRemediation, setOpeningRemediation] = useState(false);
  const [reassessing, setReassessing] = useState(false);
  const [openingNextSection, setOpeningNextSection] = useState(false);
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
    setFailedTasks(preservedFailures);
    setWorkflowMessage(
      initialTasks.some((task) => task.type === 'remediation_generation')
        ? '评分已完成，正在准备补充教学和新的等价题…'
        : '评分已完成，正在准备个人笔记和下一节…',
    );
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
        setFailedTasks(failures);
        setWorkflowRunning(false);
        setWorkflowMessage(
          failures.length
            ? '评分结果已保存，但部分后续内容生成失败，可以安全重试。'
            : passed
              ? '个人笔记和下一节已经准备完成。'
              : '补充教学和新的等价题已经准备完成。',
        );
        if (!failures.length && passed === false) {
          setRemediationReady(true);
        }
        await onRefreshSeries();
        return;
      }
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
    }
    setWorkflowRunning(false);
    setWorkflowMessage('评分结果已保存，后续内容仍在后台处理中。');
  };

  useEffect(() => {
    const unfinished = (section.workflowTasks || []).filter(
      (task) => task.status !== 'succeeded',
    );
    if (!unfinished.length) return;
    void monitorTasks(unfinished).catch((reason) => {
      setWorkflowRunning(false);
      setSubmissionError(
        reason instanceof Error ? reason.message : '无法恢复后台任务状态。',
      );
    });
  }, [section.id]);

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
    } catch (reason) {
      setSubmissionError(reason instanceof Error ? reason.message : '任务重试失败。');
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
      setSubmissionError('这套验证题未通过发布门禁，系统不会接受作答。请重新生成后再试。');
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
      try {
        sessionStorage.setItem(quizResultStorageKey, JSON.stringify(reviewValue));
      } catch {
        // The current in-memory review remains available for this render.
      }
      setRemediationReady(false);
      localStorage.removeItem(quizDraftKey);
      localStorage.removeItem(quizRequestStorageKey);
      if (value.passed) {
        const next = await api.section(section.id);
        onSectionChange(next);
      }
      await onRefreshSeries();
      onSubmissionComplete();
      void monitorTasks(value.workflowTasks, value.passed).catch((reason) => {
        setWorkflowRunning(false);
        setSubmissionError(
          reason instanceof Error ? reason.message : '无法读取后续任务状态。',
        );
      });
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
      <p className="quiz-rule">答对至少 80% 且通过全部必需目标即可继续；错题会进入重点巩固与学习画像。</p>
      <p className="quiz-draft-note">单选题只能选择一个答案，多选题可选择多个；切回正文查阅时，当前作答会自动保留。</p>
      {quizGovernanceBlocked && (
        <aside className="quiz-governance-notice" role="alert">
          <b>这套验证题尚未通过发布门禁</b>
          <p>系统不会展示或接受这套题的作答，也不会用它评分、解锁或形成学习证据。请重新生成后再试。</p>
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
          onReviewContent={onReviewContent}
          onOpenRemediation={openRemediation}
          onReassess={reassessAttempt}
          onOpenNextSection={openNextSection}
        />
      ) : (
        <>
          {section.remediations.map((item) => (
            <section className="remediation-card" key={item.id}>
              <span>错题补充教学 · {item.strategy}</span>
              {item.blocks.map((block) => (
                <div key={block.id}>
                  <h3>{block.heading}</h3>
                  <BlockBody block={block} />
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
        {result && (
          <p className={result.passed ? 'result success' : 'result failure'}>
            {result.passed
              ? nextSectionTask
                ? '验证已通过，下一节正在准备；正文和验证题完成后即可进入。'
                : '验证已通过，学习结果已经保存。'
              : '本次未通过，评分结果已经保存。'}
          </p>
        )}
        {workflowMessage && <p className={failedTasks.length ? 'result failure' : 'result success'}>{workflowMessage}</p>}
        {workflowTasks.length > 0 && (
          <div className="workflow-task-list" aria-label="后台任务状态">
            {workflowTasks.map((task) => (
              <div className={`workflow-task ${task.status}`} key={task.taskId}>
                <span>{{
                  content_feedback_regeneration: '反馈修订',
                  initial_book_preload: '第一节准备',
                  note_generation: '个人笔记',
                  remediation_generation: '补充教学与新题',
                  next_section_preload: '下一节预加载',
                }[task.type]}</span>
                <b>{{
                  pending: '等待中',
                  running: '进行中',
                  succeeded: '已完成',
                  failed: '失败',
                }[task.status]}</b>
                <small>
                  尝试 {task.attemptCount || 0}/{task.maxAttempts || 0}
                  {task.status === 'failed'
                    ? ` · ${task.errorMessage || task.errorCode || '未知错误'}`
                    : ''}
                </small>
              </div>
            ))}
          </div>
        )}
        {failedTasks.some((task) => task.retryable) && (
          <button
            type="button"
            className="secondary-button"
            disabled={retryingTasks || workflowRunning}
            onClick={retryFailedTasks}
          >
            {retryingTasks ? '正在重试…' : '安全重试后续生成'}
          </button>
        )}
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
  onReviewContent,
  onOpenRemediation,
  onReassess,
  onOpenNextSection,
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
  onReviewContent: () => void;
  onOpenRemediation: () => Promise<void>;
  onReassess: () => Promise<void>;
  onOpenNextSection: () => Promise<void>;
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

  return (
    <section className="quiz-review" aria-labelledby="quiz-review-title">
      <header className={result.passed ? 'passed' : 'failed'}>
        <p className="eyebrow">评分已完成，无需等待 AI</p>
        <h3 id="quiz-review-title">
          {result.passed ? '本次验证已通过' : `有 ${wrongIndexes.length} 道题需要回看`}
        </h3>
        <strong>{result.score}<small> / {result.total}</small></strong>
        <p>
          {result.passed
            ? '答题事实已经保存，可以查看解析或回到正文。'
            : '先查看下面的即时错题解析；个性化补充教学会在后台继续准备。'}
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
              <button className="quiet-button" onClick={onReviewContent}>回看本节正文</button>
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

      <div className={`remediation-readiness ${remediationReady || result.passed || eligibleUnderCurrentPolicy ? 'ready' : ''}`}>
        {result.passed ? (
          nextSectionTask ? (
            nextSectionTask.status === 'succeeded' && nextSectionId ? (
              <>
                <span>下一节已经准备完成</span>
                <button
                  className="primary-button"
                  disabled={openingNextSection}
                  onClick={onOpenNextSection}
                >
                  {openingNextSection ? '正在进入…' : '进入下一节'}
                </button>
              </>
            ) : nextSectionTask.status === 'failed' ? (
              <>
                <span>本节已通过，下一节准备失败</span>
                <small>可以在下方安全重试，不会影响已经保存的成绩。</small>
              </>
            ) : (
              <>
                <span><i />本节已通过，正在直接生成下一节</span>
                <button className="primary-button" disabled>下一节准备中…</button>
              </>
            )
          ) : (
            <>
              <span>本节验证已经完成</span>
              <button className="secondary-button" onClick={onReviewContent}>返回正文</button>
            </>
          )
        ) : eligibleUnderCurrentPolicy ? (
          <>
            <span>按当前规则，答对 {result.score}/{result.total} 已达到继续学习标准</span>
            <button
              className="primary-button"
              disabled={reassessing}
              onClick={onReassess}
            >
              {reassessing ? '正在更新进度…' : '按当前规则继续'}
            </button>
            <small>错题仍会进入个人笔记和掌握画像，不会被视为已经掌握。</small>
          </>
        ) : remediationReady ? (
          <>
            <span>个性化补充教学和变式题已准备完成</span>
            <button
              className="primary-button"
              disabled={openingRemediation}
              onClick={onOpenRemediation}
            >
              {openingRemediation ? '正在打开…' : '开始补充教学与变式题'}
            </button>
          </>
        ) : (
          <>
            <span><i />个性化补充教学正在后台准备</span>
            <small>你可以继续阅读上面的错题解析，生成不会阻塞当前页面。</small>
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
      setMessage('已保存为新的个人版本；底稿与复习补充保持不变。');
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : '保存失败');
    }
  };
  return (
    <div className="note-view">
      <p className="eyebrow">完成后持续生长的学习资产</p>
      <h2>三层学习笔记</h2>
      <p className="note-intro">底稿记录首次通过时的理解，真正完成复习后只追加补充；你的改写单独保存，不会覆盖前两层。</p>

      <article className="note-layer note-summary-layer">
        <header>
          <span className="note-layer-index">01</span>
          <div><b>学习总结</b><small>首次通过时冻结的稳定底稿</small></div>
          {summary && <em>V{summary.version}</em>}
        </header>
        <NoteContentView content={summary?.content ?? note.aiContent} empty="本节还没有学习总结。" />
        {summary && (
          <footer>
            <span>内容版本 {summary.sourceContentVersionId ?? '旧数据'}</span>
            <span>契约 {summary.sourceContractVersion}</span>
            <span>观察水位 {summary.sourceObservationWatermark}</span>
          </footer>
        )}
      </article>

      <section className="note-layer note-review-layer">
        <header>
          <span className="note-layer-index">02</span>
          <div><b>复习补充</b><small>按完成时间追加，不改写底稿</small></div>
          <em>{note.layers.reviewSupplements.length} 条</em>
        </header>
        {note.layers.reviewSupplements.length === 0 ? (
          <p className="note-empty-layer">完成一次无辅助复习后，新的理解会出现在这里。单纯答对不会自动生成正文。</p>
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
          <span className="note-layer-index">03</span>
          <div><b>我的版本</b><small>只有你的保存操作会创建新版本</small></div>
          <em>{revision ? `V${revision.version}` : '未创建'}</em>
        </header>
        {revision && <NoteContentView content={revision.content} empty="当前个人版本为空。" />}
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
          <button className="primary-button" onClick={save}>保存为新的个人版本</button>
        </div>
      </article>

      <aside className="note-verification" aria-label="当前验证标注">
        <header><b>当前验证标注</b><span>只读 · 会随后续证据变化</span></header>
        <p>这些状态来自测量与掌握度投影，不属于笔记正文，也不会静默改写任何版本。</p>
        {note.verificationAnnotations.length === 0 ? (
          <small>目前没有可显示的验证标注。</small>
        ) : (
          <ul>
            {note.verificationAnnotations.map((annotation) => (
              <li key={annotation.assessmentTargetId}>
                <span><b>{annotation.objective}</b><small>{annotation.dimension}</small></span>
                <em>{annotation.claimStatus}</em>
                <strong>{Math.round(annotation.pKnown * 100)}%</strong>
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
    if (!message) return;
    const timer = window.setTimeout(() => setMessage(''), 2400);
    return () => window.clearTimeout(timer);
  }, [message]);

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
    setMessage('正在评估，本次回答只会记录一次…');
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
        crypto.randomUUID(),
      );
      setDiscussion(next);
      setAnswer('');
      setConfirmingFinish(false);
      turnRequest.current = null;
      setMessage(action === 'next_topic'
        ? '已切换主题。'
        : action === 'pause'
          ? '讨论已暂停，刷新页面后也可以继续。'
          : action === 'resume'
            ? '已恢复到上次讨论的位置。'
            : '讨论已结束，已完成主题的证据已写入掌握画像。');
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
                <p>已完成主题的回答与评估已形成学习证据；未讨论主题不会被假定为已经掌握。</p>
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
                    <p>你的主题、回答和反馈都已经保存。</p>
                    <button className="primary-button" disabled={actioning} onClick={() => applyAction('resume')}>
                      {actioning ? '正在恢复…' : '继续讨论'}
                    </button>
                  </div>
                ) : (
                  <div className="askme-composer">
                    <label htmlFor={`askme-answer-${sectionId}`}>
                      <span>{activeTurns.length ? '下一问' : '当前问题'}</span>
                      <strong>{displayedPrompt}</strong>
                    </label>
                    <textarea
                      id={`askme-answer-${sectionId}`}
                      value={answer}
                      disabled={submitting || actioning}
                      onChange={(event) => setAnswer(event.target.value)}
                      placeholder="写下你的判断、依据和不确定的地方…"
                    />
                    <div className="askme-composer-actions">
                      <button className="primary-button" disabled={submitting || actioning || !answer.trim()} onClick={submit}>
                        {submitting ? '正在评估…' : '提交回答'}
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
}: {
  section: Section | null;
  dailyMode: DailyMode;
  hidden: boolean;
  onClose: () => void;
  selectedBlockId: string;
  selectedQuote: TextQuote | null;
  onAnchor: (id: string) => void;
  onClearQuote: () => void;
}) {
  const [threadId, setThreadId] = useState<string>();
  const [newQuestion, setNewQuestion] = useState(false);
  const [question, setQuestion] = useState('');
  const [messages, setMessages] = useState<QaExchange[]>([]);
  const [asking, setAsking] = useState(false);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const messagesRef = useRef<HTMLDivElement>(null);
  const selectedBlock =
    section?.content?.blocks.find((block) => block.id === selectedBlockId) ??
    section?.content?.blocks[0];
  const effectiveBlockId = selectedBlock?.id ?? selectedBlockId;

  useEffect(() => {
    if (selectedQuote) composerRef.current?.focus();
  }, [selectedQuote]);

  useEffect(() => {
    const node = messagesRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [messages]);

  const ask = async () => {
    if (asking || !section || !effectiveBlockId || !question.trim()) return;
    const visibleQuestion = question.trim();
    const submittedQuestion = selectedQuote
      ? `请基于以下选中的正文回答。\n\n选中内容：${selectedQuote.text}\n\n问题：${visibleQuestion}`
      : visibleQuestion;
    const exchangeId = crypto.randomUUID();
    setMessages((current) => [
      ...current,
      { id: exchangeId, question: visibleQuestion, answer: '', relation: 'pending', status: 'streaming' },
    ]);
    setQuestion('');
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
      );
      setThreadId(result.threadId);
      setMessages((current) => current.map((message) => (
        message.id === exchangeId ? { ...message, relation: result.relation, status: 'done' } : message
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
        <button className="panel-drawer-close" aria-label="关闭答疑" onClick={onClose}>×</button>
        <span className="panel-label">答疑</span>
        <h2>围绕当前小节追问</h2>
        <p>{dailyMode === 'fast' ? '先结论、后三点；答疑不会影响学习进度。' : '答疑独立保存，不会打断正文阅读。'}</p>
      </div>
      {!section?.content ? (
        <div className="qa-empty">
          <span>?</span>
          <b>正文生成后即可提问</b>
          <p>选择正文中的具体段落，AI 会带着当前位置回答。</p>
        </div>
      ) : (
        <>
          <div className="anchor-card">
            <span>当前锚点</span>
            <b>{selectedBlock?.heading || '请选择正文段落'}</b>
            <select value={effectiveBlockId} onChange={(event) => onAnchor(event.target.value)}>
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
          <div className="qa-messages" ref={messagesRef}>
            {messages.length === 0 && (
              <div className="qa-suggestion">
                <span>可以这样问</span>
                <button onClick={() => setQuestion(dailyMode === 'fast' ? '用一句结论和三个要点解释这段。' : '这个机制最容易被误解的地方是什么？')}>{dailyMode === 'fast' ? '用一句结论和三个要点解释这段。' : '这个机制最容易被误解的地方是什么？'}</button>
                <button onClick={() => setQuestion('它在什么边界条件下会失效？')}>它在什么边界条件下会失效？</button>
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
              </div>
            ))}
          </div>
          <div className="qa-composer">
            <textarea
              ref={composerRef}
              value={question}
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
              <button disabled={asking || !question.trim()} onClick={ask}>{asking ? '回答中…' : '发送 ↑'}</button>
            </div>
          </div>
        </>
      )}
    </aside>
  );
}

function ArtifactSubmission({
  kind,
  id,
  status,
  attachmentCount,
  onSubmit,
}: {
  kind: 'practice' | 'capstone';
  id: string;
  status: string;
  attachmentCount: number;
  onSubmit: (action: () => Promise<unknown>) => Promise<void>;
}) {
  const label = kind === 'practice' ? '章末实践' : '全书大作业';
  const needsLegacyFile = status === 'completed' && attachmentCount === 0;
  const enabled = status === 'available' || needsLegacyFile;
  const upload = async (file: File) => {
    const attachment = kind === 'practice' ? await api.uploadPractice(id, file) : await api.uploadCapstone(id, file);
    return kind === 'practice'
      ? api.practice(id, { evidence: '由学习者提交', reflection: '已完成章末实践' }, [attachment.id])
      : api.capstone(id, { artifact: '全书综合成果', verification: '学习者复核记录' }, [attachment.id]);
  };
  return (
    <label className={`artifact-submit ${kind} ${enabled ? 'enabled' : ''}`}>
      <span className="artifact-icon">
        {status === 'locked' ? <LockIcon size={12} /> : kind === 'practice' ? '◇' : '◆'}
      </span>
      <span>{label}</span>
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
