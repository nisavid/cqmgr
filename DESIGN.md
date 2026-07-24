<!-- SEED: re-run $impeccable document once there is implemented UI to capture actual tokens and components. -->
---
name: Cloud Quota Manager
description: The Mineral Instrument for exact accelerator quota and obtainability operations.
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
tones rather than generic black; light surfaces use pale stone without becoming
paper-beige. A capable TUI may suggest texture, luster, and facets through
careful color and symbols, while CLI output projects the same roles through flat
terminal colors, words, weight, and spacing. Neither surface pretends to render
materials the terminal cannot support.

Peter Zumthor's Therme Vals guides stone composition, Grand Seiko Zaratsu
finishing guides brushed-versus-polished metal contrast, and Smithsonian gem
photography guides believable color depth and facets. The system explicitly
rejects both the Diablo Immortal inventory aesthetic and a provider-console
mimic: no loot-like jewel overload, ornate dark frames, constant glow,
game-status spectacle, sprawling undifferentiated tables, or request flows that
hide the exact quota slice and its consequences.

**Key Characteristics:**

- Structural stone, precise metal, vivid gemstone signals
- Integrated workbench panes rather than floating card grids
- Dense operational clarity with exact evidence close at hand
- Material realism scaled to the rendering surface
- Ornament reserved for meaning, state, and control

## Colors

The palette is organized by material role rather than arbitrary hue ramps. All
exact values and tonal ramps remain **[to be resolved during implementation]**.

### Primary

- **Dark Rock:** Slate, basalt, and obsidian tones form dark-mode backgrounds,
  workbenches, and integrated panes.
- **Light Rock:** Shale, granite, feldspar, and marble tones form light-mode
  backgrounds and surfaces without defaulting to cream, sand, or parchment.

### Secondary

- **Structural Metal:** Titanium, steel, and aluminum carry full-length
  boundaries, controls, frames, and durable separators with flat, brushed, or
  rough-polished character.
- **Luminous Metal:** Mercury, tin, silver, and gold carry rare emphasis, fine
  content separators, and special surfaces with varied realistic luster.

### Tertiary

- **Gem Signals:** Sapphire, ruby, topaz, tourmaline, emerald, and tanzanite
  provide vivid semantic lights, bullets, notifications, and compact status
  marks. Exact semantic assignments follow the implemented lifecycle and
  severity vocabulary rather than color convention alone.

### Neutral

- **Stone Text:** High-contrast mineral neutrals carry guidance and long-form
  text without washed-out gray.
- **Metal Text:** Metallic neutrals distinguish exact identities, controls,
  borders, outlines, and measured evidence.

**The Material Role Rule.** Rock structures, metal defines, and gemstones
signal. Never exchange those roles merely for visual variety.

**The Believable Matter Rule.** Material effects must reflect plausible depth,
facet, polish, and luster. Stylized noise, glassmorphism, and decorative texture
are forbidden.

**The Rare Light Rule.** A gemstone glows only for an active state, attention,
or notification. Every light has an authoritative word or symbol equivalent.

**The Separator Rule.** Content separators may be thin silver or gold and
feather at their ends. Structural separators are thicker, flatter metals and
run the full required length.

## Typography

**Display Font:** Calm humanist sans **[font to be chosen during implementation]**

**Body Font:** Calm humanist sans **[font to be chosen during implementation]**

**Label/Mono Font:** User-owned terminal monospace for CLI/TUI

**Character:** Guidance is composed and immediately readable. Commands,
revisions, quota identities, metrics, and structured evidence remain
recognizably monospaced. The user's terminal font is authoritative; Cloud Quota
Manager relies on weight, spacing, alignment, symbols, and color rather than a
bundled typeface.

### Hierarchy

- **Display:** Reserved for future brand or onboarding surfaces, never routine
  product controls.
- **Headline:** Names the current operator task without oversized drama.
- **Title:** Identifies the selected resource, quota slice, or evidence surface.
- **Body:** Explains state and next actions in lines no longer than 65-75
  characters where the terminal layout permits.
- **Label:** Carries controls, table headings, compact metadata, and state words
  without tiny uppercase tracking.

**The Terminal Ownership Rule.** Nerd-font glyphs may enrich a capable terminal
but can never be required for identity, navigation, state, or action.

**The Evidence Type Rule.** Monospace distinguishes exact evidence; it does not
make all prose feel like a daemon log.

## Elevation

Cloud Quota Manager is flat and integrated at rest. Rock tone changes and
structural metal boundaries establish depth; panes are worked into their
containing instrument. Shadows are not a default material. Polished-metal
luster and gemstone depth may add local dimensionality on capable surfaces,
but never turn every control into a floating object.

Motion is responsive state feedback only: approximately 150-200 ms for focus,
selection, progress, structural change, and active gem light. There are no
orchestrated screen entrances. Reduced-motion settings replace transitions with
an immediate change or restrained crossfade.

**The Integrated Panel Rule.** A pane belongs to its surrounding instrument; it
does not float above it merely to manufacture hierarchy.

**The Earned Glow Rule.** Glow is local, subtle, and caused by a live semantic
state. Ambient decorative bloom is forbidden.

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
