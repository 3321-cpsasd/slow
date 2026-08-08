import { useCallback, useEffect, useMemo, useState, type CSSProperties } from "react";

type UserMetric = {
  accountRef: string; username: string; accountStatus: string; createdAt: string;
  lastLoginAt: string | null; privacyConsentCurrent: boolean; privacyAcceptedAt: string | null;
  profileCompleted: boolean; firstSectionStarted: boolean; firstChapterCompleted: boolean;
  firstBookCompleted: boolean; retainedConcepts7d: number; retainedClaims: number;
  failedTasks: number; feedbackCount: number; aiInvocations: number; failedAiInvocations: number;
  productEvents7d: number; lastProductEventAt: string | null;
  inputTokens: number; outputTokens: number; totalTokens: number; exitStatus: string;
  exitRequestedAt: string | null; deletionDueAt: string | null;
};

type DashboardData = {
  generatedAt: string; cacheSeconds: number;
  summary: { accounts: number; active: number; consented: number; active7d: number; firstSection: number; firstChapter: number; firstBook: number; retained7d: number; failedTasks: number; failedAi: number; feedback: number; exits: number };
  users: UserMetric[];
};
type Filter = "all" | "attention" | "started" | "retained" | "exit";

function formatDate(value: string | null) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(value));
}
function formatNumber(value: number) { return new Intl.NumberFormat("zh-CN").format(value); }
function ProgressMark({ value, label }: { value: boolean; label: string }) {
  return <span className={value ? "progress-mark is-done" : "progress-mark"} aria-label={`${label}：${value ? "是" : "否"}`}>{value ? "●" : "○"}</span>;
}

export function App() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<Filter>("all");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<UserMetric | null>(null);
  const [inputPrice, setInputPrice] = useState(0);
  const [outputPrice, setOutputPrice] = useState(0);

  const load = useCallback(async (force = false) => {
    setLoading(true); setError("");
    try {
      const response = await fetch(`/api/dashboard${force ? "?refresh=1" : ""}`, { cache: "no-store" });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || body.error || "运营数据读取失败");
      setData(body);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "运营数据读取失败");
    } finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const users = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return (data?.users ?? []).filter((user) => {
      const searchMatch = !needle || user.username.toLowerCase().includes(needle) || user.accountRef.toLowerCase().includes(needle);
      const filterMatch = filter === "all"
        || (filter === "attention" && (user.failedTasks > 0 || user.failedAiInvocations > 0 || user.feedbackCount > 0))
        || (filter === "started" && user.firstSectionStarted)
        || (filter === "retained" && user.retainedConcepts7d > 0)
        || (filter === "exit" && user.exitStatus === "requested");
      return searchMatch && filterMatch;
    });
  }, [data, filter, query]);
  const cost = useCallback((user: UserMetric) => user.inputTokens / 1_000_000 * inputPrice + user.outputTokens / 1_000_000 * outputPrice, [inputPrice, outputPrice]);

  if (!data && loading) return <main className="state-page"><span className="pulse" /><h1>正在连接东京数据隧道</h1><p>只读查询，最多两个数据库连接。</p></main>;
  if (!data && error) return <main className="state-page"><p className="state-kicker">连接失败</p><h1>运营数据没有离开安全边界</h1><p>{error}</p><button onClick={() => void load(true)}>重新检查隧道</button></main>;

  const summary = data!.summary;
  const stages = [["账号", summary.accounts], ["同意", summary.consented], ["首节", summary.firstSection], ["首章", summary.firstChapter], ["整书", summary.firstBook], ["7天保持", summary.retained7d]] as const;

  return <div className="ops-shell">
    <header className="topbar">
      <div className="brand"><span>Slow</span><i>运营瞭望台</i></div>
      <div className="connection"><span className="connection-dot" />东京 PostgreSQL · 只读隧道</div>
      <button className="refresh" disabled={loading} onClick={() => void load(true)}>{loading ? "同步中" : "刷新数据"}</button>
    </header>
    <main>
      <section className="briefing">
        <div><p className="eyebrow">今日运营水位</p><h1>先处理阻塞，再观察学习是否真正发生。</h1><p className="timestamp">读取于 {formatDate(data!.generatedAt)} · 缓存 {data!.cacheSeconds} 秒 · 页面不保存用户数据</p></div>
        <div className="attention-ledger" aria-label="需要关注的运营指标">
          <div><span>7天有活动</span><strong>{summary.active7d}</strong></div><div><span>失败任务</span><strong>{summary.failedTasks}</strong></div><div><span>AI 失败</span><strong>{summary.failedAi}</strong></div><div><span>反馈</span><strong>{summary.feedback}</strong></div>
        </div>
      </section>
      <section className="waterline" aria-label="学习漏斗">
        {stages.map(([label, value], index) => { const percentage = summary.accounts ? value / summary.accounts * 100 : 0; return <div className="water-stage" key={label} style={{ "--water": `${percentage}%` } as CSSProperties}><span>{label}</span><strong>{value}</strong><small>{index === 0 ? "基数" : `${percentage.toFixed(0)}%`}</small></div>; })}
      </section>
      <section className="workspace">
        <div className="tools">
          <div className="filters" role="group" aria-label="筛选用户">
            {([["all", "全部"], ["attention", "需关注"], ["started", "已启动"], ["retained", "7天保持"], ["exit", "退出待办"]] as [Filter, string][]).map(([value, label]) => <button key={value} className={filter === value ? "is-active" : ""} onClick={() => setFilter(value)}>{label}</button>)}
          </div>
          <label className="search"><span>查找</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="用户名或账号引用" /></label>
          <details className="pricing"><summary>模型单价</summary><label>输入 / 百万 Token（USD）<input type="number" min="0" step="0.01" value={inputPrice} onChange={(event) => setInputPrice(Number(event.target.value))} /></label><label>输出 / 百万 Token（USD）<input type="number" min="0" step="0.01" value={outputPrice} onChange={(event) => setOutputPrice(Number(event.target.value))} /></label><small>仅在当前页面内计算，不写入服务器。</small></details>
        </div>
        <div className="table-wrap"><table><thead><tr><th>用户</th><th>账号</th><th>学习进度</th><th>7天保持</th><th>失败</th><th>反馈</th><th>Token</th><th>估算成本</th><th>近7天活动</th></tr></thead><tbody>
          {users.map((user) => <tr key={user.accountRef} onClick={() => setSelected(user)} tabIndex={0} onKeyDown={(event) => { if (event.key === "Enter") setSelected(user); }}>
            <td><strong>{user.username || "未绑定用户名"}</strong><small>{user.accountRef}</small></td><td><span className={`status status-${user.accountStatus}`}>{user.accountStatus === "active" ? "有效" : user.accountStatus}</span>{!user.privacyConsentCurrent && <small className="warning">待同意</small>}</td>
            <td><div className="progress-dots"><ProgressMark value={user.profileCompleted} label="画像" /><ProgressMark value={user.firstSectionStarted} label="首节" /><ProgressMark value={user.firstChapterCompleted} label="首章" /><ProgressMark value={user.firstBookCompleted} label="整书" /></div></td><td>{user.retainedConcepts7d || "—"}</td><td className={user.failedTasks + user.failedAiInvocations > 0 ? "danger" : ""}>{user.failedTasks + user.failedAiInvocations || "—"}</td><td>{user.feedbackCount || "—"}</td><td>{formatNumber(user.totalTokens)}</td><td>${cost(user).toFixed(4)}</td><td>{user.productEvents7d ? <>{user.productEvents7d} 次<small>{formatDate(user.lastProductEventAt)}</small></> : "—"}</td>
          </tr>)}
          {!users.length && <tr><td colSpan={9} className="empty">当前筛选下没有用户。</td></tr>}
        </tbody></table></div><p className="result-count">显示 {users.length} / {summary.accounts} 个账号</p>
      </section>
    </main>
    {selected && <div className="drawer-backdrop" onMouseDown={() => setSelected(null)}><aside className="drawer" aria-label="用户运营详情" onMouseDown={(event) => event.stopPropagation()}><button className="drawer-close" onClick={() => setSelected(null)} aria-label="关闭">×</button><p className="eyebrow">用户运营详情</p><h2>{selected.username || "未绑定用户名"}</h2><p className="account-ref">{selected.accountRef}</p><dl>
      <div><dt>账号状态</dt><dd>{selected.accountStatus}</dd></div><div><dt>隐私同意</dt><dd>{selected.privacyConsentCurrent ? `已接受 · ${formatDate(selected.privacyAcceptedAt)}` : "待接受当前版本"}</dd></div><div><dt>学习进度</dt><dd>{[selected.profileCompleted && "画像", selected.firstSectionStarted && "首节", selected.firstChapterCompleted && "首章", selected.firstBookCompleted && "整书"].filter(Boolean).join(" → ") || "尚未开始"}</dd></div><div><dt>7 天保持</dt><dd>{selected.retainedConcepts7d} 个目标</dd></div><div><dt>近 7 天产品事件</dt><dd>{selected.productEvents7d} 次 · 最近 {formatDate(selected.lastProductEventAt)}</dd></div><div><dt>失败任务 / AI</dt><dd>{selected.failedTasks} / {selected.failedAiInvocations}</dd></div><div><dt>AI 调用</dt><dd>{selected.aiInvocations} 次 · {formatNumber(selected.totalTokens)} Token</dd></div><div><dt>模型成本</dt><dd>${cost(selected).toFixed(4)}</dd></div><div><dt>退出申请</dt><dd>{selected.exitStatus || "无"}{selected.deletionDueAt ? ` · 截止 ${formatDate(selected.deletionDueAt)}` : ""}</dd></div>
    </dl><p className="drawer-note">此服务只读。反馈正文、学习内容、笔记、密码和会话均不在运营视图中。</p></aside></div>}
  </div>;
}
