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
  const title = goalChanged
    ? '这套教材仍按之前的目标编排'
    : '采用这套路径后开始记录里程碑';

  return (
    <section
      className={`path-decision ${goalChanged ? 'goal-changed' : 'path-proposed'}`}
      role="status"
      aria-labelledby="path-decision-title"
    >
      <div className="path-decision-copy">
        <span className="path-decision-label">
          {goalChanged ? '学习目标有变化' : '学习路径待采用'}
        </span>
        <h2 id="path-decision-title">{title}</h2>
        <p>
          {goalChanged
            ? '继续学习不会自动改写教材；如果新目标差异较大，先检查目标。'
            : '确认后，后续进度会按这套路径记录；教材内容不会因此改变。'}
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
          暂时收起
        </button>
        <button type="button" className="secondary-button" disabled={busy} onClick={onReviewGoal}>
          查看学习目标
        </button>
        <button type="button" className="primary-button" disabled={busy} onClick={() => void onConfirm()}>
          {busy ? '正在确认…' : goalChanged ? '教材仍适用' : '采用并开始'}
        </button>
      </div>
    </section>
  );
}
