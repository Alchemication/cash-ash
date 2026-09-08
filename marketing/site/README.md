# CashAsh public site — source

The landing page for <https://alchemication.github.io/cash-ash/>. The docs
section of that site is generated from `docs/`, not stored here.

## Build the whole site

```bash
uv run --group site python marketing/build.py
python3 -m http.server -d _site 8765
```

`_site/` is gitignored and rebuilt from scratch on every run. CI does the same
via `.github/workflows/pages.yml`, on every push to `main` that touches the
site, the docs, the README, or `src/config.py`. Enable publishing once with
**Settings → Pages → Source: GitHub Actions**.

## The design

A ledger sheet, kept in the dark. Ash-black ground, faint ruling, and one
ember margin line. Ember is the brand and the calls to action; the two verdict
colours are spent on meaning only — teal for a sourced finding or an
approval, red for anything the rules refused — so neither ever decorates. The
hero is a question beside a stone dollar going to ash; the first entry below
it is one week's review rendered as the artifact the app produces, with a
proposal the rules overruled. Section headings sit in the margin; the content
sits to its right.

Two typefaces, both self-hosted under `assets/fonts/` so no page makes an
external request: Bricolage Grotesque (variable; headings, the sheet, all
chrome) and Newsreader (variable, with italic; body prose).

## What lives here

- `assets/base.css` — **the single source of truth for the palette, the
  typefaces and the site chrome.** Never linked with `<link>`; every page
  inlines it at build time so each output file stays self-contained. Two
  consumers: the landing page (`{{BASE_CSS}}`) and the docs template in
  `marketing/build.py`. Do not redeclare a colour token anywhere else. Its
  `@font-face` URLs are written relative to the site root; `css_for_depth()`
  in the build rewrites them for pages under `docs/`.
- `index.html` — the landing page. Page-specific CSS only; the shared chrome
  arrives via the token. Opening it straight from disk looks unstyled, which is
  expected — build the site to view it. Two pieces of motion: the stamp on
  the hero sheet, which lands once on load, and the ash — the inline script at
  the foot of the page drawing a few dozen grey flecks settling and the odd
  ember rising on a fixed canvas behind the content. Both are off under
  `prefers-reduced-motion`; the canvas also pauses while the tab is hidden
  and reads its two colours from the stylesheet's tokens.
- `assets/fonts/` — the three `.woff2` files, downloaded once from Google
  Fonts (latin subset, variable).
- `assets/cash-ash-hero.jpg` — the hero image, a generated stone dollar
  crumbling into ash. Resampled from the 1254px source with `sips` at quality
  76 and 1000px wide; the source is not kept in the repository. The same file
  is composited into the link preview.
- `assets/favicon.svg` — the ledger mark. The same drawing is inlined beside
  the wordmark on every page.
- `assets/og-image.png` — the link preview, rendered from `../og-image.html`
  with headless Chrome. The command is in that file's header comment.
  Regenerate it if the headline or palette changes.

## Build-time placeholders

`index.html` may contain `{{PLACEHOLDER}}` tokens, resolved by
`landing_placeholders()` in `marketing/build.py`. The build fails rather than
publishing an unresolved token. Currently:

| Token | Source |
| --- | --- |
| `{{BASE_CSS}}` | `assets/base.css`, inlined |
| `{{MAX_POSITION_WEIGHT_PCT}}` | `src/config.py` |
| `{{MAX_NEW_TRADE_EUR}}` | `src/config.py` |
| `{{MAX_WEEKLY_ALLOCATION_EUR}}` | `src/config.py` |
| `{{RECOMMENDATION_EXPIRY_DAYS}}` | `src/config.py` |
| `{{PRICE_STALE_AFTER_DAYS}}` | `src/config.py` |
| `{{BENCHMARK_NAME}}` | `src/config.py` |
| `{{WEEKLY_RUN_DAY}}` | `src/config.py`, weekday name with Sunday as 0 |
| `{{WEEKLY_RUN_TIME}}` | `src/config.py`, `HH:00` |

The config module honours `CASH_ASH_*` environment overrides and a local
`.env`, so a build on a machine with overrides set prints those. CI has none,
so the published figures are the shipped defaults.

Never write a placeholder token literally inside `base.css`: it is inlined into
the landing page, so the token would reappear after substitution and fail the
unresolved-token check.

## Conventions worth keeping

- The review sheet in the hero is illustrative, and the page says so. The
  tickers are the invented ones from `seed_snapshot.example.toml`. Never
  replace them with real holdings: the repository must hold no personal data.
- The "cost more than it earns" section matches the bluntness of `CLAUDE.md`:
  the pipeline may cost more than it earns, model output is not investment
  authority, evals measure process. Do not soften it, and never add a figure
  that reads as a return, a hit rate, or a confidence — not even as an example
  of what the app refuses to show.
- The page describes the software and its shipped defaults, never one
  person's instance. Guardrails and the run schedule come from `src/config.py`
  through placeholders; nothing about any real portfolio — size, count,
  holdings — belongs on it. Per-user values are the app's job to print.
