import { FormEvent, useEffect, useMemo, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { api } from './api/client';
import type {
  AskMe,
  AiRuntime,
  AuthConfig,
  AuthState,
  Block,
  Book,
  Bootstrap,
  Chapter,
  LearningTask,
  Note as NoteType,
  QuizResult,
  Section,
  SectionSummary,
  Series,
  Shelf,
  ShelfCreateInput,
} from './model/types';

type View = 'home' | 'shelf' | 'learn';
type ReaderTab = 'content' | 'quiz' | 'note';
type TextQuote = { text: string; blockId: string };
type SelectionPopup = TextQuote & { top: number; left: number };
const AI_RUNTIME_SETTINGS_ENABLED = import.meta.env.DEV;
const GENERATION_STAGE_LABELS: Record<string, string> = {
  queued: '正在排队',
  content_generation: '正在生成正文',
  source_verification: '正在核验来源',
  source_repair: '正在替换无法核验的来源',
  quiz_generation: '正在生成测验',
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
  const [view, setView] = useState<View>('home');
  const [shelf, setShelf] = useState<Shelf | null>(null);
  const [series, setSeries] = useState<Series | null>(null);
  const [section, setSection] = useState<Section | null>(null);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [showAiSettings, setShowAiSettings] = useState(false);
  const [preparingInitialSection, setPreparingInitialSection] = useState(false);
  const [generatingChapterId, setGeneratingChapterId] = useState('');
  const chapterGenerationRequests = useRef(new Set<string>());

  const loadAuthenticatedState = async () => {
    const value = await api.authMe();
    const bootstrap = await api.bootstrap();
    setAuth(value);
    setData(bootstrap);
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
      setAuthChecked(true);
    };
    api.setUnauthorizedHandler(clearUserState);
    void initializeAuth();
  }, []);

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
      const bootstrap = await api.bootstrap();
      setAuth(state);
      setData(bootstrap);
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

  const loadSection = async (sectionId: string) => {
    const value = await run('正在读取小节…', () => api.section(sectionId));
    setSection(value);
    void api.updateResume(sectionId).catch(() => undefined);
    return value;
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
          if (targetSectionId) setSection(await api.section(targetSectionId));
          return true;
        }
        if (task.status === 'failed') {
          const refreshed = await api.series(value.id);
          setSeries(refreshed);
          const fallbackSectionId = firstUsableSection(refreshed);
          if (fallbackSectionId) setSection(await api.section(fallbackSectionId));
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
          </section>
        </main>
      </div>
    );
  }

  return (
    <div className="app-shell">
      <header className="app-header">
        <button
          className="brand"
          aria-label="返回 Slow 首页"
          onClick={() => {
            setView('home');
            setSeries(null);
            setSection(null);
          }}
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
        ) : (
          <small>一步一步，学成自己的书</small>
        )}
        <div className="header-actions">
          {busy && <span className="busy-indicator"><i />{busy}</span>}
          <span className="user-name">{auth.user.name}</span>
          {AI_RUNTIME_SETTINGS_ENABLED && (
            <button className="quiet-button ai-settings-trigger" onClick={() => setShowAiSettings(true)}>
              <span aria-hidden="true" />
              AI 设置
            </button>
          )}
          {view === 'learn' && (
            <button className="quiet-button" onClick={() => setView('home')}>返回书架</button>
          )}
          <button
            className="quiet-button"
            onClick={async () => {
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
              }
            }}
          >
            退出
          </button>
        </div>
      </header>

      <main className={view === 'learn' ? 'learn-main' : 'marketing-main'}>
        {error && <div className="global-error">{error}</div>}
        {view === 'home' && (
          <Home
            data={data}
            onOpen={openShelf}
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
        {view === 'learn' && series && (
          <>
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
            <LearningWorkspace
              series={series}
              section={section}
              onSelectSection={loadSection}
              onGenerateSection={generateSection}
              onRegenerateSection={regenerateSection}
              onGenerateChapter={openChapter}
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
            />
          </>
        )}
      </main>
      {AI_RUNTIME_SETTINGS_ENABLED && showAiSettings && (
        <AiSettingsDialog onClose={() => setShowAiSettings(false)} />
      )}
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

function compactSpineTitle(title: string) {
  const seriesName = title.split(/[：:]/)[0].trim();
  return seriesName.length > 18 ? `${seriesName.slice(0, 18)}…` : seriesName;
}

function Home({
  data,
  onOpen,
  onCreate,
}: {
  data: Bootstrap | null;
  onOpen: (shelf: Shelf) => void;
  onCreate: (body: ShelfCreateInput) => Promise<void>;
}) {
  const [showCreate, setShowCreate] = useState(false);

  return (
    <section className="landing-section">
      <p className="eyebrow">AI 时代的个人学习书架</p>
      <div className="title-row home-title-row">
        <div>
          <h1>我的书架</h1>
          <p className="lead">一本书是一个台阶。慢一点，真正理解、验证并留下自己的笔记。</p>
        </div>
        <button className="primary-button" onClick={() => setShowCreate(true)}>＋ 创建书架</button>
      </div>
      <div className="shelf-grid">
        {data?.shelves.map((item) => (
          <button
            className="shelf-card bookshelf-card"
            key={item.id}
            aria-label={`进入${item.name}书架，共 ${item.series.length} 个学习系列`}
            onClick={() => onOpen(item)}
          >
            <div className="shelf-card-top">
              <span>{item.name}书架</span>
              <small>{item.series.length} 册在架</small>
            </div>
            <div className="bookshelf-scene">
              <div className="book-row">
                <span className="bookend left" aria-hidden="true" />
                {item.series.map((series, index) => (
                  <span
                    className={`book-spine book-tone-${index % 6}`}
                    style={{ height: `${154 + ((index * 23) % 48)}px` }}
                    title={series.title}
                    key={series.id}
                  >
                    <i />
                    <b>{compactSpineTitle(series.title)}</b>
                    <small>{series.progress}%</small>
                  </span>
                ))}
                <span className="bookend right" aria-hidden="true" />
              </div>
              <span className="shelf-board" aria-hidden="true"><i /></span>
            </div>
            <div className="shelf-plaque">
              <span className="shelf-monogram">{item.name.slice(0, 1)}</span>
              <span>
                <b>{item.name}</b>
                <small>{item.domain} · {item.specialty}</small>
              </span>
              <em>进入书架 <i>→</i></em>
            </div>
          </button>
        ))}
      </div>
      {showCreate && (
        <ShelfCreateDialog
          onClose={() => setShowCreate(false)}
          onCreate={onCreate}
        />
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
            <em>
              {domain.trim() || '学习领域'}
              {specialty.trim() ? ` · ${specialty.trim()}` : ''}
            </em>
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
              placeholder="例如：交互设计"
            />
          </label>
          <label>
            学习领域
            <input
              required
              maxLength={100}
              disabled={submitting}
              value={domain}
              onChange={(event) => setDomain(event.target.value)}
              placeholder="例如：设计学"
            />
          </label>
          <label>
            细分方向（可选）
            <input
              maxLength={120}
              disabled={submitting}
              value={specialty}
              onChange={(event) => setSpecialty(event.target.value)}
              placeholder="例如：交互设计转型"
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
              placeholder="用逗号分隔，例如：UX、原型、研究"
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
  onCreate,
  onOpen,
  onDelete,
}: {
  shelf: Shelf;
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
      <p className="eyebrow">{shelf.domain} · {shelf.specialty}</p>
      <div className="title-row">
        <div>
          <h1>{shelf.name}</h1>
          <p className="lead">选择一个系列继续学习，或规划新的学习主题。</p>
        </div>
        <button className="primary-button" onClick={() => setShowPlan(!showPlan)}>＋ 创建学习系列</button>
      </div>
      {showPlan && <PlanForm submit={onCreate} />}
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

function PlanForm({ submit }: { submit: (body: object, idempotencyKey: string) => Promise<void> }) {
  const [topic, setTopic] = useState('');
  const [background, setBackground] = useState('');
  const [experience, setExperience] = useState('');
  const [purpose, setPurpose] = useState('');
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
  onSelectSection,
  onGenerateSection,
  onRegenerateSection,
  onGenerateChapter,
  onStartNextBook,
  chapterGenerationDisabled,
  generatingChapterId,
  onSectionChange,
  onRefreshSeries,
  onDeleteBook,
}: {
  series: Series;
  section: Section | null;
  onSelectSection: (id: string) => Promise<Section>;
  onGenerateSection: (id: string) => Promise<void>;
  onRegenerateSection: (id: string) => Promise<void>;
  onGenerateChapter: (chapter: Chapter) => Promise<void>;
  onStartNextBook: () => Promise<void>;
  chapterGenerationDisabled: boolean;
  generatingChapterId: string;
  onSectionChange: (section: Section) => void;
  onRefreshSeries: () => Promise<void>;
  onDeleteBook: (bookId: string) => Promise<void>;
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
    <div className={`learning-workspace ${directoryHidden ? 'directory-collapsed' : ''} ${qaHidden ? 'qa-collapsed' : ''}`}>
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
        chapterGenerationDisabled={chapterGenerationDisabled}
        generatingChapterId={generatingChapterId}
        onRefreshSeries={onRefreshSeries}
        onDeleteBook={onDeleteBook}
      />
      <ReaderPanel
        section={section}
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
        onSectionChange={onSectionChange}
        onRefreshSeries={onRefreshSeries}
      />
      <QaPanel
        key={section?.id || 'empty'}
        section={section}
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
        {series.books.map((book) => (
          <BookTree
            key={book.id}
            book={book}
            currentSectionId={currentSectionId}
            onSelectSection={onSelectSection}
            onGenerateChapter={onGenerateChapter}
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
  chapterGenerationDisabled,
  generatingChapterId,
  onRefreshSeries,
  onRequestDelete,
}: {
  book: Book;
  currentSectionId?: string;
  onSelectSection: (id: string) => Promise<Section>;
  onGenerateChapter: (chapter: Chapter) => Promise<void>;
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
        <span className="book-number">{book.position}</span>
        <span>
          <b>{book.title}</b>
          <small>
            {book.status === 'completed' ? '已完成' : book.status === 'locked' ? '未解锁' : '已解锁'}
            {' · '}{book.progress}% · {Math.round(book.estimatedMinutes / 60)} 小时
          </small>
        </span>
        <i>{book.status === 'locked' ? <LockIcon /> : <ChevronIcon />}</i>
      </summary>
      <div className="chapter-tree">
        {book.chapters.map((chapter) => {
          const chapterLocked = chapter.status === 'locked';
          return (
            <div className="chapter-node" key={chapter.id}>
              {chapter.generated || chapterLocked ? (
                <div className={`chapter-title ${chapterLocked ? 'locked' : ''}`}>
                  <span>{book.position}.{chapter.position}</span>
                  <b>{chapter.title}</b>
                  {chapterLocked && <LockIcon size={13} />}
                </div>
              ) : (
                <button
                  className="chapter-title chapter-entry"
                  aria-label={`生成并进入 ${chapter.title}`}
                  disabled={chapterGenerationDisabled || generatingChapterId === chapter.id}
                  onClick={() => onGenerateChapter(chapter)}
                >
                  <span>{book.position}.{chapter.position}</span>
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
                {chapter.sections.map((item) => (
                  <SectionTreeButton
                    key={item.id}
                    item={item}
                    active={item.id === currentSectionId}
                    onClick={() => onSelectSection(item.id)}
                  />
                ))}
              </div>
            ) : null}
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

function SectionTreeButton({ item, active, onClick }: { item: SectionSummary; active: boolean; onClick: () => void }) {
  const state = item.status === 'completed' ? '✓' : item.status === 'locked' ? <LockIcon size={11} /> : item.position;
  return (
    <button
      className={`section-tree-button ${active ? 'active' : ''} ${item.status}`}
      disabled={item.status === 'locked'}
      onClick={onClick}
    >
      <span>{state}</span>
      <b>{item.title}</b>
    </button>
  );
}

function ReaderPanel({
  section,
  directoryHidden,
  qaHidden,
  onToggleDirectory,
  onToggleQa,
  location,
  selectedBlockId,
  onQuote,
  onGenerate,
  onRegenerate,
  onSectionChange,
  onRefreshSeries,
}: {
  section: Section | null;
  directoryHidden: boolean;
  qaHidden: boolean;
  onToggleDirectory: () => void;
  onToggleQa: () => void;
  location: ReturnType<typeof findSectionLocation>;
  selectedBlockId: string;
  onQuote: (quote: TextQuote) => void;
  onGenerate: () => void;
  onRegenerate: () => Promise<void>;
  onSectionChange: (section: Section) => void;
  onRefreshSeries: () => Promise<void>;
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
  }, [section?.id, section?.content?.id]);

  useEffect(() => {
    if (!regenerating) return undefined;
    const timer = window.setInterval(
      () => setRegenerationClock(Date.now()),
      1000,
    );
    return () => window.clearInterval(timer);
  }, [regenerating]);

  const switchTab = (nextTab: ReaderTab) => {
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
            {location?.chapter.title}
            <i>›</i>
            第 {section.position} 节
          </p>
          <h1>{section.title}</h1>
        </div>
        <div className="reader-toolbar-actions">
          <span className={`lesson-status ${section.status}`}>
            {section.status === 'completed' ? '已完成' : section.status === 'available' ? '学习中' : '未解锁'}
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
            selectedBlockId={selectedBlockId}
            onGenerate={onGenerate}
            onStartQuiz={() => switchTab('quiz')}
          />
        )}
        {tab === 'quiz' && section.quiz && (
          <Quiz
            key={section.quiz.id}
            section={section}
            onSectionChange={onSectionChange}
            onRefreshSeries={onRefreshSeries}
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

function LessonContent({
  section,
  selectedBlockId,
  onGenerate,
  onStartQuiz,
}: {
  section: Section;
  selectedBlockId: string;
  onGenerate: () => void;
  onStartQuiz: () => void;
}) {
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
        <small className="generation-note">正文和题目会经过结构校验，引用由服务端核验后才会保存。</small>
      </div>
    );
  }

  return (
    <article className="lesson-document">
      <div className="lesson-question">
        <span>本节问题</span>
        <h2>{section.question}</h2>
      </div>
      {section.content.blocks.map((block, index) => (
        <ContentBlock
          key={block.id}
          block={block}
          index={index}
          selected={block.id === selectedBlockId}
        />
      ))}
      <details className="source-list">
        <summary>来源与核验记录 · {section.content.sources.length}</summary>
        {section.content.sources.map((source, index) => (
          <a href={source.url} target="_blank" rel="noreferrer" key={`${source.url}-${index}`}>
            <span>{index + 1}</span>
            <b>{source.title}</b>
            <small>
              {source.version} · {
                section.content?.sourceVerification[index]?.verificationStatus === 'server_unverifiable'
                  ? '站点拒绝自动核验'
                  : section.content?.sourceVerification[index]?.reachable
                    ? '服务端可达'
                    : '核验失败'
              }
            </small>
          </a>
        ))}
      </details>
      <div className="lesson-complete-action">
        <span>正文阅读完成</span>
        <h3>现在，验证你是否真正理解。</h3>
        <p>完成选择题并达到及格线，才会解锁下一节；满分后还会开放 Ask Me 隐藏关卡。</p>
        <button className="primary-button" onClick={onStartQuiz}>开始验证 <i>→</i></button>
      </div>
    </article>
  );
}

function ContentBlock({
  block,
  index,
  selected,
}: {
  block: Block;
  index: number;
  selected: boolean;
}) {
  const labels: Record<string, string> = {
    conclusion: '先说结论',
    mechanism: '理解机制',
    example: '看一个例子',
    boundary: '边界与反例',
    practice: '连接实践',
  };
  const markdown = block.kind === 'table'
    ? normalizeTableMarkdown(block.content)
    : block.content;
  return (
    <section
      className={`content-block role-${block.role} ${selected ? 'selected' : ''}`}
      data-block-id={block.id}
    >
      <div className="block-meta"><span>{String(index + 1).padStart(2, '0')}</span><b>{labels[block.role] || block.role}</b></div>
      <h2>{block.heading}</h2>
      {block.kind === 'code' ? (
        <pre className="code-block"><code>{block.content}</code></pre>
      ) : (
        <div className={`content-markdown kind-${block.kind}`}>
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{markdown}</ReactMarkdown>
        </div>
      )}
    </section>
  );
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
  onSubmissionComplete,
}: {
  section: Section;
  onSectionChange: (section: Section) => void;
  onRefreshSeries: () => Promise<void>;
  onSubmissionComplete: () => void;
}) {
  const quizDraftKey = `slow:quiz-draft:${section.id}:${section.quiz?.id || 'none'}`;
  const quizRequestStorageKey = `slow:quiz-request:${section.id}:${section.quiz?.id || 'none'}`;
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
  const [result, setResult] = useState<QuizResult | null>(null);
  const [submissionError, setSubmissionError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [workflowRunning, setWorkflowRunning] = useState(false);
  const [workflowMessage, setWorkflowMessage] = useState('');
  const [failedTasks, setFailedTasks] = useState<LearningTask[]>([]);
  const [workflowTasks, setWorkflowTasks] = useState<LearningTask[]>(
    section.workflowTasks || [],
  );
  const [retryingTasks, setRetryingTasks] = useState(false);

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
        onSectionChange(await api.section(section.id));
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

  const submit = async () => {
    if (!section.quiz) return;
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
    setSubmissionError('');
    setSubmitting(true);
    try {
      const value = await api.quiz(
        section.id,
        section.quiz.id,
        answers,
        requestId,
      );
      setResult(value);
      localStorage.removeItem(quizDraftKey);
      localStorage.removeItem(quizRequestStorageKey);
      const next = await api.section(section.id);
      onSectionChange(next);
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
      <p className="quiz-rule">核心题必须答对，且总正确率至少达到 80%。</p>
      <p className="quiz-draft-note">单选题只能选择一个答案，多选题可选择多个；切回正文查阅时，当前作答会自动保留。</p>
      {section.remediations.map((item) => (
        <section className="remediation-card" key={item.id}>
          <span>错题补充教学 · {item.strategy}</span>
          {item.blocks.map((block) => <div key={block.id}><h3>{block.heading}</h3><p>{block.content}</p></div>)}
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
              <span>{String.fromCharCode(65 + optionIndex)}</span>
              {option}
            </label>
          ))}
        </fieldset>
      ))}
      <button
        className="primary-button large"
        disabled={submitting || workflowRunning}
        aria-describedby="quiz-submission-feedback"
        onClick={submit}
      >
        {submitting ? '正在评分…' : workflowRunning ? '正在准备后续内容…' : '提交验证'}
      </button>
      <div id="quiz-submission-feedback" aria-live="polite">
        {submissionError && <p className="result failure" role="alert">{submissionError}</p>}
        {result && <p className={result.passed ? 'result success' : 'result failure'}>{result.passed ? '验证已通过，下一节已经解锁。' : '本次未通过，评分结果已经保存。'}</p>}
        {workflowMessage && <p className={failedTasks.length ? 'result failure' : 'result success'}>{workflowMessage}</p>}
        {workflowTasks.length > 0 && (
          <div className="workflow-task-list" aria-label="后台任务状态">
            {workflowTasks.map((task) => (
              <div className={`workflow-task ${task.status}`} key={task.taskId}>
                <span>{{
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

function Note({
  sectionId,
  note,
  onSaved,
}: {
  sectionId: string;
  note: NoteType;
  onSaved: (section: Section) => void;
}) {
  const [editing, setEditing] = useState(JSON.stringify(note.userContent || {}, null, 2));
  const [message, setMessage] = useState('');
  const save = async () => {
    try {
      await api.note(sectionId, JSON.parse(editing));
      onSaved(await api.section(sectionId));
      setMessage('已保存；AI 笔记未被覆盖。');
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : '保存失败');
    }
  };
  return (
    <div className="note-view">
      <p className="eyebrow">完成后的核心资产</p>
      <h2>AI 整理笔记</h2>
      <div className="note-paper"><pre>{JSON.stringify(note.aiContent, null, 2)}</pre></div>
      <h3>我的补充</h3>
      <textarea value={editing} onChange={(event) => setEditing(event.target.value)} />
      <button className="primary-button" onClick={save}>保存我的内容</button>
      {message && <p className="save-message">{message}</p>}
    </div>
  );
}

function AskMePanel({ sectionId }: { sectionId: string }) {
  const [askMe, setAskMe] = useState<AskMe | null>(null);
  const [answer, setAnswer] = useState('');
  const runAskMe = async () => {
    const next = await api.askMe(sectionId, answer);
    setAskMe(next);
    setAnswer('');
  };
  return (
    <div className="askme-view">
      <p className="eyebrow">满分隐藏关卡</p>
      <h2>机制 → 边界 → 迁移</h2>
      <p>这里是口试，不是继续教学。系统会依次探测你能否解释机制、识别边界，并迁移到新场景。</p>
      {!askMe ? (
        <button className="primary-button large" onClick={runAskMe}>开始三轮口试</button>
      ) : (
        <>
          <div className="oral-timeline">
            {askMe.entries.map((entry) => (
              <section key={entry.dimension}>
                <span>{entry.dimension}</span>
                <h3>{entry.prompt}</h3>
                {entry.answer && <p>你的回答：{entry.answer}</p>}
                {entry.evaluation !== 'not_evaluated' && <b>评估：{entry.evaluation}</b>}
              </section>
            ))}
          </div>
          {askMe.status !== 'completed' ? (
            <>
              <textarea value={answer} onChange={(event) => setAnswer(event.target.value)} placeholder="在这里作答…" />
              <button className="primary-button" onClick={runAskMe}>提交本轮</button>
            </>
          ) : (
            <p className="result success">三轮口试完成，证据已写入掌握画像。</p>
          )}
        </>
      )}
    </div>
  );
}

function QaPanel({
  section,
  hidden,
  onClose,
  selectedBlockId,
  selectedQuote,
  onAnchor,
  onClearQuote,
}: {
  section: Section | null;
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
        <p>答疑独立保存，不会打断正文阅读。</p>
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
                <button onClick={() => setQuestion('这个机制最容易被误解的地方是什么？')}>这个机制最容易被误解的地方是什么？</button>
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
