import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { Block } from '../../model/types';
import { LessonBlockTools, type ExplanationOption, type ExplanationStyle } from './LessonBlockTools';

const kindLabels: Record<string, string> = {
  text: '阅读', bullet_list: '要点', ordered_steps: '步骤', diagram: '图解',
  table: '对照', code: '演练', formula: '推导',
};

const roleLabels: Record<string, string> = {
  core_instruction: '核心依据', conclusion: '核心依据', prerequisite_scaffold: '必要前置',
  context: '背景', mechanism: '机制', derivation: '推导', worked_example: '逐步示例',
  empirical_case: '真实案例', primary_source: '原始材料', evidence_analysis: '证据分析',
  comparison: '对照', alternative_interpretation: '另一种解释', counterargument: '反方观点',
  counterexample: '反例', boundary: '适用边界', application: '应用', transfer: '迁移',
  practice: '练习', synthesis: '综合', summary: '回顾',
};

export function LessonContentBlock({
  block, selected, reviewTarget, explanationOptions, onFeedback,
  onRestorePersonalPresentation, onExplain,
}: {
  block: Block;
  selected: boolean;
  reviewTarget: boolean;
  explanationOptions: ExplanationOption[];
  onFeedback: () => void;
  onRestorePersonalPresentation: () => Promise<void>;
  onExplain: (style: ExplanationStyle, customInstruction?: string) => void;
}) {
  return (
    <section
      className={`content-block role-${block.role} ${selected ? 'selected' : ''} ${reviewTarget ? 'review-target' : ''}`}
      data-block-id={block.id}
      tabIndex={-1}
    >
      {reviewTarget && <span className="review-target-label">错题依据</span>}
      <div className="block-meta">
        <b>{kindLabels[block.kind] || '阅读'}</b>
        {roleLabels[block.role] && <span>{roleLabels[block.role]}</span>}
      </div>
      <LessonBlockTools
        blockId={block.id}
        blockHeading={block.heading}
        options={explanationOptions}
        onExplain={onExplain}
        onFeedback={onFeedback}
      />
      <h2>{block.heading}</h2>
      <LessonBlockBody block={block} />
      {block.personalPresentation && (
        <aside className="personal-presentation" aria-label="我的另一种讲法">
          <header>
            <span>我的另一种讲法</span>
            <button type="button" onClick={() => void onRestorePersonalPresentation()}>移除</button>
          </header>
          <LessonBlockBody block={block} content={block.personalPresentation.content} />
        </aside>
      )}
    </section>
  );
}

export function LessonBlockBody({ block, content = block.content }: { block: Block; content?: string }) {
  // `kind` is a presentation hint; generated block content is still GFM.
  // Keep legacy plain-code blocks readable, but let mixed prose + fenced code
  // retain the authored boundary instead of wrapping the prose in <code>.
  if (block.kind === 'code' && !hasBalancedCodeFences(content)) {
    return <pre className="code-block"><code>{content}</code></pre>;
  }
  const markdown = block.kind === 'table'
    ? normalizeTableMarkdown(content)
    : block.kind === 'text'
      ? normalizeLessonTextMarkdown(content)
      : content.replace(/\r\n?/g, '\n').trim();
  return (
    <div className={`content-markdown kind-${block.kind}`}>
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{markdown}</ReactMarkdown>
    </div>
  );
}

function hasBalancedCodeFences(content: string): boolean {
  const lines = content.replace(/\r\n?/g, '\n').split('\n');
  let opening: { marker: '`' | '~'; length: number } | null = null;
  let completedFence = false;
  for (const line of lines) {
    const match = line.match(/^ {0,3}(`{3,}|~{3,})(.*)$/);
    if (!match) continue;
    const marker = match[1][0] as '`' | '~';
    if (!opening) {
      opening = { marker, length: match[1].length };
      continue;
    }
    if (
      marker === opening.marker
      && match[1].length >= opening.length
      && match[2].trim() === ''
    ) {
      opening = null;
      completedFence = true;
    }
  }
  return completedFence && opening === null;
}

function normalizeLessonTextMarkdown(content: string): string {
  const normalized = content.replace(/\r\n?/g, '\n').trim();
  const hasAuthoredStructure = /\n\s*\n/.test(normalized)
    || /(^|\n)\s*(?:#{1,6}\s|[-+*]\s+|\d+[.)]\s+|>\s+|```)/m.test(normalized);
  if (normalized.length < 200 || hasAuthoredStructure) return normalized;
  const sentences = normalized.match(/[^。！？]+[。！？]+|[^。！？]+$/g)
    ?.map((sentence) => sentence.trim()).filter(Boolean) || [];
  if (sentences.length < 4) return normalized;
  const paragraphCount = Math.min(3, Math.floor(sentences.length / 2), Math.max(2, Math.ceil(normalized.length / 150)));
  if (paragraphCount < 2) return normalized;
  const paragraphs: string[] = [];
  let cursor = 0;
  for (let index = 0; index < paragraphCount; index += 1) {
    const take = Math.ceil((sentences.length - cursor) / (paragraphCount - index));
    paragraphs.push(sentences.slice(cursor, cursor + take).join(''));
    cursor += take;
  }
  return paragraphs.join('\n\n');
}

function normalizeTableMarkdown(content: string): string {
  const lines = content.trim().split(/\r?\n/).filter((line) => line.trim());
  if (lines.length < 2) return content;
  const cells = (line: string) => line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim());
  const columnCount = cells(lines[0]).length;
  if (columnCount < 2) return content;
  const possibleDivider = cells(lines[1]);
  const hasDivider = possibleDivider.length === columnCount
    && possibleDivider.every((cell) => /^:?-{3,}:?$/.test(cell));
  if (!hasDivider) lines.splice(1, 0, Array.from({ length: columnCount }, () => '---').join(' | '));
  return lines.join('\n');
}
