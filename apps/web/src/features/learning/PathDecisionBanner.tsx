import type { MilestonePath } from '../../model/types';

export function PathDecisionBanner({
  path,
  goalStatement,
  busy,
  onConfirm,
  onReviewGoal,
  onDismiss,
}: {
  path: MilestonePath;
  goalStatement: string;
  busy: boolean;
  onConfirm: () => Promise<void>;
  onReviewGoal: () => void;
  onDismiss: () => void;
}) {
  const goalChanged = !path.goalAligned;
  const title = goalChanged ? '确认这套教材仍符合你的目标' : '确认这套学习路径';

  return (
    <section
      className="path-decision"
      role="status"
      aria-labelledby="path-decision-title"
    >
      <div className="path-decision-copy">
        <span className="path-decision-label">开始学习前确认</span>
        <h2 id="path-decision-title">{title}</h2>
        <p>
          {goalChanged
            ? '你的学习目标已经更新。先核对目标和当前系列，再决定是否继续。'
            : '确认后，Slow 会按这套路径记录后续里程碑。'}
        </p>
      </div>
      <dl className="path-decision-context">
        <div>
          <dt>当前目标</dt>
          <dd>{goalStatement || '尚未填写明确目标'}</dd>
        </div>
        <div>
          <dt>当前系列</dt>
          <dd>{path.seriesTitle}</dd>
        </div>
      </dl>
      <div className="path-decision-actions">
        <button type="button" className="quiet-button" disabled={busy} onClick={onDismiss}>
          稍后处理
        </button>
        <button type="button" className="secondary-button" disabled={busy} onClick={onReviewGoal}>
          查看学习目标
        </button>
        <button type="button" className="primary-button" disabled={busy} onClick={() => void onConfirm()}>
          {busy ? '正在确认…' : goalChanged ? '继续使用这套教材' : '确认学习路径'}
        </button>
      </div>
    </section>
  );
}
