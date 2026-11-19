"""Design system for the supervisor console: tokens, CSS, shared widgets.

The UI used to be one flat navy sheet: page, drawer, cards and expanded
panels all sat within a few RGB points of each other, every title was the same
weight, and accent blue was spent on body links. Nothing said "this is a
surface", "this is a section", "this is the one thing to do now".

This module fixes that with three rules, applied everywhere:

1. **Elevation is a ladder, not a shade.** canvas < panel < raised field
   (``--surface-0..3``), each step separated by a hairline border *and* a
   visible luminance step, so a panel reads as an object on a background.
2. **Colour carries meaning only.** Blue = interactive/in flight,
   green = success, amber = blocked/attention, red = failure, violet = the
   judge. Everything structural is neutral. A page full of coloured text is
   a page where colour means nothing.
3. **Type has four distinct levels** — page title, panel title, group label,
   body/meta — with fixed sizes, tracking and colour, so scan order is
   predictable without reading.

Widgets here are thin: they wrap NiceGUI primitives with the classes above so
pages describe *intent* ("a panel titled Protocol") instead of repeating
Tailwind strings, which is how the six shades of near-black accumulated.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from html import escape

from nicegui import ui

_RULES = """
:root {
  --sop-canvas: #080a0e;
  --sop-surface-1: #131922;
  /* Raised from #1a212c: the field-to-panel step was 1.091:1, weaker than the
     border drawn around it, so inputs read as "slightly lighter rectangles"
     instead of recessed fields. */
  --sop-surface-2: #1b2430;
  --sop-surface-3: #232c3a;
  --sop-line: #29323f;
  --sop-line-2: #3a4557;
  --sop-text-1: #e9eef6;
  --sop-text-2: #a9b4c5;
  /* Lifted together with surface-2: #7d8a9e fell to 4.47:1 on the new
     field surface, taking 27 elements with it (23 of them .q-field__label).
     Never place this colour on surface-3 — it lands at 4.34 and fails AA. */
  --sop-text-3: #8794a8;
  --sop-accent: #4f8cff;
  --sop-accent-2: #8fbaff;
  /* Fill variant for surfaces that carry white text: white on #4f8cff is only
     3.22:1, while white on #3370e0 is 4.63:1. Borders/glow keep --sop-accent. */
  --sop-accent-fill: #3370e0;
  --sop-accent-soft: rgba(79, 140, 255, 0.13);
  --sop-ok: #3ecf8e;
  --sop-warn: #f2b955;
  --sop-bad: #ff6b6b;
  --sop-judge: #b48cff;
  --sop-radius: 10px;
  /* ── Scales ────────────────────────────────────────────────────────────
     The stylesheet had grown 14 type sizes, 20 spacing values, 9 radii and 7
     control heights, all written as literals. These are the account books:
     new rules should reference them instead of inventing a value. Current
     usage is only partially migrated — that is deliberate, so a rename does
     not reshuffle the page. */
  --fs-h1: 29px;      /* page title */
  --fs-panel: 13.5px; /* panel / fold title */
  --fs-body: 16px;    /* body copy */
  --fs-meta: 14px;    /* meta, hints */
  --fs-label: 12.5px; /* uppercase micro-label only (needs letter-spacing) */
  --sp-1: 4px; --sp-2: 8px; --sp-3: 12px; --sp-4: 16px; --sp-5: 24px; --sp-6: 40px;
  --r-sm: 6px; --r-md: 8px; --r-lg: 10px;
  --ctl-h: 42px; --ctl-h-dense: 36px; --row-h: 52px;
  /* One definition for the two header bands (panel + fold). They used to be
     pasted as literal gradients in two places, and the top stop (#1e2734)
     was also the panel-header colour — a fourth name for a shade that the
     surface ladder already owned. */
  --sop-head-grad: linear-gradient(
    180deg,
    color-mix(in srgb, var(--sop-surface-3) 62%, var(--sop-surface-2)),
    var(--sop-surface-2)
  );
  --sop-shadow: 0 1px 2px rgba(0, 0, 0, 0.45), 0 8px 24px -18px rgba(0, 0, 0, 0.9);
  /* Cascadia first: it ships with Windows, whereas JetBrains Mono is absent by
     default and there is no @font-face — putting it first meant every machine
     silently resolved to a different face and changed alignment. */
  --sop-mono: "Cascadia Mono", "JetBrains Mono", Consolas, "SF Mono", ui-monospace, monospace;
}

/* ── Base ──────────────────────────────────────────────────────────────── */

body.body--dark {
  background: var(--sop-canvas);
  color: var(--sop-text-1);
  font-family: "Segoe UI Variable Display", "Segoe UI", Inter, "Noto Sans SC",
    "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
  font-size: 13.5px;
  -webkit-font-smoothing: antialiased;
}
/* Long tokens (paths, hashes, ids) break instead of forcing a horizontal
   scrollbar; one-line strips opt back out with .nowrap. */
body { overflow-wrap: anywhere; }
html { scroll-behavior: smooth; }
.nicegui-row > *, .nicegui-column > * { min-width: 0; max-width: 100%; }

/* Page canvas: one column, capped so a 2 m monitor does not stretch forms
   into unwieldy 180-character lines. */
.nicegui-content {
  padding: 24px 28px 54px;
  gap: 18px;
  width: 100%;
  max-width: 1460px;
  margin: 0 auto;
}

h1, h2, h3 { margin: 0; font-weight: 650; letter-spacing: -0.01em; }
a { color: var(--sop-accent-2); text-decoration: none; }
a:hover { color: var(--sop-accent); }
a.nav-item, a.nav-item:hover { text-decoration: none; color: inherit; }
.mono { font-variant-numeric: tabular-nums; }
/* Components still spell a few labels as Tailwind sizes. They must follow the
   scale instead of pinning the old 12px (this rule sits in the last layer, so
   it outranks the utility without !important). */
.text-xs { font-size: 12.5px; line-height: 1.5; }
.text-sm { font-size: 13.5px; line-height: 1.55; }

*:focus-visible {
  outline: 2px solid var(--sop-accent);
  outline-offset: 2px;
  border-radius: 4px;
}
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb {
  background: #2b3341; border-radius: 999px; border: 2px solid var(--sop-canvas);
}
::-webkit-scrollbar-thumb:hover { background: #3a4557; }

/* ── Page header ───────────────────────────────────────────────────────── */

.sop-pagehead { gap: 2px; margin-bottom: 2px; }
.sop-kicker {
  font-size: 10.5px; font-weight: 700; letter-spacing: 0.14em;
  text-transform: uppercase; color: var(--sop-text-3);
}
.sop-h1 {
  font-size: var(--fs-h1); font-weight: 700; letter-spacing: -0.02em;
  color: var(--sop-text-1); line-height: 1.25;
}
.sop-sub { font-size: 12.5px; color: var(--sop-text-2); line-height: 1.55; }
.sop-h2 {
  font-size: 15px; font-weight: 650; color: var(--sop-text-1);
  letter-spacing: -0.01em; display: flex; align-items: center; gap: 8px;
}
.sop-h2 .q-icon { font-size: 17px; color: var(--sop-text-3); }
.sop-section-head {
  display: flex; align-items: center; gap: 10px; width: 100%;
  margin-top: 6px; padding-bottom: 8px; border-bottom: 1px solid var(--sop-line);
}
.sop-section-head__count {
  font-family: var(--sop-mono); font-size: 11px; color: var(--sop-text-3);
  background: var(--sop-surface-2); border: 1px solid var(--sop-line-2);
  border-radius: 5px; padding: 0 6px;
}
.sop-h1-actions { gap: 8px; align-items: center; }

/* ── Panel ─────────────────────────────────────────────────────────────── */

.sop-panel {
  width: 100%;
  background: var(--sop-surface-1);
  border: 1px solid var(--sop-line);
  border-radius: var(--sop-radius);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.035), var(--sop-shadow);
  overflow: hidden;
  gap: 0;
}
.sop-panel__head {
  display: flex; align-items: center; gap: 10px;
  padding: 13px 19px;
  background: var(--sop-head-grad);
  border-bottom: 1px solid var(--sop-line);
  min-height: 56px;
}
.sop-panel__icon {
  display: inline-flex; align-items: center; justify-content: center;
  width: 30px; height: 30px; border-radius: 8px; flex: none;
  background: var(--sop-surface-3);
  border: 1px solid var(--sop-line-2);
  color: var(--sop-accent-2);
  font-size: 15px;
}
.sop-panel__titles { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
.sop-panel__title {
  font-size: 11.5px; font-weight: 700; letter-spacing: 0.1em;
  text-transform: uppercase; color: var(--sop-text-1); line-height: 1.35;
}
.sop-panel__sub { font-size: 11.5px; color: var(--sop-text-3); line-height: 1.4; }
.sop-panel__body {
  padding: 16px 18px 18px; gap: 14px; width: 100%;
}
.sop-panel--plain .sop-panel__head { background: none; border-bottom: none; }
.sop-panel--warn { border-color: rgba(242, 185, 85, 0.34); }
.sop-panel--warn .sop-panel__icon { color: var(--sop-warn); border-color: rgba(242, 185, 85, 0.4); }
.sop-panel--quiet { background: #0f1319; box-shadow: none; }
.sop-panel--quiet .sop-panel__head { background: none; }
.sop-panel--quiet .sop-panel__title { color: var(--sop-text-2); }

/* Panel-body layout helpers */
.sop-grid { display: grid; gap: 12px; width: 100%; }
.sop-grid--2 { grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }
.sop-row { display: flex; flex-wrap: wrap; gap: 12px; align-items: flex-end; width: 100%; }
.sop-divider { height: 1px; background: var(--sop-line); width: 100%; margin: 2px 0; }

.sop-group {
  font-size: 10.5px; font-weight: 700; letter-spacing: 0.12em;
  text-transform: uppercase; color: var(--sop-text-3);
  display: flex; align-items: center; gap: 8px; width: 100%;
}
.sop-group::after {
  content: ""; flex: 1; height: 1px;
  background: linear-gradient(90deg, var(--sop-line), transparent);
}
.sop-hint { font-size: 11.5px; color: var(--sop-text-3); line-height: 1.55; }
.sop-meta { font-size: 11.5px; color: var(--sop-text-2); }
.sop-value { font-size: 13.5px; color: var(--sop-text-1); }

/* ── Fields (Quasar standard -> filled box) ────────────────────────────── */

.q-field--standard .q-field__control {
  background: var(--sop-surface-2);
  border: 1px solid var(--sop-line);
  border-radius: 8px;
  padding: 0 11px;
  min-height: 42px;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}
.q-field--standard .q-field__control:before,
.q-field--standard .q-field__control:after { display: none; }
.q-field--standard .q-field__control:hover { border-color: var(--sop-line-2); }
.q-field--standard.q-field--focused .q-field__control {
  border-color: var(--sop-accent);
  box-shadow: 0 0 0 3px var(--sop-accent-soft);
}
.q-field--standard.q-field--error .q-field__control { border-color: var(--sop-bad); }
.q-field--standard .q-field__label {
  color: var(--sop-text-3); font-size: 10.5px; letter-spacing: 0.04em;
  font-weight: 600; text-transform: uppercase;
}
.q-field--standard .q-field__native,
.q-field--standard .q-field__input,
.q-field--standard .q-field__marginal {
  color: var(--sop-text-1); font-size: 13px;
}
.q-field--standard .q-field__native::placeholder { color: var(--sop-text-3); }
.q-field--standard.q-field--dense .q-field__control { min-height: 36px; }
.q-field--standard textarea.q-field__native { padding: 6px 0; line-height: 1.55; }
.q-field__bottom { padding: 4px 2px 0; color: var(--sop-text-3); font-size: 11px; }
.q-field--dark .q-field__native { color: var(--sop-text-1); }
.q-select__dropdown { background: var(--sop-surface-2); }
.q-menu {
  background: var(--sop-surface-2); border: 1px solid var(--sop-line-2);
  border-radius: 8px; box-shadow: var(--sop-shadow);
}
.q-item { color: var(--sop-text-2); font-size: 14px; min-height: 38px; }
.q-item.q-item--active, .q-item--active { color: var(--sop-accent-2); }
.q-item.q-manual-focusable:hover { background: var(--sop-surface-3); }
.q-checkbox__inner, .q-toggle__inner { color: var(--sop-accent); }
.q-checkbox__label, .q-toggle__label { font-size: 12.5px; color: var(--sop-text-2); }
.q-separator { background: var(--sop-line); }

/* ── Buttons ───────────────────────────────────────────────────────────── */

.q-btn {
  border-radius: var(--r-md); text-transform: none; letter-spacing: 0.01em;
  font-weight: 600; font-size: 12.5px; min-height: var(--ctl-h); padding: 0 14px;
  transition: background 0.15s ease, border-color 0.15s ease, transform 0.1s ease;
}
.q-btn--dense { min-height: var(--ctl-h-dense); padding: 0 10px; font-size: 12px; }
.q-btn .q-icon { font-size: 17px; }
/* Colour and border live in @layer sop-important: they have to outrank
   Quasar's own !important colour utilities. */
.q-btn--outline { background: var(--sop-surface-2); }
.q-btn--outline:hover { background: var(--sop-accent-soft); }
.q-btn--flat:hover { background: var(--sop-surface-2); }
.q-btn.disabled, .q-btn[disabled], .q-btn--disabled {
  /* opacity now wins the cascade (see @layer sop-important), so the old
     filter() workaround is gone: filter created a containing block, forced a
     compositing layer and dropped 12px text to greyscale antialiasing. */
  cursor: not-allowed;
}
/* Quasar ships its colour utilities (.bg-primary, .text-red) inside an
   @layer with !important, and a layered !important outranks every unlayered
   rule regardless of specificity. Rather than fight that layer order, the
   palette is taken from ui.colors() — the primary is the same accent — and
   only the properties Quasar leaves alone (shadow, radius, spacing, hover
   filter) are set here. A locked action is an outline button, never a dimmed
   primary, so "unavailable" never looks like "nearly ready". */
.q-btn.bg-primary {
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.14),
    0 6px 18px -12px rgba(79, 140, 255, 0.95);
}
.q-btn.bg-primary:not(.disabled):hover { filter: brightness(1.07); }
.q-btn.bg-primary:active { transform: translateY(1px); }

/* ── Pills / state badges ──────────────────────────────────────────────── */

.pill {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 1px 8px 1px 6px; border-radius: 6px;
  font-size: 10.5px; font-weight: 700; letter-spacing: 0.07em;
  font-family: var(--sop-mono); line-height: 1.7;
  border: 1px solid transparent; white-space: nowrap; vertical-align: middle;
}
.pill::before {
  content: ""; width: 6px; height: 6px; border-radius: 50%;
  background: currentColor; box-shadow: 0 0 0 2px rgba(255, 255, 255, 0.04);
  flex: none;
}
.pill-idle { background: var(--sop-surface-2); color: var(--sop-text-3); border-color: var(--sop-line-2); }
.pill-running {
  background: rgba(79, 140, 255, 0.14); color: var(--sop-accent-2);
  border-color: rgba(79, 140, 255, 0.42);
}
.pill-running::before { animation: sop-pulse 1.6s ease-in-out infinite; }
.pill-success { background: rgba(62, 207, 142, 0.13); color: var(--sop-ok); border-color: rgba(62, 207, 142, 0.38); }
.pill-failure { background: rgba(255, 107, 107, 0.13); color: var(--sop-bad); border-color: rgba(255, 107, 107, 0.38); }
.pill-blocked { background: rgba(242, 185, 85, 0.13); color: var(--sop-warn); border-color: rgba(242, 185, 85, 0.4); }
/* Cancelled by the operator: neutral, same family as pill-idle. Red is
   reserved for FAILED, otherwise every stopped run looks like a crash. */
.pill-cancelled { background: var(--sop-surface-2); color: var(--sop-text-2); border-color: var(--sop-line-2); }
@keyframes sop-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.25; } }

.sop-chips { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.chip {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 3px 9px; border-radius: 6px;
  background: var(--sop-surface-2); border: 1px solid var(--sop-line-2);
  color: var(--sop-text-2); font-size: 11px; line-height: 1.7;
}
.chip b { color: var(--sop-text-1); font-weight: 600; }
.chip--accent { color: var(--sop-accent-2); border-color: rgba(79, 140, 255, 0.35); background: var(--sop-accent-soft); }
.chip--ok { color: var(--sop-ok); border-color: rgba(62, 207, 142, 0.35); background: rgba(62, 207, 142, 0.1); }
.chip--warn { color: var(--sop-warn); border-color: rgba(242, 185, 85, 0.35); background: rgba(242, 185, 85, 0.1); }
.chip--bad { color: var(--sop-bad); border-color: rgba(255, 107, 107, 0.35); background: rgba(255, 107, 107, 0.1); }

/* ── Notices (inline status strips) ────────────────────────────────────── */

.sop-notice {
  display: flex; align-items: flex-start; gap: 10px; width: 100%;
  padding: 11px 14px; border-radius: 8px;
  background: var(--sop-surface-2); border: 1px solid var(--sop-line);
  border-left: 2px solid var(--sop-line-2);
  font-size: 12.5px; color: var(--sop-text-2); line-height: 1.55;
}
.sop-notice .q-icon { font-size: 17px; margin-top: 1px; }
.sop-notice--info { border-left-color: var(--sop-accent); background: rgba(79, 140, 255, 0.07); }
.sop-notice--info .q-icon { color: var(--sop-accent-2); }
.sop-notice--ok { border-left-color: var(--sop-ok); background: rgba(62, 207, 142, 0.07); }
.sop-notice--ok .q-icon { color: var(--sop-ok); }
.sop-notice--warn { border-left-color: var(--sop-warn); background: rgba(242, 185, 85, 0.08); }
.sop-notice--warn .q-icon { color: var(--sop-warn); }
.sop-notice--bad { border-left-color: var(--sop-bad); background: rgba(255, 107, 107, 0.08); }
.sop-notice--bad .q-icon { color: var(--sop-bad); }
.sop-notice__text { color: var(--sop-text-2); }
.sop-notice b { color: var(--sop-text-1); }

/* ── Stepper (setup progress) ──────────────────────────────────────────── */

/* The stepper is a table of contents, so it has to stay on screen: the page
   is three viewports long. top matches the fixed header height. */
.sop-steps {
  display: flex; align-items: stretch; gap: 0; width: 100%;
  background: var(--sop-surface-1); border: 1px solid var(--sop-line);
  border-radius: var(--sop-radius); overflow: hidden;
  position: sticky; top: 50px; z-index: 6;
}
.sop-step {
  flex: 1 1 0; min-width: 0; display: flex; align-items: center; gap: 9px;
  padding: 12px 14px; border-right: 1px solid var(--sop-line);
  position: relative; background: none;
  /* Rendered as <a href="#section">: clickable, focusable, keyboard-native. */
  text-decoration: none; color: inherit; cursor: pointer;
}
a.sop-step:not(.sop-step--current):hover { background: var(--sop-surface-2); }
/* Anchor targets would otherwise land under the sticky stepper. */
.sop-fold[id], .sop-panel[id] { scroll-margin-top: 128px; }
.sop-step:last-child { border-right: none; }
.sop-step__num {
  width: 20px; height: 20px; border-radius: 50%; flex: none;
  display: inline-flex; align-items: center; justify-content: center;
  font-size: 10.5px; font-weight: 700; font-family: var(--sop-mono);
  border: 1px solid var(--sop-line-2); color: var(--sop-text-3);
  background: var(--sop-surface-2);
}
.sop-step__label {
  font-size: 11.5px; font-weight: 600; color: var(--sop-text-3);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.sop-step--done .sop-step__num { color: var(--sop-ok); border-color: rgba(62, 207, 142, 0.45); background: rgba(62, 207, 142, 0.12); }
.sop-step--done .sop-step__label { color: var(--sop-text-2); }
.sop-step--current { background: rgba(79, 140, 255, 0.08); }
.sop-step--current .sop-step__num { color: #fff; background: var(--sop-accent-fill); border-color: transparent; }
.sop-step--current .sop-step__label { color: var(--sop-text-1); }
.sop-step--current::after {
  content: ""; position: absolute; left: 0; right: 0; bottom: 0; height: 2px;
  background: var(--sop-accent);
}

.sop-next {
  display: flex; align-items: center; gap: 8px; width: 100%;
  padding: 0 2px; margin-top: -4px;
}
.sop-next .sop-kicker { color: var(--sop-text-3); }

/* ── Drawer / nav ──────────────────────────────────────────────────────── */

.sop-drawer { background: #06070a; border-right: 1px solid var(--sop-line); }
.sop-brand { display: flex; align-items: center; gap: 10px; padding: 14px 14px 12px; }
.sop-brand__mark {
  width: 28px; height: 28px; border-radius: 8px; flex: none;
  display: inline-flex; align-items: center; justify-content: center;
  background: linear-gradient(150deg, #4f8cff, #7a5cff);
  box-shadow: 0 4px 14px -6px rgba(79, 140, 255, 0.9);
  color: #fff; font-size: 16px;
}
.sop-brand__name { font-size: 13px; font-weight: 700; color: var(--sop-text-1); line-height: 1.2; }
.sop-brand__sub { font-size: 10.5px; letter-spacing: 0.1em; text-transform: uppercase; color: var(--sop-text-3); }
.sop-nav { display: flex; flex-direction: column; gap: 2px; padding: 0 8px; width: 100%; }
.sop-nav__caption {
  font-size: 9.5px; font-weight: 700; letter-spacing: 0.14em; text-transform: uppercase;
  /* Structural labels must stay legible: #5b667a measured 3.48:1 on the rail. */
  color: var(--sop-text-3); padding: 12px 8px 5px;
}
.nav-item {
  display: flex; align-items: center; gap: 10px;
  padding: 8px 10px; border-radius: 8px; color: var(--sop-text-2);
  font-size: 12.5px; font-weight: 500; position: relative;
  border: 1px solid transparent; transition: background 0.13s ease, color 0.13s ease;
}
.nav-item .q-icon { font-size: 17px; color: var(--sop-text-3); }
.nav-item:hover { background: #10141b; color: var(--sop-text-1); }
.nav-item:hover .q-icon { color: var(--sop-text-2); }
.nav-item--active {
  background: var(--sop-accent-soft); color: var(--sop-text-1);
  border-color: rgba(79, 140, 255, 0.28); font-weight: 600;
}
.nav-item--active .q-icon { color: var(--sop-accent-2); }
.nav-item--active::before {
  content: ""; position: absolute; left: -8px; top: 8px; bottom: 8px; width: 2px;
  border-radius: 2px; background: var(--sop-accent);
}
.nav-item__badges {
  margin-left: auto; display: inline-flex; align-items: center; gap: 4px; flex: none;
}
.nav-item__badge {
  font-family: var(--sop-mono); font-size: 10px;
  color: var(--sop-text-3); background: #10141b; border: 1px solid var(--sop-line);
  border-radius: 5px; padding: 0 5px; line-height: 1.6;
}
.nav-item__badge--live { color: var(--sop-accent-2); border-color: rgba(79, 140, 255, 0.4); background: var(--sop-accent-soft); }

.sop-drawer__foot {
  /* #4d5769 measured 2.77:1 — the least readable text in the whole chrome. */
  padding: 10px 16px 14px; font-size: 10.5px; color: var(--sop-text-3);
  border-top: 1px solid var(--sop-line); width: 100%;
}

/* ── Header bar ────────────────────────────────────────────────────────── */

.sop-header {
  background: rgba(10, 12, 17, 0.92); border-bottom: 1px solid var(--sop-line);
  min-height: 50px; padding: 0 20px; gap: 10px;
  /* Sits above the drawer's own stacking context so the rail does not
     paint over the sticky bar when the drawer is toggled. */
  backdrop-filter: blur(8px);
}
.sop-header__where {
  display: inline-flex; align-items: center; gap: 7px; min-width: 0;
  color: var(--sop-text-2); font-size: 12px;
}
.sop-header__where .q-icon { color: var(--sop-text-3); font-size: 16px; }
.sop-header__sep { width: 1px; height: 16px; background: var(--sop-line-2); flex: none; }
.sop-header__chip {
  display: inline-flex; align-items: center; gap: 5px;
  font-size: 11px; color: var(--sop-text-2);
  background: var(--sop-surface-1); border: 1px solid var(--sop-line);
  border-radius: 6px; padding: 1px 8px; max-width: 320px;
}
.sop-header__chip span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* ── Job cards ─────────────────────────────────────────────────────────── */

.sop-cards { display: grid; gap: 10px; width: 100%; }
.sop-cards--jobs { grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); }
.sop-jobcard {
  position: relative; width: 100%;
  background: var(--sop-surface-1); border: 1px solid var(--sop-line);
  border-radius: var(--sop-radius); padding: 12px 14px 10px 16px;
  gap: 7px; box-shadow: var(--sop-shadow); overflow: hidden;
  transition: border-color 0.15s ease, transform 0.15s ease;
}
.sop-jobcard:hover { border-color: var(--sop-line-2); }
.sop-jobcard::before {
  content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 3px;
  background: var(--sop-line-2);
}
.sop-jobcard--running::before { background: var(--sop-accent); }
.sop-jobcard--success::before { background: var(--sop-ok); }
.sop-jobcard--failed::before { background: var(--sop-bad); }
/* Cancelled is the operator's own action, not a failure: neutral rail so the
   board does not read as "everything broke" when most jobs were stopped. */
.sop-jobcard--cancelled::before { background: var(--sop-line-2); }
.sop-jobcard--blocked::before, .sop-jobcard--abandoned::before { background: var(--sop-warn); }
.sop-jobcard__id { font-family: var(--sop-mono); font-size: 11.5px; color: var(--sop-text-1); letter-spacing: -0.01em; }
.sop-jobcard__meta { font-size: 11.5px; color: var(--sop-text-3); }
.sop-jobcard__summary { flex: none; white-space: nowrap; }
.sop-jobcard .sop-jobcard__meta.mono { flex: none; white-space: nowrap; }
/* "Already harvested": a quiet trailing glyph, not a badge. The card was
   designed as three fixed lines and a chip here added height or squeezed the
   id at the 300 px column width; a 15 px icon costs one gap on line 1 and
   the tooltip carries the folder it went to. Neutral on purpose: green or
   red would read as a verdict next to the state pill. */
.sop-jobcard__harvest { font-size: 15px; color: var(--sop-text-3); }
.sop-jobcard:hover .sop-jobcard__harvest { color: var(--sop-text-2); }
/* Actions stay at full opacity: "View" is the only way into a task, and a
   hover-gated entrance is invisible to touch and keyboard users. Secondary
   actions carry their own colour (folder = neutral, delete = red) instead of
   being dimmed, so nothing on the card depends on hover to be discoverable. */
.sop-jobcard__actions { opacity: 1; }
.sop-jobcard:hover { border-color: var(--sop-line-2); }
.sop-jobcard:focus-within { border-color: var(--sop-line-2); }

/* ── Row shrink discipline ───────────────────────────────────────────────
   In a single-line row only truncating elements may absorb width pressure.
   Badges, buttons and stamps keep their natural size: left shrinkable, they
   collapsed into one-word-per-line columns at card and rail widths. */
.pill, .chip, .sop-metric, .nav-item__badge { white-space: nowrap; }
.q-btn { flex-shrink: 0; }
.q-btn .q-btn__content { white-space: nowrap; }
.row > div:has(> .pill:only-child),
.row > div:has(> .chip:only-child) { flex: none; }

/* ── Metrics strip ─────────────────────────────────────────────────────── */

.sop-metrics { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; width: 100%; }
.sop-metric {
  display: inline-flex; align-items: baseline; gap: 6px;
  padding: 4px 12px; border-radius: 7px; background: var(--sop-surface-1);
  border: 1px solid var(--sop-line); font-size: 11px; color: var(--sop-text-3);
}
.sop-metric b { font-family: var(--sop-mono); font-size: 13px; color: var(--sop-text-1); font-weight: 700; }
.sop-metric--live { border-color: rgba(79, 140, 255, 0.32); }
.sop-metric--live b { color: var(--sop-accent-2); }
.sop-metric--ok b { color: var(--sop-ok); }
.sop-metric--warn { border-color: rgba(242, 185, 85, 0.32); }
.sop-metric--warn b { color: var(--sop-warn); }
.sop-metric--bad b { color: var(--sop-bad); }
/* Zero keeps its semantic border colour so position stays learnable; only the
   number recedes. */
.sop-metric--zero b { color: var(--sop-text-3); }

/* ── List rows (selection lists, detail rows) ──────────────────────────── */

.sop-list { width: 100%; display: flex; flex-direction: column; }
.sop-listrow {
  display: flex; align-items: center; gap: 10px; width: 100%;
  padding: 7px 10px; border-bottom: 1px solid var(--sop-line);
  border-left: 2px solid transparent; transition: background 0.12s ease;
}
.sop-listrow:hover { background: var(--sop-surface-2); }
.sop-listrow--on { background: var(--sop-accent-soft); border-left-color: var(--sop-accent); }
.sop-listrow__meta { font-size: 11.5px; color: var(--sop-text-3); }
.sop-kv { display: grid; grid-template-columns: 92px 1fr; gap: 3px 10px; width: 100%; align-items: baseline; }
.sop-kv dt { font-size: 11px; color: var(--sop-text-3); }
.sop-kv dd { font-size: 11.5px; color: var(--sop-text-2); margin: 0; overflow-wrap: anywhere; }

/* ── Log surface ───────────────────────────────────────────────────────── */

.log-box {
  background: #0b0f14;
  border: 1px solid var(--sop-line);
  border-radius: 8px;
  padding: 10px 12px;
  font-family: var(--sop-mono);
  font-size: 11.5px;
  line-height: 1.65;
  max-height: 580px;
  overflow-y: auto;
  white-space: pre-wrap;
  word-break: break-word;
  color: #c3cbd8;
}
.log-line { white-space: pre-wrap; word-break: break-word; }
/* Tail-strip rows for errors wrap instead of ellipsizing (see TailStrip). */
.sop-tail__wrap { white-space: pre-wrap; overflow-wrap: anywhere; }
.log-ts { color: #66748a; margin-right: 6px; }
.log-warn { color: var(--sop-warn); }
.log-error { color: var(--sop-bad); }
.log-success { color: var(--sop-ok); font-weight: 600; }
.log-tool { color: #5ad0d8; font-weight: 600; }
.log-supervisor { color: var(--sop-judge); }
.log-prompt { color: var(--sop-accent-2); }
.log-output { color: #c3cbd8; }
.log-blocked { color: var(--sop-warn); font-weight: 600; }

.sop-tail {
  width: 100%; background: #0b0f14; border: 1px solid var(--sop-line);
  border-radius: 8px; padding: 7px 10px; gap: 2px;
  font-family: var(--sop-mono); font-size: 11.5px; color: var(--sop-text-2);
}

/* ── Expansion panels (collapsible sections) ───────────────────────────── */

.q-expansion-item.sop-fold {
  width: 100%; background: var(--sop-surface-1);
  border: 1px solid var(--sop-line); border-radius: var(--sop-radius);
  box-shadow: var(--sop-shadow); overflow: hidden;
}
.sop-fold .q-expansion-item__container > .q-item {
  min-height: 56px; padding: 10px 13px;
  background: var(--sop-head-grad);
  border-bottom: 1px solid transparent;
}
.sop-fold.q-expansion-item--expanded .q-expansion-item__container > .q-item { border-bottom-color: var(--sop-line); }
.sop-fold .q-item__section--avatar { min-width: 34px; padding-right: 8px; }
.sop-fold .q-item__label {
  font-size: 11.5px; font-weight: 700; letter-spacing: 0.1em;
  text-transform: uppercase;
}
.sop-fold .q-item__label--caption {
  font-size: 11.5px; font-weight: 400; letter-spacing: 0;
  text-transform: none; margin-top: 1px;
}
.sop-fold .q-expansion-item__toggle-icon { color: var(--sop-text-3); }
.sop-fold .q-item:hover .q-expansion-item__toggle-icon { color: var(--sop-text-1); }
.sop-fold .q-expansion-item__content { padding: 15px 17px 17px; gap: 14px; }
.sop-fold .q-item__section--avatar .q-icon { color: var(--sop-accent-2); }
.sop-fold:hover { border-color: var(--sop-line-2); }
.sop-fold .nicegui-expansion__content { padding: 0; }

/* Compact filter strip inside a fold */
.sop-fold--dense .q-expansion-item__container > .q-item { min-height: 36px; padding: 5px 10px; }
.sop-fold--dense .q-item__label { font-size: 10.5px; letter-spacing: 0.1em; }

/* ── Progress / phase ──────────────────────────────────────────────────── */

.q-linear-progress { border-radius: 999px; background: var(--sop-surface-3); height: 5px; }
.q-linear-progress__track { background: var(--sop-surface-3); }
.q-linear-progress__model { background: var(--sop-accent); }

.phase-crumb { font-family: var(--sop-mono); letter-spacing: 0.3px; white-space: nowrap; }
/* Unreached phases stay quiet but must remain readable: #343c4a measured
   1.59:1, which made the "->" separators effectively invisible. */
.phase-done { color: var(--sop-text-3); text-decoration: line-through; text-decoration-color: #48505f; }
.phase-skipped { color: #5b667a; text-decoration: line-through dotted #48505f; }
.phase-todo { color: #5b667a; }
.phase-arrow { margin: 0 6px; }
.phase-arrow-lit { color: var(--sop-accent); opacity: 0.7; }
.phase-arrow-dim { color: #5b667a; }
.phase-loop::before, .phase-loop::after { color: #5b667a; }
.phase-loop::before { content: "["; margin-right: 5px; }
.phase-loop::after { content: "]"; margin-left: 5px; }
.phase-round { color: var(--sop-text-3); font-size: 10.5px; margin-left: 5px; vertical-align: 1px; }
.phase-current {
  display: inline-block; color: var(--sop-accent-2); font-weight: 600; line-height: 1.35;
  padding: 1px 8px 2px; border-radius: 5px;
  background: rgba(79, 140, 255, 0.14); border: 1px solid rgba(79, 140, 255, 0.34);
  animation: phase-enter 0.3s ease both, phase-breathe 2.6s ease-in-out 0.3s infinite;
}
.phase-current::after {
  content: ""; display: block; height: 2px; margin-top: 1px; border-radius: 1px;
  background: linear-gradient(90deg, transparent, var(--sop-accent) 50%, transparent) -100% 0 / 45% 100% no-repeat,
              rgba(79, 140, 255, 0.18);
  animation: phase-sheen 1.8s ease-in-out infinite;
}
.phase-current[class*="phase-end-"] { animation: none; }
.phase-current[class*="phase-end-"]::after { animation: none; background: currentColor; opacity: 0.45; }
.phase-end-success { color: var(--sop-ok); background: rgba(62, 207, 142, 0.14); border-color: rgba(62, 207, 142, 0.4); }
.phase-end-blocked { color: var(--sop-warn); background: rgba(242, 185, 85, 0.12); border-color: rgba(242, 185, 85, 0.4); }
.phase-end-failed { color: var(--sop-bad); background: rgba(255, 107, 107, 0.12); border-color: rgba(255, 107, 107, 0.4); }
@keyframes phase-enter { from { opacity: 0; transform: translateY(2px); } }
@keyframes phase-breathe {
  0%, 100% { box-shadow: 0 0 2px rgba(79, 140, 255, 0.12); }
  50% { box-shadow: 0 0 9px rgba(79, 140, 255, 0.32); }
}
@keyframes phase-sheen { from { background-position: -100% 0; } to { background-position: 200% 0; } }

/* ── Empty / blocked states ────────────────────────────────────────────── */

.sop-empty {
  display: flex; flex-direction: column; align-items: center; gap: 8px;
  padding: 40px 22px; text-align: center; width: 100%;
  border: 1px dashed var(--sop-line-2); border-radius: var(--sop-radius);
  background: #0d1218;
}
.sop-empty .q-icon { font-size: 34px; color: var(--sop-text-3); }
.sop-empty__title { font-size: 13px; font-weight: 650; color: var(--sop-text-2); }
.sop-empty__body { font-size: 12px; color: var(--sop-text-3); max-width: 520px; line-height: 1.6; }

/* ── Misc ──────────────────────────────────────────────────────────────── */

.sop-markdown .q-markdown { font-size: 13px; color: var(--sop-text-2); }
.q-markdown h1, .q-markdown h2, .q-markdown h3 { color: var(--sop-text-1); }
.q-markdown table { font-size: 12px; }
.q-markdown code { color: var(--sop-accent-2); background: var(--sop-surface-2); }
.sop-dialog { background: var(--sop-surface-1); border: 1px solid var(--sop-line-2); border-radius: 10px; }

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation: none !important; transition: none !important; }
  html { scroll-behavior: auto; }
}
"""

# ── Cascade layers ───────────────────────────────────────────────────────────
#
# The stylesheet used to be injected unlayered, which produced two opposite
# failures at once:
#   * normal declarations beat every Tailwind utility (5/5 conflicts measured),
#     so `.sop-meta text-xs` silently rendered at 11.5px;
#   * its own !important declarations LOST to Quasar's `quasar_importants`
#     layer, so `.q-btn--flat` never got its text colour and every button
#     rendered in the primary accent.
#
# Layer order fixes both, but a single layer cannot: CSS reverses the order for
# !important declarations, so "wins" means "last layer" for normal rules and
# "first layer" for !important ones.
#
# Writing our own `@layer a, b, c, ...` declaration does NOT help — NiceGUI has
# already declared the order, and a second declaration only appends unknown
# names to the END. So:
#   * !important rules go into `theme`, which NiceGUI's own declaration puts
#     FIRST (first layer wins for !important);
#   * normal rules go into `sop`, first mentioned here, so it is appended LAST
#     (last layer wins for normal declarations).
_IMPORTANT_RULES = """
.mono { font-family: var(--sop-mono) !important; }
.q-btn--outline { border-color: var(--sop-line-2) !important; color: var(--sop-text-1) !important; }
.q-btn--outline:hover { border-color: var(--sop-accent) !important; color: var(--sop-accent-2) !important; }
.q-btn--flat { color: var(--sop-text-2) !important; }
.q-btn--flat:hover { color: var(--sop-text-1) !important; }
/* Destructive icon: quiet at rest so the board's red stays meaningful, red
   the moment the pointer is on it. */
.sop-danger { color: var(--sop-text-3) !important; }
.sop-danger:hover { color: var(--sop-bad) !important; }
.q-btn--flat.text-red, .q-btn--flat.text-negative { color: var(--sop-bad) !important; }
.sop-fold .q-item__label { color: var(--sop-text-1) !important; }
.sop-fold .q-item__label--caption { color: var(--sop-text-3) !important; }
.nicegui-log .q-scrollarea__content { white-space: pre-wrap !important; overflow-wrap: anywhere; }
.nicegui-markdown pre, .nicegui-markdown code, .nicegui-code pre, .nicegui-code code { white-space: pre-wrap !important; overflow-wrap: anywhere; }
.q-btn.disabled, .q-btn[disabled], .q-btn--disabled { opacity: 0.62 !important; }

/* Quasar's toast/tooltip surfaces were never themed, so they rendered as
   solid brand colour with white text: white on the amber warning is 1.77:1,
   on red 2.78:1. Neutral surface + a semantic rail keeps both readable and
   still tells success from failure at a glance. */
.q-notification {
  background: var(--sop-surface-3) !important;
  color: var(--sop-text-1) !important;
  border: 1px solid var(--sop-line-2); border-left: 3px solid var(--sop-line-2);
  border-radius: 8px; font-size: 12.5px; box-shadow: var(--sop-shadow);
}
.q-notification__message { color: var(--sop-text-1) !important; }
.q-notification--positive { border-left-color: var(--sop-ok) !important; }
.q-notification--negative { border-left-color: var(--sop-bad) !important; }
.q-notification--warning { border-left-color: var(--sop-warn) !important; }
.q-notification--info { border-left-color: var(--sop-accent) !important; }
.q-notification .q-icon { color: var(--sop-text-2) !important; }
.q-tooltip {
  background: var(--sop-surface-3) !important;
  color: var(--sop-text-1) !important;
  border: 1px solid var(--sop-line-2); border-radius: 6px;
  font-size: 11.5px; max-width: 420px;
}
"""

# ── Type scale ───────────────────────────────────────────────────────────────
#
# One block, one layer, last: everything here is a *size* decision and must
# outrank the component rules above regardless of their specificity. It lives
# apart from _RULES so it can be re-stated wholesale (and so a future edit to a
# component cannot silently pin a 10.5px label back into the layout).
#
# The scale: body 15 / title 27 / section 17 / panel title 12.5 / group 11.5 /
# body copy 13 / meta 13 / micro 11.5. Nothing below 11.5px, which is the floor
# at which uppercase micro-labels stay legible on a 108 %-scaled laptop panel.

_TYPE_SCALE = """
body.body--dark { font-size: 16px; line-height: 1.55; }
.sop-kicker { font-size: 12.5px; }
.sop-h1 { font-size: 29px; }
.sop-sub { font-size: 15px; }
.sop-h2 { font-size: 18.5px; }
.sop-h2 .q-icon { font-size: 19px; }
.sop-section-head__count { font-size: 13px; }
.sop-panel__title { font-size: 13.5px; letter-spacing: 0.09em; }
.sop-panel__sub { font-size: 13.5px; }
.sop-panel__icon { width: 32px; height: 32px; font-size: 18.5px; }
.sop-group { font-size: 12.5px; letter-spacing: 0.11em; }
.sop-hint { font-size: 13.5px; }
.sop-meta { font-size: 14px; }
.sop-value { font-size: 15.5px; }

.q-field--standard .q-field__label { font-size: 12.5px; }
.q-field--standard .q-field__native,
.q-field--standard .q-field__input,
.q-field--standard .q-field__marginal { font-size: 15px; }
.q-field--standard .q-field__control { min-height: 46px; }
.q-field--standard.q-field--dense .q-field__control { min-height: 40px; }
.q-field__bottom { font-size: 13px; }
.q-item { font-size: 15px; min-height: 42px; }
.q-checkbox__label, .q-toggle__label { font-size: 14.5px; }

.q-btn { font-size: 14.5px; min-height: 42px; padding: 0 16px; }
.q-btn--dense { font-size: 13.5px; min-height: 36px; padding: 0 12px; }
.q-btn .q-icon { font-size: 19px; }

.pill { font-size: 12.5px; }
.chip { font-size: 13px; padding: 4px 10px; }
.sop-notice { font-size: 14.5px; padding: 12px 15px; }
.sop-notice .q-icon { font-size: 19px; }

.sop-step { padding: 12px 14px; }
.sop-step__num { width: 24px; height: 24px; font-size: 12.5px; }
.sop-step__label { font-size: 13.5px; }

.sop-brand__mark { font-size: 19px; }
.sop-brand__name { font-size: 15px; }
.sop-brand__sub { font-size: 12px; }
.sop-nav__caption { font-size: 11.5px; }
.nav-item { font-size: 14.5px; padding: 9px 11px; }
.nav-item .q-icon { font-size: 19px; }
.nav-item__badge { font-size: 12px; }
.sop-drawer__foot { font-size: 12.5px; }

.sop-header__where { font-size: 14px; }
.sop-header__where .q-icon { font-size: 18.5px; }
.sop-header__chip { font-size: 13px; }

.sop-jobcard { padding: 13px 15px 11px 17px; gap: 8px; }
.sop-jobcard__id { font-size: 13.5px; }
.sop-jobcard__meta { font-size: 13.5px; }

.sop-metric { font-size: 13px; padding: 5px 13px; }
.sop-metric b { font-size: 16px; }
.sop-listrow { padding: 9px 12px; }
.sop-listrow__meta { font-size: 13.5px; }
.sop-kv dt { font-size: 13px; }
.sop-kv dd { font-size: 13.5px; }

.log-box { font-size: 13.5px; line-height: 1.7; }
.sop-tail { font-size: 13.5px; }

.sop-fold .q-item__label { font-size: 13.5px; letter-spacing: 0.09em; }
.sop-fold .q-item__label--caption { font-size: 13.5px; }
.sop-fold--dense .q-item__label { font-size: 12.5px; }
.phase-round { font-size: 12.5px; }

.sop-empty { padding: 41px 23px; }
.sop-empty .q-icon { font-size: 37px; }
.sop-empty__title { font-size: 15.5px; }
.sop-empty__body { font-size: 14.5px; max-width: 640px; }
.sop-markdown .q-markdown { font-size: 15.5px; }
.q-markdown table { font-size: 14px; }

/* Components that still spell a label with a Tailwind utility follow the
   scale too, instead of pinning the old 12px. */
.text-xs { font-size: 13.5px; line-height: 1.5; }
.text-sm { font-size: 14.5px; line-height: 1.55; }
"""

CSS = (
    "@layer theme {\n"
    + _IMPORTANT_RULES
    + "\n}\n@layer sop {\n"
    + _RULES
    + "\n}\n"
    # Last layer: the type scale outranks every component size above.
    + "@layer sop-type {\n"
    + _TYPE_SCALE
    + "\n}\n"
)


def apply() -> None:
    """Install tokens and component CSS; set the Quasar palette.

    No web font is fetched: the stack below resolves to fonts already present
    on the operator's machine (Cascadia Mono on Windows, SF Mono on macOS).
    """
    ui.add_head_html(f"<style>{CSS}</style>")
    # The document shipped with an empty lang attribute, so a screen reader
    # guessed the language (and guessed wrong on mixed Chinese paths).
    ui.add_head_html('<script>document.documentElement.lang="en";</script>')
    ui.colors(
        primary="#3370e0",
        secondary="#232c3a",
        accent="#8fbaff",
        positive="#3ecf8e",
        warning="#f2b955",
        negative="#ff6b6b",
        dark="#131922",
    )
    ui.dark_mode(True)


# ── Widgets ──────────────────────────────────────────────────────────────────


def page_header(
    title: str,
    subtitle: str = "",
    *,
    kicker: str = "",
    actions: Callable[[], None] | None = None,
) -> None:
    """Page identity block: kicker, title, one-line purpose, right actions.

    The title is a real ``<h1>``: it used to be a ``<div>``, so the document
    had no heading outline at all and a screen reader had no way to jump
    between pages or sections.
    """
    with ui.column().classes("sop-pagehead w-full"):
        with ui.row().classes("w-full items-end gap-3 flex-wrap"):
            with ui.column().classes("gap-1"):
                if kicker:
                    ui.label(kicker).classes("sop-kicker")
                ui.html(f'<h1 class="sop-h1">{escape(title)}</h1>')
            ui.space()
            if actions is not None:
                with ui.row().classes("sop-h1-actions"):
                    actions()
        if subtitle:
            ui.label(subtitle).classes("sop-sub")


@contextlib.contextmanager
def panel(
    title: str = "",
    *,
    subtitle: str = "",
    icon: str = "",
    tone: str = "",
    actions: Callable[[], None] | None = None,
    head: Callable[[], None] | None = None,
    anchor: str = "",
) -> Iterator[ui.column]:
    """A surface with an optional header band; yields the body container.

    ``tone``: ``""`` (default), ``quiet`` (secondary content), ``warn``.
    ``head`` replaces the plain title label when a caller needs custom markup
    (e.g. a test-pinned literal) while keeping the standard header band.
    ``anchor`` gives the panel an id so in-page links can jump to it.
    """
    classes = "sop-panel"
    if tone:
        classes += f" sop-panel--{tone}"
    root = ui.column().classes(classes)
    if anchor:
        root.props(f'id="{anchor}"')
    with root:
        if head is not None or title or actions is not None:
            with ui.row().classes("sop-panel__head w-full items-center"):
                if icon:
                    with ui.element("div").classes("sop-panel__icon"):
                        ui.icon(icon)
                with ui.column().classes("sop-panel__titles"):
                    if head is not None:
                        head()
                    else:
                        if title:
                            # h2, not a div: panel titles are the document's
                            # second level and must appear in the outline.
                            ui.html(
                                f'<h2 class="sop-panel__title">{escape(title)}</h2>',
                            )
                        if subtitle:
                            ui.label(subtitle).classes("sop-panel__sub")
                if actions is not None:
                    ui.space()
                    with ui.row().classes("items-center gap-2"):
                        actions()
        body = ui.column().classes("sop-panel__body")
        with body:
            yield body


def outline_button(
    text: str = "",
    *,
    icon: str = "",
    on_click: Callable[[], None] | None = None,
    dense: bool = True,
) -> ui.button:
    """Outline button that carries no Quasar colour utility.

    ``color=primary`` makes Quasar emit ``.text-primary !important``, which
    used to beat every rule this file declared — that is why outline and flat
    buttons all rendered in the accent and "View" looked identical to "delete".
    ``color=none`` keeps the palette out of the markup so @layer sop-important
    owns the colour.
    """
    button = ui.button(text, icon=icon or None, on_click=on_click)
    button.props("outline color=none" + (" dense" if dense else ""))
    return button


def group_label(text: str) -> None:
    """A sub-section marker inside a panel body (rule to the right)."""
    ui.label(text).classes("sop-group")


def hint(text: str) -> None:
    ui.label(text).classes("sop-hint")


def notice(
    kind: str,
    text: str,
    *,
    icon: str = "",
    action: Callable[[], None] | None = None,
) -> ui.row:
    """Inline status strip; *kind* is info / ok / warn / bad.

    Announced to assistive tech: these strips carry the app's most important
    asynchronous news ("launch blocked", "run failed"), and without a live
    region none of it reaches a screen reader.
    """
    icons = {"info": "info", "ok": "check_circle", "warn": "warning", "bad": "error"}
    role = 'role=alert aria-live=assertive' if kind == "bad" else 'role=status aria-live=polite'
    with ui.row().classes(
        f"sop-notice sop-notice--{kind} items-start flex-wrap",
    ).props(role) as row:
        ui.icon(icon or icons.get(kind, "info"))
        ui.label(text).classes("sop-notice__text grow")
        if action is not None:
            action()
    return row


def empty_state(
    icon: str,
    title: str,
    body: str,
    *,
    detail: str = "",
    action_label: str = "",
    action: Callable[[], None] | None = None,
) -> None:
    """A blocked/dead-end surface: what is wrong, what it looked like, next step.

    ``detail`` carries the offending value (a path, an id) on its own mono line
    so it can be read and copied without parsing a sentence.
    """
    with ui.column().classes("sop-empty"):
        ui.icon(icon)
        ui.label(title).classes("sop-empty__title")
        ui.label(body).classes("sop-empty__body")
        if detail:
            ui.label(detail).classes("mono text-xs").style(
                "color:var(--sop-text-3);background:var(--sop-surface-2);"
                "border:1px solid var(--sop-line);border-radius:6px;"
                "padding:3px 10px;max-width:100%;overflow-wrap:anywhere",
            )
        if action is not None and action_label:
            ui.button(action_label, on_click=action).props("outline dense")


@contextlib.contextmanager
def fold(
    key: str,
    title: str,
    *,
    caption: str = "",
    icon: str = "",
    default: bool = False,
    anchor: str = "",
) -> Iterator[ui.expansion]:
    """Collapsible section whose open state survives rebuilds and restarts."""
    from services.webui.ui_state import state_expansion

    exp = state_expansion(
        key, title, caption=caption, icon=icon or None, default=default,
    ).classes("sop-fold")
    if anchor:
        exp.props(f'id="{anchor}"')
    with exp:
        yield exp


__all__ = [
    "CSS",
    "apply",
    "empty_state",
    "fold",
    "group_label",
    "hint",
    "notice",
    "outline_button",
    "page_header",
    "panel",
]
