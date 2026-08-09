import { FormEvent, useMemo, useState } from 'react';
import { api } from './api/client';
import type { LearningProfile, OnboardingState } from './model/types';

const STAGES: { value: Exclude<LearningProfile['stage'], ''>; label: string; note: string }[] = [
  { value: 'exploring', label: '正在探索', note: '还在确认方向和问题' },
  { value: 'beginner', label: '刚刚入门', note: '需要建立基础概念' },
  { value: 'foundation', label: '已有基础', note: '想补齐体系和机制' },
  { value: 'practice', label: '实践提升', note: '希望解决真实任务' },
  { value: 'advanced', label: '系统进阶', note: '需要边界、迁移和深度' },
];

function parseDomains(value: string) {
  return Array.from(new Set(
    value
      .split(/[,，、\n]/)
      .map((item) => item.trim())
      .filter(Boolean),
  )).slice(0, 6);
}

export function ProfileOnboardingFlow({
  initial,
  userName,
  onComplete,
  onLogout,
}: {
  initial: OnboardingState;
  userName: string;
  onComplete: () => Promise<void>;
  onLogout: () => Promise<void>;
}) {
  const [state, setState] = useState(initial);
  const [profession, setProfession] = useState(initial.profile.profession);
  const [stage, setStage] = useState<LearningProfile['stage']>(initial.profile.stage);
  const [purpose, setPurpose] = useState(initial.profile.purpose);
  const [experience, setExperience] = useState(initial.profile.experience);
  const [domainText, setDomainText] = useState(initial.profile.domains.join('，'));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const domains = useMemo(() => parseDomains(domainText), [domainText]);
  const requestedIndex = Math.max(0, state.steps.findIndex((item) => item.id === state.currentStep));
  const safeIndex = !profession.trim() || !stage
    ? 0
    : !purpose.trim() || domains.length === 0
      ? Math.min(requestedIndex, 1)
      : requestedIndex;
  const [stepIndex, setStepIndex] = useState(safeIndex);
  const step = state.steps[stepIndex] || state.steps[0];

  const persist = async (nextIndex: number) => {
    const nextStep = state.steps[nextIndex]?.id || 'review';
    const next = await api.saveProfileDraft({
      currentStep: nextStep,
      profession: profession.trim(),
      stage: stage || undefined,
      purpose: purpose.trim(),
      domains,
      experience: experience.trim(),
    });
    setState(next);
    setStepIndex(nextIndex);
  };

  const next = async (event: FormEvent) => {
    event.preventDefault();
    setError('');
    if (step.id === 'identity' && (!profession.trim() || !stage)) {
      setError('请填写当前身份，并选择最接近你的学习阶段。');
      return;
    }
    if (step.id === 'direction' && (!purpose.trim() || domains.length === 0)) {
      setError('请填写学习目的，并至少填写一个目标领域。');
      return;
    }
    setBusy(true);
    try {
      if (step.id !== 'review') {
        await persist(stepIndex + 1);
        return;
      }
      await api.completeProfile({
        profession: profession.trim(),
        stage,
        purpose: purpose.trim(),
        domains,
        experience: experience.trim(),
        preferences: initial.profile.preferences,
      });
      await onComplete();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '画像保存失败，请重试');
    } finally {
      setBusy(false);
    }
  };

  const back = async () => {
    if (stepIndex === 0 || busy) return;
    setBusy(true);
    setError('');
    try {
      await persist(stepIndex - 1);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '暂时无法返回上一步');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="profile-flow-shell">
      <header className="profile-flow-header">
        <span className="brand"><span className="brand-mark"><i /></span><b>slow</b></span>
        <span>建立你的学习起点</span>
        <button className="quiet-button" disabled={busy} onClick={() => void onLogout()}>退出</button>
      </header>

      <main className="profile-flow-main">
        <aside className="profile-flow-rail" aria-label="基础画像填写进度">
          <p className="eyebrow">BASELINE PROFILE · V{state.flowVersion}</p>
          <h1>先让教材<br />认识现在的你。</h1>
          <p>这些自述信息决定第一本书从哪里讲起。之后，答题和实践证据会继续修正你的掌握画像。</p>
          <ol>
            {state.steps.map((item, index) => (
              <li className={index === stepIndex ? 'active' : index < stepIndex ? 'done' : ''} key={item.id}>
                <span>{index < stepIndex ? '✓' : String(index + 1).padStart(2, '0')}</span>
                <div><b>{item.title}</b><small>{item.description}</small></div>
              </li>
            ))}
          </ol>
          <small className="profile-flow-account">当前账号 · {userName}</small>
        </aside>

        <section className="profile-flow-stage">
          <div className="profile-sheet-corner" aria-hidden="true" />
          <form onSubmit={(event) => void next(event)}>
            <div className="profile-step-heading">
              <span>{String(stepIndex + 1).padStart(2, '0')} / {String(state.steps.length).padStart(2, '0')}</span>
              <p>{step.description}</p>
            </div>

            {step.id === 'identity' && (
              <div className="profile-step-fields">
                <h2>你现在以什么身份学习？</h2>
                <p>不用写正式职位，写最能影响学习内容的身份即可。</p>
                <label>
                  当前身份或职业
                  <input
                    autoFocus
                    required
                    maxLength={120}
                    value={profession}
                    onChange={(event) => { setProfession(event.target.value); setError(''); }}
                    placeholder="例如：产品设计师、计算机专业大一学生、准备转行"
                  />
                </label>
                <fieldset className="profile-stage-options">
                  <legend>当前学习阶段</legend>
                  {STAGES.map((item) => (
                    <button
                      type="button"
                      className={stage === item.value ? 'selected' : ''}
                      aria-pressed={stage === item.value}
                      onClick={() => { setStage(item.value); setError(''); }}
                      key={item.value}
                    >
                      <b>{item.label}</b><small>{item.note}</small>
                    </button>
                  ))}
                </fieldset>
              </div>
            )}

            {step.id === 'direction' && (
              <div className="profile-step-fields">
                <h2>这一次，你想走向哪里？</h2>
                <p>目标越具体，教材越能选择适合你的案例、边界和练习。</p>
                <label>
                  目标领域
                  <input
                    autoFocus
                    required
                    maxLength={240}
                    value={domainText}
                    onChange={(event) => { setDomainText(event.target.value); setError(''); }}
                    placeholder="例如：信息可视化，交互设计"
                  />
                  <small>可填写 1–6 个，用逗号分隔。</small>
                </label>
                <label>
                  学习目的
                  <textarea
                    required
                    maxLength={1000}
                    value={purpose}
                    onChange={(event) => { setPurpose(event.target.value); setError(''); }}
                    placeholder="例如：为作品集完成一个数据叙事项目，并能解释自己的设计判断"
                  />
                </label>
                <label>
                  相关经验 <em>可选</em>
                  <textarea
                    maxLength={1000}
                    value={experience}
                    onChange={(event) => setExperience(event.target.value)}
                    placeholder="写下已经学过、做过或最容易卡住的内容"
                  />
                </label>
              </div>
            )}

            {step.id === 'review' && (
              <div className="profile-step-fields profile-review">
                <h2>这是教材将采用的起点。</h2>
                <p>它不是能力证明，只是你对当前状态的自述；真实掌握度将由后续学习证据更新。</p>
                <dl>
                  <div><dt>身份</dt><dd>{profession}</dd></div>
                  <div><dt>阶段</dt><dd>{STAGES.find((item) => item.value === stage)?.label}</dd></div>
                  <div><dt>领域</dt><dd>{domains.join(' · ')}</dd></div>
                  <div><dt>目的</dt><dd>{purpose}</dd></div>
                  <div><dt>经验</dt><dd>{experience || '暂未填写，将从学习证据中逐步补充'}</dd></div>
                </dl>
                <div className="profile-evidence-note">
                  <span>自述</span>
                  <p><b>不会直接计为“已掌握”</b>答题、错题、深入讨论和实践结果才会形成掌握证据。</p>
                </div>
              </div>
            )}

            {error && <p className="profile-flow-error" role="alert">{error}</p>}
            <div className="profile-flow-actions">
              <button type="button" className="quiet-button" disabled={stepIndex === 0 || busy} onClick={() => void back()}>上一步</button>
              <button className="profile-flow-next" disabled={busy}>
                {busy ? '正在保存…' : step.id === 'review' ? '确认并进入书架' : '保存并继续'} <span>→</span>
              </button>
            </div>
          </form>
        </section>
      </main>
    </div>
  );
}
