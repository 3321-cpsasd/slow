import { useEffect, useRef, useState } from 'react';

export type PresetExplanationStyle =
  | 'worked_example'
  | 'diagram'
  | 'analogy'
  | 'derivation'
  | 'precise'
  | 'concise';

export type ExplanationStyle = PresetExplanationStyle | 'custom';

export type ExplanationOption = {
  style: PresetExplanationStyle;
  label: string;
  prompt: string;
};

export function LessonBlockTools({
  blockId,
  blockHeading,
  options,
  onExplain,
  onFeedback,
}: {
  blockId: string;
  blockHeading: string;
  options: ExplanationOption[];
  onExplain: (style: ExplanationStyle, customInstruction?: string) => void;
  onFeedback: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [customExplanation, setCustomExplanation] = useState('');
  const shellRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return undefined;
    const closeOnOutsidePress = (event: MouseEvent) => {
      if (!shellRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', closeOnOutsidePress);
    document.addEventListener('keydown', closeOnEscape);
    return () => {
      document.removeEventListener('mousedown', closeOnOutsidePress);
      document.removeEventListener('keydown', closeOnEscape);
    };
  }, [open]);

  const chooseStyle = (style: ExplanationStyle, customInstruction?: string) => {
    onExplain(style, customInstruction);
    setOpen(false);
  };

  return (
    <div className="block-tools" ref={shellRef}>
      <button
        className="block-tools-trigger"
        type="button"
        aria-expanded={open}
        aria-haspopup="dialog"
        onClick={() => setOpen((visible) => !visible)}
      >
        这段需要帮助？
      </button>
      {open && (
        <div className="block-tools-menu" role="dialog" aria-label={`帮助理解“${blockHeading}”`}>
          <header>
            <div>
              <b>换一种方式理解</b>
              <small>先选最接近你需要的讲法</small>
            </div>
            <button type="button" aria-label="关闭段落工具" onClick={() => setOpen(false)}>关闭</button>
          </header>
          <div className="block-tools-recommended">
            {options.slice(0, 3).map((option) => (
              <button type="button" key={option.style} onClick={() => chooseStyle(option.style)}>
                {option.label}
              </button>
            ))}
          </div>
          <details className="block-tools-more">
            <summary>更多讲法</summary>
            <div className="block-tools-more-options">
              {options.slice(3).map((option) => (
                <button type="button" key={option.style} onClick={() => chooseStyle(option.style)}>
                  {option.label}
                </button>
              ))}
            </div>
            <form
              className="block-explanation-custom"
              onSubmit={(event) => {
                event.preventDefault();
                const instruction = customExplanation.trim();
                if (instruction.length < 2) return;
                chooseStyle('custom', instruction);
                setCustomExplanation('');
              }}
            >
              <label htmlFor={`custom-explanation-${blockId}`}>或者，写下你希望怎样讲</label>
              <div>
                <input
                  id={`custom-explanation-${blockId}`}
                  value={customExplanation}
                  maxLength={240}
                  placeholder="例如：像讲故事一样，少用术语"
                  onChange={(event) => setCustomExplanation(event.target.value)}
                />
                <button type="submit" disabled={customExplanation.trim().length < 2}>按这个讲</button>
              </div>
            </form>
          </details>
          <div className="block-tools-feedback">
            <span>发现内容问题？</span>
            <button
              type="button"
              onClick={() => {
                setOpen(false);
                onFeedback();
              }}
            >
              反馈这段
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
