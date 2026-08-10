# Study mode dialog design QA

- Source visual truth: `/Users/pix/worker/slow/prototypes/study-mode-dialog.html`
- Source assets: `/Users/pix/worker/slow/prototypes/assets/study-mode-rabbit.png`, `/Users/pix/worker/slow/prototypes/assets/study-mode-turtle.png`
- Rendered implementation: `http://127.0.0.1:5173/`
- Implementation screenshot: `/tmp/slow-visual-qa.63YiXN/implementation-final-1470x835.png`
- Desktop viewport: 1470 × 835 CSS px, device pixel ratio 1, screenshot 1470 × 835 px
- Mobile viewport: 390 × 844 CSS px, device pixel ratio 1, screenshot `/tmp/slow-visual-qa.63YiXN/implementation-mobile-390x844.png`
- State: initial mode unselected, 1 hour selected, primary action disabled

## Full-view comparison evidence

The controlled browser opened and captured the React implementation at the target desktop and mobile viewports. The source HTML could not be opened by that browser because local `file://` navigation is blocked by its URL safety policy. The source therefore has no browser-rendered screenshot or independently measured pixel dimensions in this run.

The implementation ports the source DOM hierarchy, copy, mascot assets, colors, spacing values, breakpoints, selected-state fills, diagonal divider, duration controls, expiry preview, and primary action directly into the production component. The desktop dialog measured 1060 × 577.63 CSS px, with a 330 px versus arena, matching the source CSS specifications.

## Focused region evidence

- Rabbit asset rendered at 155 × 125 CSS px and turtle asset at 200 × 125 CSS px on desktop, matching the source slots.
- At 390 × 844, the page had `body.scrollWidth = 390`, so no horizontal overflow was present.
- Fast/Slow selection worked by mouse and arrow keys; duration selection worked by mouse and arrow keys.
- Submitting Slow for 3 hours closed the dialog and updated the header state through the real local API.
- The header duration popover opened, accepted a new duration, and closed after the update.
- Browser console errors: none.

## Comparison history

1. The supplied production screenshot showed the old text-heavy Fast/Slow cards and character circles rather than the source prototype's rabbit and turtle mascots.
2. The React dialog was replaced with the source prototype's structure and assets, and the header control was aligned with the same prototype.
3. Desktop and mobile implementation captures showed the expected mascot, diagonal VS, selected fill, duration preview, disabled/enabled CTA, and responsive stacked layout.
4. A final source-to-implementation image comparison could not be completed because the source HTML capture was blocked.

## Findings

- No actionable P0/P1/P2 issue was found in the rendered implementation itself.
- Formal visual equivalence remains unverified until the source HTML and implementation can be captured at the same viewport and placed into one comparison image.

## Primary interactions tested

- Fast and Slow selection
- Mode selection by arrow keys from the initial unselected state
- Four duration choices and keyboard traversal
- Disabled and enabled primary-action states
- Real local submission and dialog close
- Header duration popover update
- Desktop and mobile responsive layouts

## Final result

final result: blocked

Blocker: the controlled browser's URL policy prevents opening the local source HTML, so the required same-viewport source capture is unavailable.
