import { useEffect, useId, useRef, useState } from 'react';

type MermaidState =
  | { status: 'rendering'; svg: '' }
  | { status: 'rendered'; svg: string }
  | { status: 'error'; svg: '' };

let mermaidReady: Promise<typeof import('mermaid').default> | null = null;

function loadMermaid() {
  if (!mermaidReady) {
    mermaidReady = import('mermaid').then(({ default: mermaid }) => {
      mermaid.initialize({
        startOnLoad: false,
        securityLevel: 'strict',
        theme: 'neutral',
        fontFamily: 'inherit',
      });
      return mermaid;
    });
  }
  return mermaidReady;
}

export function MermaidDiagram({ source }: { source: string }) {
  const reactId = useId();
  const hostRef = useRef<HTMLDivElement>(null);
  const [state, setState] = useState<MermaidState>({ status: 'rendering', svg: '' });

  useEffect(() => {
    let cancelled = false;
    setState({ status: 'rendering', svg: '' });

    void loadMermaid()
      .then(async (mermaid) => {
        const diagramId = `mermaid-${reactId.replace(/[^a-zA-Z0-9_-]/g, '')}`;
        const { svg, bindFunctions } = await mermaid.render(diagramId, source);
        if (cancelled) return;
        setState({ status: 'rendered', svg });
        requestAnimationFrame(() => {
          if (!cancelled && hostRef.current) bindFunctions?.(hostRef.current);
        });
      })
      .catch(() => {
        if (!cancelled) setState({ status: 'error', svg: '' });
      });

    return () => {
      cancelled = true;
    };
  }, [reactId, source]);

  if (state.status === 'error') {
    return (
      <div className="mermaid-fallback" role="alert">
        <b>这张关系图暂时无法解析</b>
        <span>下面保留了原始 Mermaid 内容。</span>
        <pre><code>{source}</code></pre>
      </div>
    );
  }

  if (state.status === 'rendering') {
    return (
      <div
        className="mermaid-diagram rendering"
        role="img"
        aria-label="AI 生成的关系图"
        aria-busy="true"
      >
        <span>正在绘制关系图…</span>
      </div>
    );
  }

  return (
    <div
      ref={hostRef}
      className="mermaid-diagram rendered"
      role="img"
      aria-label="AI 生成的关系图"
      aria-busy="false"
      dangerouslySetInnerHTML={{ __html: state.svg }}
    />
  );
}
