import { FormEvent, useEffect, useMemo, useRef, useState } from 'react';
import { api } from './api/client';
import type {
  AskMe,
  Block,
  Book,
  Bootstrap,
  Chapter,
  Note as NoteType,
  Section,
  SectionSummary,
  Series,
  Shelf,
} from './model/types';

type View = 'home' | 'shelf' | 'learn';
type ReaderTab = 'content' | 'quiz' | 'note';
type TextQuote = { text: string; blockId: string };
type SelectionPopup = TextQuote & { top: number; left: number };

export default function App() {
  const [data, setData] = useState<Bootstrap | null>(null);
  const [view, setView] = useState<View>('home');
  const [shelf, setShelf] = useState<Shelf | null>(null);
  const [series, setSeries] = useState<Series | null>(null);
  const [section, setSection] = useState<Section | null>(null);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');

  useEffect(() => {
    api.bootstrap().then(setData).catch((reason) => setError(reason.message));
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

  const openShelf = (value: Shelf) => {
    setShelf(value);
    setView('shelf');
  };

  const loadSection = async (sectionId: string) => {
    const value = await run('正在读取小节…', () => api.section(sectionId));
    setSection(value);
    return value;
  };

  const firstUsableSection = (value: Series) => {
    for (const book of value.books) {
      for (const chapter of book.chapters) {
        const match = chapter.sections.find((item) => item.status !== 'locked');
        if (match) return match.id;
      }
    }
    return null;
  };

  const openSeries = async (seriesId: string) => {
    const value = await run('正在进入学习空间…', () => api.series(seriesId));
    setSeries(value);
    setView('learn');
    const initial = firstUsableSection(value);
    if (initial) await loadSection(initial);
    else setSection(null);
  };

  const refreshSeries = async () => {
    if (!series) return;
    const value = await api.series(series.id);
    setSeries(value);
  };

  const openChapter = async (chapter: Chapter) => {
    const updated = await run('正在规划本章小节…', () => api.chapter(chapter.id));
    await refreshSeries();
    const first = updated.sections.find((item) => item.status !== 'locked');
    if (first) await loadSection(first.id);
  };

  const generateSection = async (sectionId: string) => {
    const value = await run('正在核查来源并生成本节…', () => api.generateSection(sectionId));
    setSection(value);
    await refreshSeries();
  };

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
          {view === 'learn' && (
            <button className="quiet-button" onClick={() => setView('home')}>返回书架</button>
          )}
        </div>
      </header>

      <main className={view === 'learn' ? 'learn-main' : 'marketing-main'}>
        {error && <div className="global-error">{error}</div>}
        {view === 'home' && <Home data={data} onOpen={openShelf} />}
        {view === 'shelf' && shelf && (
          <ShelfPage
            shelf={shelf}
            onCreate={async (body) => {
              const value = await run('AI 正在规划系列…', () => api.createPlan({ ...body, shelfId: shelf.id }));
              setSeries(value);
              setSection(null);
              setView('learn');
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
          <LearningWorkspace
            series={series}
            section={section}
            onSelectSection={loadSection}
            onGenerateSection={generateSection}
            onGenerateChapter={openChapter}
            onSectionChange={setSection}
            onRefreshSeries={refreshSeries}
          />
        )}
      </main>
    </div>
  );
}

function compactSpineTitle(title: string) {
  const seriesName = title.split(/[：:]/)[0].trim();
  return seriesName.length > 18 ? `${seriesName.slice(0, 18)}…` : seriesName;
}

function Home({ data, onOpen }: { data: Bootstrap | null; onOpen: (shelf: Shelf) => void }) {
  return (
    <section className="landing-section">
      <p className="eyebrow">AI 时代的个人学习书架</p>
      <h1>我的书架</h1>
      <p className="lead">一本书是一个台阶。慢一点，真正理解、验证并留下自己的笔记。</p>
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
    </section>
  );
}

function ShelfPage({
  shelf,
  onCreate,
  onOpen,
  onDelete,
}: {
  shelf: Shelf;
  onCreate: (body: object) => Promise<void>;
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
          <p className="lead">选择一个系列继续学习，或创建新的学习台阶。</p>
        </div>
        <button className="primary-button" onClick={() => setShowPlan(!showPlan)}>＋ 新的台阶</button>
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
              <small>点击“新的台阶”创建第一册。</small>
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

function PlanForm({ submit }: { submit: (body: object) => Promise<void> }) {
  const [topic, setTopic] = useState('Kubernetes');
  const [role, setRole] = useState('技术人员');
  const [experience, setExperience] = useState('熟悉 Linux、Docker 和基础网络，但没有实际使用过 K8s');
  const [purpose, setPurpose] = useState('即将参与基于 K8s 的应用部署与日常排障项目');
  const [depth, setDepth] = useState('deep');
  const send = async (event: FormEvent) => {
    event.preventDefault();
    await submit({ topic, role, experience, purpose, depth, details: '希望理解核心机制，而不只是记命令' });
  };
  return (
    <form className="plan-form" onSubmit={send}>
      <label>学习内容<input value={topic} onChange={(event) => setTopic(event.target.value)} /></label>
      <fieldset>
        <legend>你的角色</legend>
        {['技术人员', '产品或运营', '管理人员', '猎头或人力'].map((item) => (
          <button type="button" className={role === item ? 'selected' : ''} onClick={() => setRole(item)} key={item}>{item}</button>
        ))}
      </fieldset>
      <label>相关经验<textarea value={experience} onChange={(event) => setExperience(event.target.value)} /></label>
      <label>学习目的<textarea value={purpose} onChange={(event) => setPurpose(event.target.value)} /></label>
      <fieldset>
        <legend>目标深度</legend>
        {[['overview', '简单了解'], ['deep', '深度学习'], ['mastery', '掌握路径']].map(([value, label]) => (
          <button type="button" className={depth === value ? 'selected' : ''} onClick={() => setDepth(value)} key={value}>{label}</button>
        ))}
      </fieldset>
      <button className="primary-button">生成目录方案</button>
    </form>
  );
}

function LearningWorkspace({
  series,
  section,
  onSelectSection,
  onGenerateSection,
  onGenerateChapter,
  onSectionChange,
  onRefreshSeries,
}: {
  series: Series;
  section: Section | null;
  onSelectSection: (id: string) => Promise<Section>;
  onGenerateSection: (id: string) => Promise<void>;
  onGenerateChapter: (chapter: Chapter) => Promise<void>;
  onSectionChange: (section: Section) => void;
  onRefreshSeries: () => Promise<void>;
}) {
  const [selectedBlockId, setSelectedBlockId] = useState('');
  const [selectedQuote, setSelectedQuote] = useState<TextQuote | null>(null);

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

  return (
    <div className="learning-workspace">
      <DirectoryPanel
        series={series}
        currentSectionId={section?.id}
        onSelectSection={onSelectSection}
        onGenerateChapter={onGenerateChapter}
        onRefreshSeries={onRefreshSeries}
      />
      <ReaderPanel
        section={section}
        location={location}
        selectedBlockId={activeBlockId}
        onQuote={(quote) => {
          setSelectedBlockId(quote.blockId);
          setSelectedQuote(quote);
        }}
        onGenerate={() => section && onGenerateSection(section.id)}
        onSectionChange={onSectionChange}
      />
      <QaPanel
        key={section?.id || 'empty'}
        section={section}
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
  currentSectionId,
  onSelectSection,
  onGenerateChapter,
  onRefreshSeries,
}: {
  series: Series;
  currentSectionId?: string;
  onSelectSection: (id: string) => Promise<Section>;
  onGenerateChapter: (chapter: Chapter) => Promise<void>;
  onRefreshSeries: () => Promise<void>;
}) {
  return (
    <aside className="directory-panel" aria-label="课程目录">
      <div className="directory-heading">
        <span className="panel-label">目录</span>
        <h2>{series.title}</h2>
        <div className="series-progress">
          <span><i style={{ width: `${series.progress}%` }} /></span>
          <b>{series.progress}%</b>
        </div>
      </div>
      <nav className="book-tree">
        {series.books.map((book) => (
          <BookTree
            key={book.id}
            book={book}
            currentSectionId={currentSectionId}
            onSelectSection={onSelectSection}
            onGenerateChapter={onGenerateChapter}
            onRefreshSeries={onRefreshSeries}
          />
        ))}
      </nav>
    </aside>
  );
}

function BookTree({
  book,
  currentSectionId,
  onSelectSection,
  onGenerateChapter,
  onRefreshSeries,
}: {
  book: Book;
  currentSectionId?: string;
  onSelectSection: (id: string) => Promise<Section>;
  onGenerateChapter: (chapter: Chapter) => Promise<void>;
  onRefreshSeries: () => Promise<void>;
}) {
  const containsCurrent = book.chapters.some((chapter) => chapter.sections.some((item) => item.id === currentSectionId));
  return (
    <details className="book-node" open={containsCurrent || book.status !== 'locked'}>
      <summary>
        <span className="book-number">{book.position}</span>
        <span><b>{book.title}</b><small>{book.progress}% · {Math.round(book.estimatedMinutes / 60)} 小时</small></span>
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
                  onClick={() => onGenerateChapter(chapter)}
                >
                  <span>{book.position}.{chapter.position}</span>
                  <b>{chapter.title}</b>
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
  location,
  selectedBlockId,
  onQuote,
  onGenerate,
  onSectionChange,
}: {
  section: Section | null;
  location: ReturnType<typeof findSectionLocation>;
  selectedBlockId: string;
  onQuote: (quote: TextQuote) => void;
  onGenerate: () => void;
  onSectionChange: (section: Section) => void;
}) {
  const [tab, setTab] = useState<ReaderTab>('content');
  const [selectionPopup, setSelectionPopup] = useState<SelectionPopup | null>(null);
  const readerScrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setTab('content');
    setSelectionPopup(null);
  }, [section?.id]);

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

  if (!section) {
    return (
      <main className="reader-panel empty-reader">
        <span className="empty-symbol">S</span>
        <p className="eyebrow">选择左侧目录开始</p>
        <h1>今天，学清楚一个问题。</h1>
        <p>完成一节、通过验证、留下笔记。下一节会在掌握后自动解锁。</p>
      </main>
    );
  }

  return (
    <main className="reader-panel">
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
        <span className={`lesson-status ${section.status}`}>
          {section.status === 'completed' ? '已完成' : section.status === 'available' ? '学习中' : '未解锁'}
        </span>
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
          <Quiz key={section.quiz.id} section={section} onSectionChange={onSectionChange} />
        )}
        {tab === 'note' && section.note && (
          <Note sectionId={section.id} note={section.note} onSaved={onSectionChange} />
        )}
      </div>
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
            <small>{source.version} · {section.content?.sourceVerification[index]?.reachable ? '服务端可达' : '核验失败'}</small>
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
  return (
    <section
      className={`content-block role-${block.role} ${selected ? 'selected' : ''}`}
      data-block-id={block.id}
    >
      <div className="block-meta"><span>{String(index + 1).padStart(2, '0')}</span><b>{labels[block.role] || block.role}</b></div>
      <h2>{block.heading}</h2>
      <pre className={block.kind === 'code' ? 'code-block' : ''}>{block.content}</pre>
    </section>
  );
}

function Quiz({
  section,
  onSectionChange,
}: {
  section: Section;
  onSectionChange: (section: Section) => void;
}) {
  const quizDraftKey = `slow:quiz-draft:${section.id}:${section.quiz?.id || 'none'}`;
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
  const [result, setResult] = useState<{ passed: boolean } | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    localStorage.setItem(quizDraftKey, JSON.stringify(answers));
  }, [answers, quizDraftKey]);

  const submit = async () => {
    if (!section.quiz) return;
    setSubmitting(true);
    try {
      const value = await api.quiz(section.id, section.quiz.id, answers);
      setResult(value);
      localStorage.removeItem(quizDraftKey);
      const next = await api.section(section.id);
      onSectionChange(next);
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
      <button className="primary-button large" disabled={submitting} onClick={submit}>
        {submitting ? '正在评分…' : '提交验证'}
      </button>
      {result && <p className={result.passed ? 'result success' : 'result failure'}>{result.passed ? '通过，笔记已经生成。' : '未通过：补充教学已保存，并换成一组新题。'}</p>}
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
  selectedBlockId,
  selectedQuote,
  onAnchor,
  onClearQuote,
}: {
  section: Section | null;
  selectedBlockId: string;
  selectedQuote: TextQuote | null;
  onAnchor: (id: string) => void;
  onClearQuote: () => void;
}) {
  const [threadId, setThreadId] = useState<string>();
  const [newQuestion, setNewQuestion] = useState(false);
  const [question, setQuestion] = useState('');
  const [messages, setMessages] = useState<{ question: string; answer: string; relation: string }[]>([]);
  const [asking, setAsking] = useState(false);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const selectedBlock =
    section?.content?.blocks.find((block) => block.id === selectedBlockId) ??
    section?.content?.blocks[0];
  const effectiveBlockId = selectedBlock?.id ?? selectedBlockId;

  useEffect(() => {
    if (selectedQuote) composerRef.current?.focus();
  }, [selectedQuote]);

  const ask = async () => {
    if (!section || !effectiveBlockId || !question.trim()) return;
    const visibleQuestion = question.trim();
    const submittedQuestion = selectedQuote
      ? `请基于以下选中的正文回答。\n\n选中内容：${selectedQuote.text}\n\n问题：${visibleQuestion}`
      : visibleQuestion;
    setAsking(true);
    try {
      const result = await api.ask(
        section.id,
        effectiveBlockId,
        submittedQuestion,
        newQuestion ? undefined : threadId,
        newQuestion ? 'new_question' : undefined,
      );
      setThreadId(result.threadId);
      setMessages((current) => [...current, { question: visibleQuestion, answer: result.answer, relation: result.relation }]);
      setQuestion('');
      setNewQuestion(false);
    } finally {
      setAsking(false);
    }
  };

  return (
    <aside className="qa-panel" aria-label="本节答疑">
      <div className="qa-heading">
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
          <div className="qa-messages">
            {messages.length === 0 && (
              <div className="qa-suggestion">
                <span>可以这样问</span>
                <button onClick={() => setQuestion('这个机制最容易被误解的地方是什么？')}>这个机制最容易被误解的地方是什么？</button>
                <button onClick={() => setQuestion('它在什么边界条件下会失效？')}>它在什么边界条件下会失效？</button>
              </div>
            )}
            {messages.map((message, index) => (
              <div className="qa-exchange" key={index}>
                <div className="user-message"><span>你</span><p>{message.question}</p></div>
                <div className="assistant-message"><span>S</span><p>{message.answer}</p></div>
              </div>
            ))}
          </div>
          <div className="qa-composer">
            <textarea
              ref={composerRef}
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              placeholder={selectedQuote ? '针对选中的内容输入问题…' : '基于当前段落继续追问…'}
            />
            <div>
              <label><input type="checkbox" checked={newQuestion} onChange={(event) => setNewQuestion(event.target.checked)} /> 新问题</label>
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
