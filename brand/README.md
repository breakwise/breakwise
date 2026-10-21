# Brand

The Breakwise identity grows from one picture: **a sheet with a lifted corner**.
A notice is a page. When a release changes something, one corner of the page moves.
Breakwise shows you that corner and sets it back. Everything else stays quiet, so the change is the one thing you see.

## Files

| File | Use |
|---|---|
| [`logo/breakwise-logo.svg`](logo/breakwise-logo.svg) | The mark with the wordmark, on light grounds |
| [`logo/breakwise-logo-dark.svg`](logo/breakwise-logo-dark.svg) | The same, on dark grounds |
| [`logo/breakwise-mark.svg`](logo/breakwise-mark.svg) | The mark alone, for square spaces on light grounds |
| [`logo/breakwise-mark-dark.svg`](logo/breakwise-mark-dark.svg) | The mark alone, on dark grounds |
| [`logo/breakwise-mark-mono.svg`](logo/breakwise-mark-mono.svg) | The mark in one ink, for one-color printing |
| [`avatar.png`](avatar.png) | 1024 × 1024 profile picture for the GitHub organization and other accounts |
| [`social-preview.png`](social-preview.png) | 1280 × 640 image shown when the repository is shared |
| [`tokens.json`](tokens.json) | Colors for both themes, type styles, spacing and radii: the source for any Breakwise interface |

The wordmark in the SVG files is set in outlines, so they need no font.

## Logo

- The mark is a sheet on a 24-unit grid. Its top-right corner, 9 units on a side, is cut along the diagonal and lifted 2.2 units straight away from the cut.
- In the lockup the sheet is as tall as the **b** of the wordmark, and the gap between them equals the corner.
- Keep clear space equal to the corner on every side. Don't use the mark below 16px or the lockup below 20px tall.
- Don't rotate the mark, outline it, add shadows or gradients, close the gap, recolor the corner, or set "breakwise" in another typeface.

## Color

| Token | Light | Dark | Use |
|---|---|---|---|
| `sheet` | `#f4f5f1` | `#0d1317` | Page background |
| `sheet-raised` | `#ffffff` | `#141c22` | Cards and notices |
| `sheet-sunken` | `#eceee9` | `#090d10` | Code, diffs, terminal output |
| `ink` | `#11181d` | `#e7ebe6` | Text and the logo's sheet |
| `ink-muted` | `#53616a` | `#97a4ac` | Secondary text |
| `shift` | `#e8a33d` | `#f0ae4b` | The lifted corner: marks what changed |
| `shift-ink` | `#875610` | `#f4c374` | Text that points at a change |
| `jade` | `#1f6b4b` | `#6ccba3` | Verified notices, added lines |
| `oxide` | `#a33a27` | `#f2907a` | Removed lines, failed checks |

- `shift` means one thing: *this changed*. Never use it for decoration, buttons or links. On light grounds it is a graphic beside a word, never text.
- `jade` and `oxide` always come with their word or sign (Verified, `ok`, `FAIL`, `+`, `−`), never with color alone.
- Every text color reads at 4.5:1 or better on every sheet color, in both themes. The full set, with borders and soft tints, is in [`tokens.json`](tokens.json).

## Type

- [Instrument Sans](https://fonts.google.com/specimen/Instrument+Sans) for everything people read.
- [Martian Mono](https://fonts.google.com/specimen/Martian+Mono) for everything code reads: names, versions, IDs, paths and commands.
- Both are free under the SIL Open Font License.

## Voice

Say what changed, since which version, and how to fix it, in that order.

- Write plainly and exactly, in short sentences. A breaking change is information, not an emergency.
- Use sentence case. The product is **Breakwise** in prose; lowercase **breakwise** appears only in the logo and on the command line.
- Write names, versions and IDs exactly as code has them: `OpenAIModel`, `2.0.0`, `BW-2026-0001`.
- No exclamation marks, no hype, no blame, no emoji.

| Do | Don't |
|---|---|
| OpenAIModel was renamed to OpenAIChatModel, and the old name was removed in 2.0.0. | Heads up! pydantic-ai just broke your app |
| 1 breaking change in this upgrade. Apply the fix, or read the notice first. | Oops, something went wrong with your dependencies. |
