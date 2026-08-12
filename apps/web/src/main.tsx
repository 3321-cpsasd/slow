import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

const docsOnly = import.meta.env.VITE_DOCS_ONLY === 'true';
const publicBase = import.meta.env.BASE_URL.replace(/\/$/, '');
const currentPath = publicBase && window.location.pathname.startsWith(publicBase)
  ? window.location.pathname.slice(publicBase.length) || '/'
  : window.location.pathname;
const isDocsRoute = docsOnly || currentPath === '/docs' || currentPath.startsWith('/docs/');

void (async () => {
  if (!isDocsRoute) await import('./styles.css');
  const { default: RootApp } = isDocsRoute ? await import('./docs/DocsApp') : await import('./App');

  createRoot(document.getElementById('root')!).render(
    <StrictMode><RootApp /></StrictMode>
  );
})();
