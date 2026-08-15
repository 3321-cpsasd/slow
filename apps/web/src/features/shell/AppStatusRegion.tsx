export function AppBusyStatus({ message }: { message: string }) {
  if (!message) return null;
  return (
    <span className="busy-indicator" role="status" aria-live="polite" aria-atomic="true">
      <i aria-hidden="true" />{message}
    </span>
  );
}

export function AppStatusRegion({
  error,
  notice,
  progress,
  onDismissError,
  onDismissNotice,
  onDismissProgress,
}: {
  error: string;
  notice: string;
  progress: { title: string; message: string } | null;
  onDismissError: () => void;
  onDismissNotice: () => void;
  onDismissProgress: () => void;
}) {
  if (!error && !notice && !progress) return null;
  return (
    <div className="app-status-region" aria-label="系统消息">
      {error && (
        <div className="global-error" role="alert">
          <span>
            <b>这次操作没有完成</b>
            <small>{error}</small>
          </span>
          <button type="button" aria-label="关闭错误提示" onClick={onDismissError}>关闭</button>
        </div>
      )}
      {notice && (
        <div className="global-notice" role="status" aria-live="polite" aria-atomic="true">
          <span>{notice}</span>
          <button type="button" aria-label="关闭状态提示" onClick={onDismissNotice}>关闭</button>
        </div>
      )}
      {progress && (
        <div className="global-progress" role="status" aria-live="polite" aria-atomic="true">
          <i aria-hidden="true" />
          <span>
            <b>{progress.title}</b>
            <small>{progress.message}</small>
          </span>
          <button type="button" aria-label="收起生成提示" onClick={onDismissProgress}>收起</button>
        </div>
      )}
    </div>
  );
}
