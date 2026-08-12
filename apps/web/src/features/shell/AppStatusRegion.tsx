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
  onDismissError,
  onDismissNotice,
}: {
  error: string;
  notice: string;
  onDismissError: () => void;
  onDismissNotice: () => void;
}) {
  if (!error && !notice) return null;
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
    </div>
  );
}
