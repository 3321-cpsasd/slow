import { useEffect, useRef, useState, type KeyboardEvent } from 'react';

export type ReaderTab = 'content' | 'quiz' | 'note';

type LessonReaderHeaderProps = {
  bookPosition?: number;
  chapterPosition?: number;
  chapterTitle?: string;
  sectionPosition: number;
  title: string;
  status: string;
  sessionSeconds: number;
  canRegenerate: boolean;
  regenerating: boolean;
  readerTypographyStep: number;
  readerTypographyLabel: string;
  readerTypographyBodySize: number;
  readerTypographyStepCount: number;
  onReaderTypographyStepChange: (step: number) => void;
  onRequestRegenerate: () => void;
  onFeedback: () => void;
  condensed?: boolean;
  directoryOpen: boolean;
  onToggleDirectory: () => void;
};

const statusLabel = (status: string) => {
  if (status === 'completed') return '已验证';
  if (status === 'failed') return '待重新准备';
  if (status === 'available') return '学习中';
  if (status === 'preparing') return '准备中';
  return '未解锁';
};

export function LessonReaderHeader({
  bookPosition,
  chapterPosition,
  chapterTitle,
  sectionPosition,
  title,
  status,
  sessionSeconds,
  canRegenerate,
  regenerating,
  readerTypographyStep,
  readerTypographyLabel,
  readerTypographyBodySize,
  readerTypographyStepCount,
  onReaderTypographyStepChange,
  onRequestRegenerate,
  onFeedback,
  condensed = false,
  directoryOpen,
  onToggleDirectory,
}: LessonReaderHeaderProps) {
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!menuOpen) return undefined;
    const closeOnOutsidePress = (event: MouseEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) setMenuOpen(false);
    };
    const closeOnEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') setMenuOpen(false);
    };
    document.addEventListener('mousedown', closeOnOutsidePress);
    document.addEventListener('keydown', closeOnEscape);
    return () => {
      document.removeEventListener('mousedown', closeOnOutsidePress);
      document.removeEventListener('keydown', closeOnEscape);
    };
  }, [menuOpen]);

  return (
    <>
      <div className={`reader-toolbar ${condensed ? 'is-condensed' : ''}`}>
        <div className="reader-title-group">
          <nav className="breadcrumb" aria-label="当前位置">
            <ol>
              <li>第 {bookPosition} 本</li>
              <li>第 {chapterPosition} 章 · {chapterTitle}</li>
              <li aria-current="page">{chapterPosition}.{sectionPosition}</li>
            </ol>
          </nav>
          <h1>{title}</h1>
        </div>
        <div className="reader-toolbar-actions">
          <button
            type="button"
            className="reader-mobile-directory"
            aria-controls="course-directory-panel"
            aria-expanded={directoryOpen}
            onClick={onToggleDirectory}
          >
            目录
          </button>
          <span className="lesson-session-time">
            {sessionSeconds < 60
              ? '本次 <1 分钟'
              : `本次 ${String(Math.floor(sessionSeconds / 60)).padStart(2, '0')} 分钟`}
          </span>
          <span className={`lesson-status ${status}`}>{statusLabel(status)}</span>
          <div className="reader-options" ref={menuRef}>
            <button
              type="button"
              className="quiet-button reader-options-trigger"
              aria-haspopup="dialog"
              aria-expanded={menuOpen}
              onClick={() => setMenuOpen((open) => !open)}
            >
              <span className="reader-options-label">小节选项</span>
              <span aria-hidden="true">···</span>
            </button>
            {menuOpen && (
              <div className="reader-options-menu" role="dialog" aria-label="小节选项">
                <section className="reader-typography-control" aria-labelledby="reader-typography-title">
                  <header>
                    <div>
                      <b id="reader-typography-title">阅读字号</b>
                      <small>正文与标题同步调整</small>
                    </div>
                    <output htmlFor="reader-typography-range">
                      {readerTypographyLabel} · {readerTypographyBodySize}px
                    </output>
                  </header>
                  <div className="reader-typography-stepper">
                    <button
                      type="button"
                      aria-label="减小阅读字号"
                      disabled={readerTypographyStep === 0}
                      onClick={() => onReaderTypographyStepChange(readerTypographyStep - 1)}
                    >
                      <span aria-hidden="true">A</span>
                    </button>
                    <div>
                      <input
                        id="reader-typography-range"
                        type="range"
                        min="0"
                        max={readerTypographyStepCount - 1}
                        step="1"
                        value={readerTypographyStep}
                        aria-label="阅读字号"
                        aria-valuetext={`${readerTypographyLabel}，正文 ${readerTypographyBodySize}px`}
                        onChange={(event) => onReaderTypographyStepChange(Number(event.currentTarget.value))}
                      />
                      <span className="reader-typography-ticks" aria-hidden="true">
                        {Array.from({ length: readerTypographyStepCount }, (_, index) => (
                          <i key={index} className={index <= readerTypographyStep ? 'active' : ''} />
                        ))}
                      </span>
                    </div>
                    <button
                      type="button"
                      aria-label="增大阅读字号"
                      disabled={readerTypographyStep === readerTypographyStepCount - 1}
                      onClick={() => onReaderTypographyStepChange(readerTypographyStep + 1)}
                    >
                      <span aria-hidden="true">A</span>
                    </button>
                  </div>
                  <footer>
                    <span>共 {readerTypographyStepCount} 档</span>
                    {readerTypographyStep !== 3 && (
                      <button type="button" onClick={() => onReaderTypographyStepChange(3)}>
                        恢复标准
                      </button>
                    )}
                  </footer>
                </section>
                <button
                  type="button"
                  className="reader-option-action"
                  onClick={() => {
                    setMenuOpen(false);
                    onFeedback();
                  }}
                >
                  <b>反馈产品问题</b>
                  <small>报告当前学习页的体验问题</small>
                </button>
                {canRegenerate && (
                  <button
                    type="button"
                    className="reader-option-action"
                    disabled={regenerating}
                    onClick={() => {
                      setMenuOpen(false);
                      onRequestRegenerate();
                    }}
                  >
                    <b>重新生成本节</b>
                    <small>仅在尚未完成验证时可用</small>
                  </button>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </>
  );
}

export function LessonReaderTabs({
  active,
  quizAvailable,
  noteAvailable,
  completed,
  onChange,
}: {
  active: ReaderTab;
  quizAvailable: boolean;
  noteAvailable: boolean;
  completed: boolean;
  onChange: (tab: ReaderTab) => void;
}) {
  const tablistRef = useRef<HTMLDivElement>(null);
  const tabs: { id: ReaderTab; label: string }[] = [
    { id: 'content', label: '正文' },
    ...(quizAvailable ? [{ id: 'quiz' as const, label: completed ? '验证结果' : '验证' }] : []),
    ...(noteAvailable ? [{ id: 'note' as const, label: '笔记' }] : []),
  ];

  const moveFocus = (event: KeyboardEvent<HTMLButtonElement>, tabId: ReaderTab) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const currentIndex = tabs.findIndex((item) => item.id === tabId);
    const nextIndex = event.key === 'Home'
      ? 0
      : event.key === 'End'
        ? tabs.length - 1
        : (currentIndex + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
    const nextTab = tabs[nextIndex];
    onChange(nextTab.id);
    requestAnimationFrame(() => {
      tablistRef.current?.querySelector<HTMLButtonElement>(`[data-reader-tab="${nextTab.id}"]`)?.focus();
    });
  };

  return (
    <div className="reader-tabs" role="tablist" aria-label="小节学习阶段" ref={tablistRef}>
      {tabs.map((item) => (
        <button
          key={item.id}
          type="button"
          role="tab"
          id={`reader-tab-${item.id}`}
          data-reader-tab={item.id}
          aria-selected={active === item.id}
          aria-controls="reader-tabpanel"
          tabIndex={active === item.id ? 0 : -1}
          className={active === item.id ? 'active' : ''}
          onClick={() => onChange(item.id)}
          onKeyDown={(event) => moveFocus(event, item.id)}
        >
          {item.label}
        </button>
      ))}
    </div>
  );
}
