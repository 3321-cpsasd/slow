import { useEffect, useRef, useState } from 'react';
import type { DailyMode, DailyModeDuration, DailyModeSource, DailyModeState } from './model/types';

const DURATIONS: { value: DailyModeDuration; label: string }[] = [
  { value: '1h', label: '1 小时' },
  { value: '3h', label: '3 小时' },
  { value: '6h', label: '6 小时' },
  { value: 'today', label: '今天' },
];

const modeName = (mode: DailyMode) => mode === 'fast' ? 'Fast' : 'Slow';

const modePartner = (mode: DailyMode) => mode === 'fast' ? '兔子' : '乌龟';

const formatExpiry = (expiresAt: string | null) => {
  if (!expiresAt) return '等待选择时间';
  return new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(expiresAt));
};

const sameCalendarDay = (left: Date, right: Date) => (
  left.getFullYear() === right.getFullYear()
  && left.getMonth() === right.getMonth()
  && left.getDate() === right.getDate()
);

const previewExpiry = (duration: DailyModeDuration) => {
  const now = new Date();
  const expiresAt = new Date(now);
  if (duration === 'today') expiresAt.setHours(23, 59, 59, 999);
  else expiresAt.setHours(expiresAt.getHours() + Number.parseInt(duration, 10));
  const tomorrow = new Date(now);
  tomorrow.setDate(tomorrow.getDate() + 1);
  const dayLabel = sameCalendarDay(expiresAt, now)
    ? '今天'
    : sameCalendarDay(expiresAt, tomorrow)
      ? '明天'
      : new Intl.DateTimeFormat('zh-CN', {
          month: 'numeric',
          day: 'numeric',
        }).format(expiresAt);
  const time = new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(expiresAt);
  return `保持至${dayLabel} ${time}`;
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
        aria-label={state.active
          ? `调整学习模式持续时间，当前保持至 ${formatExpiry(state.expiresAt)}`
          : '调整学习模式持续时间'}
        onClick={() => setPopoverOpen((open) => !open)}
      >
        {expiredInActivity
          ? '本节结束后重选'
          : state.active
            ? `保持至 ${formatExpiry(state.expiresAt)}`
            : '调整时间'}
      </button>
      <button
        type="button"
        className="daily-mode-pill"
        disabled={busy}
        aria-label={`当前 ${modeName(mode)}，点击切换到 ${modeName(mode === 'fast' ? 'slow' : 'fast')}`}
        title={mode === 'fast'
          ? '快速阅读：只显示关键段落和自检'
          : '完整阅读：显示本节全部正文'}
        onClick={() => void onActivate(
          mode === 'fast' ? 'slow' : 'fast',
          duration,
          'header_toggle',
        )}
      >
        <b>{modeName(mode)}</b>
        <i aria-hidden="true">↔</i>
      </button>
      {popoverOpen && (
        <section className="daily-mode-popover" role="dialog" aria-label="调整模式持续时间">
          <b>保持当前模式</b>
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
  const [selectedMode, setSelectedMode] = useState<DailyMode | null>(null);
  const [duration, setDuration] = useState<DailyModeDuration>(state.duration || '1h');
  const dialogRef = useRef<HTMLElement>(null);

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    dialogRef.current?.focus();
    const trapFocus = (event: KeyboardEvent) => {
      if (event.key !== 'Tab') return;
      const focusable = Array.from(
        dialogRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled)') || [],
      );
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
        tabIndex={-1}
      >
        <header className="daily-mode-dialog-header">
          <p>CHOOSE YOUR PACE</p>
          <h2 id="daily-mode-title">这次，跟谁一起出发？</h2>
        </header>

        <div className="daily-mode-versus" role="radiogroup" aria-label="学习模式">
          <span className="daily-mode-versus-line" aria-hidden="true" />
          <span className="daily-mode-versus-mark" aria-hidden="true">VS</span>

          <button
            type="button"
            role="radio"
            aria-checked={selectedMode === 'fast'}
            data-mode="fast"
            className={`daily-mode-fighter fast ${selectedMode === 'fast' ? 'selected' : ''}`}
            onClick={() => setSelectedMode('fast')}
            onKeyDown={(event) => {
              if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) return;
              event.preventDefault();
              const nextMode: DailyMode = 'slow';
              setSelectedMode(nextMode);
              dialogRef.current?.querySelector<HTMLButtonElement>(`[data-mode="${nextMode}"]`)?.focus();
            }}
          >
            <span className="daily-mode-fighter-top">
              <span className="daily-mode-code">FAST</span>
              <span className="daily-mode-radio-mark" aria-hidden="true" />
            </span>
            <span className="daily-mode-mascot-stage" aria-hidden="true">
              <img className="daily-mode-mascot rabbit" src="/study-mode-rabbit.png" alt="" />
            </span>
            <span className="daily-mode-fighter-copy">
              <span className="daily-mode-details">
                <span className="daily-mode-use-case"><small>适合</small><span>通勤 · 户外 · 碎片时间</span></span>
                <span className="daily-mode-study-time"><small>单节约</small><b>3–5 分钟</b></span>
              </span>
            </span>
          </button>

          <button
            type="button"
            role="radio"
            aria-checked={selectedMode === 'slow'}
            data-mode="slow"
            className={`daily-mode-fighter deep ${selectedMode === 'slow' ? 'selected' : ''}`}
            onClick={() => setSelectedMode('slow')}
            onKeyDown={(event) => {
              if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) return;
              event.preventDefault();
              const nextMode: DailyMode = 'fast';
              setSelectedMode(nextMode);
              dialogRef.current?.querySelector<HTMLButtonElement>(`[data-mode="${nextMode}"]`)?.focus();
            }}
          >
            <span className="daily-mode-fighter-top">
              <span className="daily-mode-code">SLOW</span>
              <span className="daily-mode-radio-mark" aria-hidden="true" />
            </span>
            <span className="daily-mode-mascot-stage" aria-hidden="true">
              <img className="daily-mode-mascot turtle" src="/study-mode-turtle.png" alt="" />
            </span>
            <span className="daily-mode-fighter-copy">
              <span className="daily-mode-details">
                <span className="daily-mode-use-case"><small>适合</small><span>安静环境 · 时间完整 · 方便专注</span></span>
                <span className="daily-mode-study-time"><small>单节约</small><b>10–20 分钟</b></span>
              </span>
            </span>
          </button>
        </div>

        <footer className="daily-mode-footer">
          <div className="daily-mode-duration">
            <label>保持这次选择</label>
            <div role="radiogroup" aria-label="模式持续时间">
              {DURATIONS.map((item) => (
                <button
                  key={item.value}
                  type="button"
                  role="radio"
                  aria-checked={duration === item.value}
                  data-duration={item.value}
                  className={duration === item.value ? 'selected' : ''}
                  onClick={() => setDuration(item.value)}
                  onKeyDown={(event) => {
                    if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) return;
                    event.preventDefault();
                    const currentIndex = DURATIONS.findIndex((candidate) => candidate.value === item.value);
                    const direction = ['ArrowRight', 'ArrowDown'].includes(event.key) ? 1 : -1;
                    const next = DURATIONS[(currentIndex + direction + DURATIONS.length) % DURATIONS.length];
                    setDuration(next.value);
                    dialogRef.current?.querySelector<HTMLButtonElement>(`[data-duration="${next.value}"]`)?.focus();
                  }}
                >
                  {item.label}
                </button>
              ))}
            </div>
            <span className="daily-mode-expiry-preview">{previewExpiry(duration)}</span>
          </div>
          <div className="daily-mode-footer-action">
            <button
              type="button"
              disabled={!selectedMode || busy}
              onClick={() => selectedMode && void onActivate(selectedMode, duration, 'dialog')}
            >
              {busy
                ? '正在同步…'
                : selectedMode
                  ? `和${modePartner(selectedMode)}以 ${modeName(selectedMode)} 模式开始`
                  : '先选一位学习搭档'}
              <span aria-hidden="true">→</span>
            </button>
          </div>
        </footer>
      </section>
    </div>
  );
}
