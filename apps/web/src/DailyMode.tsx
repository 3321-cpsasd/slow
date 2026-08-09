import { useEffect, useRef, useState } from 'react';
import type { DailyMode, DailyModeDuration, DailyModeSource, DailyModeState } from './model/types';

const DURATIONS: { value: DailyModeDuration; label: string }[] = [
  { value: '1h', label: '1 小时' },
  { value: '3h', label: '3 小时' },
  { value: '6h', label: '6 小时' },
  { value: 'today', label: '今天' },
];

const modeName = (mode: DailyMode) => mode === 'fast' ? 'Fast' : 'Slow';

const formatExpiry = (expiresAt: string | null) => {
  if (!expiresAt) return '等待选择时间';
  return new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(expiresAt));
};

export function DailyModeHeader({
  state,
  effectiveMode,
  expiredInActivity,
  busy,
  onActivate,
}: {
  state: DailyModeState;
  effectiveMode: DailyMode | null;
  expiredInActivity: boolean;
  busy: boolean;
  onActivate: (
    mode: DailyMode,
    duration: DailyModeDuration,
    source: DailyModeSource,
  ) => Promise<void>;
}) {
  const [popoverOpen, setPopoverOpen] = useState(false);
  const shellRef = useRef<HTMLDivElement>(null);
  const mode = effectiveMode || state.lastDailyMode || 'slow';
  const duration = state.duration || '1h';

  useEffect(() => {
    if (!popoverOpen) return;
    const close = (event: MouseEvent) => {
      if (!shellRef.current?.contains(event.target as Node)) setPopoverOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setPopoverOpen(false);
    };
    document.addEventListener('mousedown', close);
    document.addEventListener('keydown', escape);
    return () => {
      document.removeEventListener('mousedown', close);
      document.removeEventListener('keydown', escape);
    };
  }, [popoverOpen]);

  return (
    <div className={`daily-mode-header mode-${mode}`} ref={shellRef}>
      <button
        type="button"
        className="daily-mode-expiry"
        aria-haspopup="dialog"
        aria-expanded={popoverOpen}
        onClick={() => setPopoverOpen((open) => !open)}
      >
        {expiredInActivity
          ? '本节结束后重选'
          : state.active
            ? `至 ${formatExpiry(state.expiresAt)}`
            : '调整时间'}
      </button>
      <button
        type="button"
        className="daily-mode-pill"
        disabled={busy}
        aria-label={`当前 ${modeName(mode)}，点击切换到 ${modeName(mode === 'fast' ? 'slow' : 'fast')}`}
        onClick={() => void onActivate(
          mode === 'fast' ? 'slow' : 'fast',
          duration,
          'header_toggle',
        )}
      >
        <span aria-hidden="true">{mode === 'fast' ? '兔' : '龟'}</span>
        <b>{modeName(mode)}</b>
        <i aria-hidden="true">↔</i>
      </button>
      {popoverOpen && (
        <section className="daily-mode-popover" role="dialog" aria-label="调整模式持续时间">
          <b>这次学多久？</b>
          <p>自然到期不会打断正在进行的小节。</p>
          <div role="radiogroup" aria-label="持续时间">
            {DURATIONS.map((item) => (
              <button
                key={item.value}
                type="button"
                role="radio"
                aria-checked={duration === item.value}
                className={duration === item.value ? 'selected' : ''}
                disabled={busy}
                onClick={async () => {
                  await onActivate(mode, item.value, 'duration_adjustment');
                  setPopoverOpen(false);
                }}
              >
                {item.label}
              </button>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

export function DailyModeDialog({
  state,
  busy,
  onActivate,
}: {
  state: DailyModeState;
  busy: boolean;
  onActivate: (mode: DailyMode, duration: DailyModeDuration, source: 'dialog') => Promise<void>;
}) {
  const [selectedMode, setSelectedMode] = useState<DailyMode | null>(state.lastDailyMode);
  const [duration, setDuration] = useState<DailyModeDuration>(state.duration || '1h');
  const titleRef = useRef<HTMLHeadingElement>(null);
  const dialogRef = useRef<HTMLElement>(null);

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    titleRef.current?.focus();
    const trapFocus = (event: KeyboardEvent) => {
      if (event.key !== 'Tab') return;
      const focusable = Array.from(
        dialogRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled)') || [],
      );
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && (document.activeElement === first || document.activeElement === titleRef.current)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', trapFocus);
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener('keydown', trapFocus);
    };
  }, []);

  return (
    <div className="daily-mode-backdrop">
      <section
        ref={dialogRef}
        className="daily-mode-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="daily-mode-title"
      >
        <header>
          <p>DAILY MODE · 今天怎么学</p>
          <h2 id="daily-mode-title" ref={titleRef} tabIndex={-1}>把这一段时间，交给龟还是兔？</h2>
          <span>教材与验证标准不变，只改变你此刻看到的节奏。</span>
        </header>

        <div className="daily-mode-versus" role="radiogroup" aria-label="学习模式">
          <button
            type="button"
            role="radio"
            aria-checked={selectedMode === 'fast'}
            className={`daily-mode-card fast ${selectedMode === 'fast' ? 'selected' : ''}`}
            onClick={() => setSelectedMode('fast')}
          >
            <span className="daily-mode-animal" aria-hidden="true">兔</span>
            <small>3–5 MIN · 碎片时间</small>
            <h3>Fast</h3>
            <p>从同一篇已发布正文中抽取关键段落，先抓住结论、机制与一个可迁移判断。</p>
            <ul>
              <li>通勤、户外、临时空档</li>
              <li>短回答与低摩擦互动</li>
              <li>快速阅读本身不产生掌握证据</li>
            </ul>
          </button>

          <div className="daily-mode-versus-mark" aria-hidden="true">VS</div>

          <button
            type="button"
            role="radio"
            aria-checked={selectedMode === 'slow'}
            className={`daily-mode-card slow ${selectedMode === 'slow' ? 'selected' : ''}`}
            onClick={() => setSelectedMode('slow')}
          >
            <span className="daily-mode-animal" aria-hidden="true">龟</span>
            <small>10–20 MIN · 完整小节</small>
            <h3>Slow</h3>
            <p>完整阅读结论、因果机制、例子、边界与练习连接，建立可验证的理解。</p>
            <ul>
              <li>安静、可持续投入的时段</li>
              <li>完整正文与展开式答疑</li>
              <li>进入同一套正式测验</li>
            </ul>
          </button>
        </div>

        <div className="daily-mode-duration">
          <div>
            <b>保持多久</b>
            <span>到期只影响下一项活动</span>
          </div>
          <div role="radiogroup" aria-label="模式持续时间">
            {DURATIONS.map((item) => (
              <button
                key={item.value}
                type="button"
                role="radio"
                aria-checked={duration === item.value}
                className={duration === item.value ? 'selected' : ''}
                onClick={() => setDuration(item.value)}
              >
                {item.label}
              </button>
            ))}
          </div>
        </div>

        <footer>
          <p><b>同一份教材，同一套验证。</b> Fast / Slow 不会改变 Learning Contract、答案或及格线。</p>
          <button
            type="button"
            disabled={!selectedMode || busy}
            onClick={() => selectedMode && void onActivate(selectedMode, duration, 'dialog')}
          >
            {busy ? '正在同步…' : selectedMode ? `以 ${modeName(selectedMode)} 开始` : '先选择一种模式'}
            {!busy && selectedMode && <span aria-hidden="true">→</span>}
          </button>
        </footer>
      </section>
    </div>
  );
}
