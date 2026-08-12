# Study time design QA

- Source visual truth: `/Users/pix/.codex/generated_images/019ff67e-84cb-7ac3-a943-b3d7432e5cd7/exec-c26b8af2-517d-4bd1-9c67-f725dceafa3f.png`
- Implementation screenshot: `/private/tmp/slow-study-home-activity.png`
- Timeline screenshot: `/private/tmp/slow-study-home-timeline.png`
- Reader with Ask AI screenshot: `/private/tmp/slow-study-reader-ask-ai.png`
- Mobile screenshot: `/private/tmp/slow-study-home-mobile.png`
- Full comparison evidence: `/private/tmp/slow-study-design-qa-full.jpg`
- Focused comparison evidence: `/private/tmp/slow-study-design-qa-focus.jpg`
- Viewport: 1488 × 1059 CSS px, device scale factor 1; responsive check at 390 × 844 CSS px
- Source pixels: 1485 × 1059; normalized to 1488 × 1059 with 3 px white padding
- Implementation pixels: 1488 × 1059
- State: homepage, study-time popover open, “按活动” selected, real server-derived data

## Findings

No actionable P0, P1, or P2 differences remain in the study-time feature.

- Fonts and typography: the total is the dominant element, the popover title and rows retain the existing Slow editorial hierarchy, and utility labels use the existing UI font stack.
- Spacing and layout rhythm: the total, inventory summary, create action, segmented switch, and detail rows follow the selected hierarchy. The implementation preserves the repository’s existing page width and real content density rather than replacing the whole dashboard with the generated screen.
- Colors and visual tokens: forest green, warm paper background, rules, and low elevation map to existing Slow tokens and the selected visual.
- Image quality and asset fidelity: the changed surface contains no raster imagery or missing custom assets. The existing brand mark and decorative dashboard elements were preserved.
- Copy and content: “今天已投入”, “按活动”, “时间线”, the three activity labels, and the timeline wording match the approved direction. The rejected explanatory footer is absent.

P3 follow-up: the generated concept includes a small pointer notch between the popover and total. The implementation uses proximity and shared focus state instead, avoiding an extra decorative shape; anchoring remains clear.

## Interaction and browser checks

- Hover/focus/click trigger opens the popover; click pins it; outside click and Escape close it.
- “按活动 / 时间线” switches in place without moving the dashboard.
- A real reading interval appeared in the total, category view, and timeline from the same server facts.
- Opening Ask AI added Ask AI time while leaving the reader and right panel unobscured.
- Mobile tap state remains within the 390 px viewport.
- Browser console errors and warnings checked: none.

## Comparison history

1. Initial browser render exposed a P1 hierarchy issue: the total used an undefined font token, so the number rendered at normal button size.
2. The total was corrected to the project’s `--font-ui` stack and recaptured at the same viewport.
3. Post-fix full and focused comparisons show the intended dominant number, visible segmented switch, compact rows, and stable surrounding layout. No P0/P1/P2 findings remain.

## Implementation checklist

- [x] Desktop total and popover
- [x] Activity and timeline states
- [x] Responsive mobile state
- [x] Reader session time and Ask AI coexistence
- [x] Keyboard focus and Escape behavior
- [x] Console check

final result: passed
