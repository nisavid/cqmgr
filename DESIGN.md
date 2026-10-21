---
name: Cloud Quota Manager
description: The Mineral Instrument for exact accelerator quota and obtainability operations.
colors:
  dark-rock: "#15191d"
  instrument-rock: "#22282e"
  navigation-rock: "#1b2025"
  panel-rock: "#1d2328"
  status-rock: "#20262b"
  evidence-rock: "#232b31"
  control-metal: "#2a3138"
  blue-steel-selection: "#334c63"
  bright-metal: "#89939c"
  structural-metal: "#67727b"
  fine-metal: "#4d5963"
  stone-text: "#eef1f3"
  selected-text: "#ffffff"
  evidence-text: "#d9e7f2"
  status-text: "#dce2e7"
  coverage-text: "#cbd3da"
  technical-text: "#b8c4cc"
typography:
  body:
    fontFamily: monospace
  active-label:
    fontFamily: monospace
    fontWeight: 700
components:
  workspace-tab:
    backgroundColor: "{colors.control-metal}"
    textColor: "{colors.stone-text}"
    typography: "{typography.body}"
  workspace-tab-active:
    backgroundColor: "{colors.blue-steel-selection}"
    textColor: "{colors.selected-text}"
    typography: "{typography.active-label}"
  instrument-bar:
    backgroundColor: "{colors.instrument-rock}"
    textColor: "{colors.stone-text}"
  status-line:
    backgroundColor: "{colors.status-rock}"
    textColor: "{colors.status-text}"
  integrated-pane:
    backgroundColor: "{colors.panel-rock}"
    textColor: "{colors.stone-text}"
  copy-cli:
    backgroundColor: "{colors.evidence-rock}"
    textColor: "{colors.evidence-text}"
    typography: "{typography.body}"
---

# Design System: Cloud Quota Manager

## Overview

**Creative North Star: "The Mineral Instrument"**

Cloud Quota Manager is a precise operational instrument composed from
believable natural materials. Rock carries structure, metal carries boundaries
and control, and gemstones carry compact live signals. The material language
is timeless and functional: it should feel carefully made, not themed.

The primary user works in a terminal beside an editor or cloud tool, often for
long sessions in subdued ambient light. Dark surfaces therefore use quiet rock
tones rather than generic black; light surfaces, when implemented, must use
pale stone without becoming paper-beige. The implemented Textual TUI expresses
the world through flat dark tones, integrated panes, metal-colored boundaries,
and a restrained blue-steel selection. CLI output preserves meaning through
words, alignment, spacing, and optional terminal color. Neither surface
pretends to render materials the terminal cannot support.

Peter Zumthor's Therme Vals guides stone composition, Grand Seiko Zaratsu
finishing guides brushed-versus-polished metal contrast, and Smithsonian gem
photography guides believable color depth and facets. The system explicitly
rejects both the Diablo Immortal inventory aesthetic and a provider-console
mimic: no loot-like jewel overload, ornate dark frames, constant glow,
game-status spectacle, sprawling undifferentiated tables, or request flows that
hide the exact quota slice and its consequences.

This record replaces the pre-implementation seed with the current terminal
projection. The frontmatter records explicit color assignments from
`src/cqmgr/adapters/tui/app.py` and terminal typography semantics. It is not a
browser theme or a claim that every part of the material direction is built.
Light-mode values, humanist fonts for future non-terminal surfaces, a complete
gem-signal palette, texture, luster, and custom motion remain unresolved or
unimplemented. The sidecar contains static documentation samples, not a web
implementation of the product.

**Key Characteristics:**

- Structural stone, precise metal, vivid gemstone signals
- Integrated workbench panes rather than floating card grids
- Dense operational clarity with exact evidence close at hand
- Material realism scaled to the rendering surface
- Ornament reserved for meaning, state, and control

## Colors

The implemented palette is a dark terminal projection of material roles. The
frontmatter is normative for extracted color values; descriptive names below
explain their use. The sidecar's synthesized tonal ramps are display swatches,
not implemented runtime scales or additional approved semantic colors.

### Primary

- **Blue Steel Selection** (`blue-steel-selection`): the active workspace
  control, paired with Selected Text and bold type. This muted selection color
  does not establish a complete gemstone status mapping.

### Secondary

- **Control Metal** (`control-metal`): inactive workspace controls.
- **Bright Metal** (`bright-metal`): the instrument bar's lower boundary and
  the bound-scope boundary in consequential routes.
- **Structural Metal** (`structural-metal`): full pane boundaries and durable
  separators around technical, audit, and obtainability evidence.
- **Fine Metal** (`fine-metal`): quieter navigation and status dividers.

Structural metal retains the confirmed titanium, steel, and aluminum character.
Luminous mercury, tin, silver, and gold remain the direction for rare emphasis,
fine content separators, and special surfaces; realistic luster is not yet an
implemented token family.

### Tertiary

**Gem Signals** remain the confirmed direction for vivid semantic lights,
bullets, notifications, and compact status marks: sapphire, ruby, topaz,
tourmaline, emerald, and tanzanite. Exact lifecycle and severity assignments are
not yet established by the project stylesheet. Do not invent them from color
convention or treat the workspace-selection color as provider status.

### Neutral

- **Dark Rock** (`dark-rock`): the containing terminal screen and ledger ground.
- **Instrument Rock** (`instrument-rock`): the persistent instrument bar.
- **Navigation Rock** (`navigation-rock`): the workspace-navigation strip.
- **Panel Rock** (`panel-rock`): integrated filter, detail, obtainability, and
  lifecycle surfaces.
- **Status Rock** (`status-rock`): the operation-status strip.
- **Evidence Rock** (`evidence-rock`): Copy CLI evidence surfaces.
- **Stone Text** (`stone-text`): the screen's default high-contrast text.
- **Selected Text** (`selected-text`): active workspace labels.
- **Evidence Text** (`evidence-text`): exact commands and bound-scope evidence.
- **Status Text** (`status-text`): authoritative operation-status words.
- **Coverage Text** (`coverage-text`): source completeness and coverage.
- **Technical Text** (`technical-text`): supporting technical evidence.

Dark rock retains the slate, basalt, and obsidian direction. Future light rock
must draw from shale, granite, feldspar, and marble rather than cream, sand, or
parchment. Stone text carries guidance; metal text distinguishes exact
identities, controls, outlines, and measured evidence. No light palette is
extracted here.

**The Material Role Rule.** Rock structures, metal defines, and gemstones
signal. Never exchange those roles merely for visual variety.

**The Believable Matter Rule.** Material effects must reflect plausible depth,
facet, polish, and luster. Stylized noise, glassmorphism, and decorative texture
are forbidden.

**The Rare Light Rule.** A gemstone glows only for an active state, attention,
or notification. Every light has an authoritative word or symbol equivalent.

**The Separator Rule.** Content separators may be thin silver or gold and
feather at their ends. Structural separators are thicker, flatter metals and
run the full required length. The current terminal implementation uses solid
cell boundaries; feathering is direction, not an implemented effect.

## Typography

**Body and Label Font:** User-owned terminal monospace for CLI/TUI.

**Display Font:** Calm humanist sans for future non-terminal brand or onboarding
surfaces **[not chosen or implemented]**.

**Non-terminal Body Font:** Calm humanist sans **[not chosen or implemented]**.

**Character:** Guidance is composed and immediately readable. Commands,
revisions, quota identities, metrics, and structured evidence remain
recognizably monospaced. The user's terminal font is authoritative; Cloud Quota
Manager relies on weight, spacing, alignment, symbols, and color rather than a
bundled typeface. Frontmatter `monospace` expresses that role, not a shipped font
or a browser stylesheet. No project font-size, line-height, or tracking scale
has been extracted. The active workspace explicitly uses bold text.

### Hierarchy

- **Display:** Reserved for future brand or onboarding surfaces, never routine
  product controls.
- **Headline:** Names the current operator task without oversized drama.
- **Title:** Identifies the selected resource, quota slice, or evidence surface.
- **Body:** Explains state and next actions; the confirmed direction is lines
  no longer than 65-75 characters where the terminal layout permits.
- **Label:** Carries controls, table headings, compact metadata, and state words
  without tiny uppercase tracking. Active workspace labels use bold weight.

**The Terminal Ownership Rule.** Nerd-font glyphs may enrich a capable terminal
but can never be required for identity, navigation, state, or action.

**The Evidence Type Rule.** Monospace distinguishes exact evidence; it does not
make all prose feel like a daemon log.

## Layout

The spatial grammar is an integrated instrument with persistent context above
the working body. The current shell has an instrument bar, workspace
navigation, and status strip, each three terminal rows high. Workspaces occupy
the remaining height. A supporting pane belongs to its containing workbench
rather than becoming a floating card.

The implemented quota workbench changes at terminal-column thresholds:

- **Wide (120 columns and above):** scope/filter rail, quota ledger, and detail
  pane share the body. The rail is 30 columns wide; the ledger and detail use a
  2:1 flexible width relationship, subject to their minimum widths.
- **Medium (80-119 columns):** the rail is hidden while ledger and detail remain.
- **Narrow (below 80 columns):** the ledger and detail become separate one-pane
  routes; selection and return focus survive the transition. The obtainability
  form and results stack vertically.

Spacing is measured in terminal rows and columns, not browser pixels. Current
panes commonly use one cell of inset; the lifecycle route uses one row and two
columns. No CSS-pixel spacing or radius scale is implied by these measurements.
The sidecar retains the actual column thresholds as terminal metadata.

## Elevation & Depth

Cloud Quota Manager is flat and integrated at rest. Rock tone changes and
structural metal boundaries establish depth; panes are worked into their
containing instrument. The project stylesheet defines no custom shadows.
Polished-metal luster and gemstone depth remain permitted local material
direction on capable surfaces, but never turn every control into a floating
object.

The confirmed motion direction is responsive state feedback only: approximately
150-200 ms for focus, selection, progress, structural change, and active gem
light, with no orchestrated screen entrances. These are directional targets,
not extracted timing tokens: the project stylesheet defines no custom motion
system. When implemented, reduced-motion settings must replace transitions
with an immediate change or restrained crossfade. Textual's inherited widget
behavior is not a project-authored motion specification.

**The Integrated Panel Rule.** A pane belongs to its surrounding instrument; it
does not float above it merely to manufacture hierarchy.

**The Earned Glow Rule.** Glow is local, subtle, and caused by a live semantic
state. Ambient decorative bloom is forbidden.

## Shapes

The current terminal projection uses rectangular regions, straight aligned
edges, and solid full-length structural boundaries. Workspace tabs remove the
default border; other widgets retain Textual's native rendering unless the
project overrides it. There is no project-owned rounded-corner scale. Do not
translate terminal cells into invented pixel radii or decorative browser chrome.

## Components

The assignments below describe the implemented Textual shell. Shared widget
behavior comes from Textual unless an override is named. The sidecar's static
HTML/CSS samples illustrate extracted colors and terminal content; they are
not interactive replicas or a new source of production tokens.

### Buttons

Workspace tabs are compact rectangular controls. Inactive tabs use Control
Metal and Stone Text; the selected tab uses Blue Steel Selection, Selected
Text, and bold type. They have no project border, a minimum width of 16 terminal
columns, and a one-column gap. Other buttons inherit Textual variants. The
project does not define a separate hover/focus palette for extraction; preserve
native visible focus and disabled behavior.

### Inputs / Fields

Fields are ordinary keyboard-operable Textual inputs, selects, and checkboxes.
Lifecycle inputs have a one-row bottom gap. Their border, focus, error, and
disabled styling is inherited rather than a new brand token family. Keep
labels and explicit state words; the documentation sample shows field content
without inventing a browser input skin.

### Navigation

The navigation strip uses Navigation Rock, a Fine Metal lower divider, and
labeled workspace tabs. Selected appearance follows the button assignment.
Keyboard movement, help, and return controls remain understandable without
color or optional glyphs.

### Integrated Panes

Filter, detail, obtainability, and lifecycle surfaces use Panel Rock with
Structural Metal boundaries where they divide adjacent regions. The ledger
shares the containing ground. Tone and aligned separators establish hierarchy;
there is no floating-card or wide-shadow treatment.

### Instrument Bar

The persistent context surface uses Instrument Rock, Stone Text, a Bright Metal
lower boundary, and one-column side insets. Scope, identity evidence, and
completeness remain authoritative text. A locked scope is stated explicitly.

### Status and Coverage

The status strip uses Status Rock, Status Text, and a Fine Metal lower divider.
Coverage and technical detail use their distinct neutral text roles. Complete,
incomplete, failed, unknown, and unsupported conditions retain explicit words;
a colored light must never carry the only meaning.

### Copy CLI

Exact command evidence uses Evidence Rock and Evidence Text with one-column
side insets. Its constrained-height presentation belongs to the current
workspace or route. Keep commands selectable and readable without ornament or
secret-bearing examples.

## Do's and Don'ts

### Do:

- **Do** use rock for structural surfaces, metal for boundaries and controls,
  and gemstones for compact semantic signals.
- **Do** use rows, tables, panes, and integrated panels for ordinary product
  structure; reserve card-like treatment for self-contained portable artifacts.
- **Do** keep exact quota identity, authenticated principal, provider evidence,
  warnings, and planned mutation consequences visible before confirmation.
- **Do** adapt material realism to the rendering surface while preserving the
  same semantic roles in CLI, TUI, and structured output.
- **Do** pair every color, icon, emoji, or optional Nerd-font glyph with
  authoritative language or an accessible label.
- **Do** preserve terminal-owned typography, native keyboard behavior, and
  meaningful no-color output when extending the implemented projection.

### Don't:

- **Don't** build a provider-console mimic: a sprawling quota table or request
  wizard that hides exact slice identity, companion constraints,
  desired-versus-effective state, and mutation consequences.
- **Don't** build a raw API console that exposes provider trivia instead of
  operator intent, authoritative evidence, and valid next actions.
- **Don't** use generic dashboard card grids, decorative terminal nostalgia,
  neon cyberpunk, gratuitous animation, or unfamiliar controls invented for
  visual novelty.
- **Don't** imitate Diablo Immortal inventory UI with loot-like jewel overload,
  ornate dark frames, constant glow, or game-status spectacle.
- **Don't** use cream, sand, parchment, or beige as the reflexive light-mode
  background; light stone must remain visibly mineral and product-specific.
- **Don't** use colored side stripes, gradient text, glassmorphism, nested
  cards, or border-plus-wide-shadow ghost cards.
- **Don't** promote documentation swatches, browser preview mechanics, or
  unresolved material effects into implemented product tokens.
