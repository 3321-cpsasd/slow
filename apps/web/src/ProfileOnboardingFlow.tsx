import { FormEvent, useMemo, useState } from 'react';
import { api } from './api/client';
import type { LearningProfile, OnboardingState } from './model/types';

export type OnboardingContinuation = {
  firstShelfId: string | null;
  topic: string;
  experience: string;
};

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
  onComplete: (continuation: OnboardingContinuation) => Promise<void>;
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
      const completion = await api.completeProfile({
        profession: profession.trim(),
        stage,
        purpose: purpose.trim(),
        domains,
        experience: experience.trim(),
        preferences: initial.profile.preferences,
        firstShelfName: domains[0],
      });
      await onComplete({
        firstShelfId: completion.firstShelfId,
        topic: domains[0],
        experience: experience.trim(),
      });
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
        <span>建立起点，开始第一本书</span>
        <button className="quiet-button" disabled={busy} onClick={() => void onLogout()}>退出</button>
      </header>

      <main className="profile-flow-main">
        <aside className="profile-flow-rail" aria-label="基础画像填写进度">
          <p className="eyebrow">建立学习起点</p>
          <h1>先让教材<br />认识现在的你。</h1>
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
                <label>
                  学习方向
                  <input
                    autoFocus
                    required
                    maxLength={240}
                    value={domainText}
                    onChange={(event) => { setDomainText(event.target.value); setError(''); }}
                    placeholder="例如：信息可视化，交互设计"
                  />
                  <small>第一个方向会成为你的首个书架；可继续填写其他长期方向，用逗号分隔。</small>
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
                <dl>
                  <div><dt>身份</dt><dd>{profession}</dd></div>
                  <div><dt>阶段</dt><dd>{STAGES.find((item) => item.value === stage)?.label}</dd></div>
                  <div><dt>领域</dt><dd>{domains.join(' · ')}</dd></div>
                  <div><dt>目的</dt><dd>{purpose}</dd></div>
                  <div><dt>经验</dt><dd>{experience || '暂未填写，之后可以随时补充'}</dd></div>
                </dl>
                <section className="profile-first-shelf-preview" aria-label="即将创建的第一个书架">
                  <div className="profile-first-shelf-mark" aria-hidden="true">
                    <i /><i /><i />
                  </div>
                  <div>
                    <span>接下来会自动创建</span>
                    <h3>{domains[0]}</h3>
                    <p>确认后直接继续设置第一个学习目标，不经过空白书架页。</p>
                  </div>
                  <b>首个书架</b>
                </section>
              </div>
            )}

            {error && <p className="profile-flow-error" role="alert">{error}</p>}
            <div className="profile-flow-actions">
              <button type="button" className="quiet-button" disabled={stepIndex === 0 || busy} onClick={() => void back()}>上一步</button>
              <button className="profile-flow-next" disabled={busy}>
                {busy
                  ? step.id === 'review' ? '正在创建第一个书架…' : '正在保存…'
                  : step.id === 'review' ? '创建书架并继续' : '保存并继续'} <span>→</span>
              </button>
            </div>
          </form>
        </section>
      </main>
    </div>
  );
}
