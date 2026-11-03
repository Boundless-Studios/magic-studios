# Roadmap

What the studio should do next, and why. Each item names its source so the
priority can be re-argued when the evidence changes. Delivered work is listed at
the bottom so the same request is not filed twice.

## Sources

- **Parity audit, 2026-09-01.** The first customer's existing animation
  tooling set the bar for what this studio needs to replace
  (`docs/superpowers/plans/2026-09-01-studio-capability-parity.md`).
- **Community, 2026-09-02.** The r/aigamedev launch thread for AutoSprite, a
  competing one-image-to-animation-set tool: 85 comments from working
  developers, several of them paying customers of that tool. Their asks and
  complaints are the clearest public statement of what this category has not
  solved. Items below marked *community* come from that thread.
- **GPU-owner feedback, 2026-10-20.** Review from the operator of the 16GB
  desktop optimization. Items marked *GPU feedback*. Its two first priorities,
  a homepage that shows real output before sign-in and validating the
  onboarding templates, are addressed in
  [onboarding template validation](plans/onboarding-template-validation.md).

## Now

1. **Motion quality that survives the play button.** The single loudest
   complaint in the category: sheets look fine as stills and "floaty, jittery,
   identical frames" in motion. *Community.* We already gate on duplicate
   terminal frames and loop seams; extend the motion policy with
   frozen-frame and near-duplicate detection, velocity discontinuity across the
   loop point, and a per-motion motion score surfaced before signoff. Judge
   motion as motion: the review artifact is a GIF, never a strip.
2. **Consistent character scale across a set.** Paying users report the run
   sheet's character coming out a different size than the walk sheet and
   hand-rescaling every sheet. *Community.* The set check already flags canvas
   and baseline mismatch; add automatic subject-scale normalization across
   selected candidates, driven by the shared opening pose, so the fix is one
   click rather than a slider guessed before generation.
3. **Transitions between states.** Idle does not line up with how attack
   starts; walks stop sharply. *Community; also in the product spec.* Generate
   transition clips from the end pose of one motion to the start pose of
   another, and export them as first-class motions with `from`/`to` semantics.
4. **Preset library beyond fantasy melee.** Pistol and rifle attacks with a
   custom-weapon field, interaction presets (open chest, pull switch, dive),
   and non-humanoid subjects (vehicles). *Community.* Presets are data in
   `presets.py`; the missing piece is a curated second tier plus the topology
   contract the spec already reserves.

## P1 follow-ups from GPU feedback

- **Generate other directions from one image.** Directional templates ask for
  one uploaded image per direction; Ludo, Scenario and Pixa generate them.
  *GPU feedback; competitor research.* This reverses the supplied-view-only
  rule in [character-view sprite production](plans/character-view-sprite-production.md),
  so it needs an accepted design first. Start from the Flux2 Klein subject
  path, which already renders the same character from a reference, and keep a
  generated view a draft until a person approves it as that view's reference.
  Proposed design and spike: [direction views from one image](design/direction-views-from-one-image.md)
  (accepted 2026-10-22: a turnaround video through the existing motion path,
  frames picked per heading, and an optional standing pass).
- **A studio that feels like AutoSprite before launch.** *GPU feedback.* Adopt
  the flow and density (gallery first, one image to a set on one page), not
  its visual identity. Which parts the reviewer means is still open.
- **Clip discovery for longer takes.** *GPU feedback:* finding full-length
  clips is weaker than sprite motion creation. Scope unclear: choosing among
  takes, or the movie flow. Clarify before planning.

Then: **benchmark the standard 24GB route.** *GPU feedback.* It runs INT8 H3
at 20 steps with no turbo adapter, so it likely leaves speed and quality
unused. Try the 16GB methods (LightX2V 8-step, the adapter's native 768p,
Sage/Kitchen attention) and a lighter quantization than W4A8, with image,
prompt, seed, resolution and frame count held constant. Needs a 24GB card;
ship any winner as a new versioned profile, never by changing the existing route.

## Next

5. **Public API and an MCP server.** Agent-driven users want their coding agent
   to generate sheets without the UI; one paying user churned over the manual
   interface being "too slow". *Community.* The typed routes exist; ship an
   OpenAPI-documented client and an MCP server that exposes create project,
   add motions, generate, release, export, and delivery status.
6. **Combined atlas export.** One sheet for a whole animation set instead of
   one per motion, with the manifest carrying per-motion regions. *Community.*
   An export profile, not a new pipeline.
7. **Pixel-art profile.** "True pixel art" means respecting a pixel grid, not
   emulating the look; current AI output has fractional pixels. *Community.*
   A generation and derivation profile that quantizes to a declared grid and
   palette, with a quality check that fails fractional pixels.
8. **Directional sets.** 4- and 8-direction and isometric variants as a set
   dimension, exported with direction semantics. *Community.*
9. **Frame budgets by default.** A 50-frame idle is "ridiculously wasteful";
   users want 4 to 8 frames. *Community.* Resample and cell size exist; make
   the preset carry a default frame budget and show the cost before generating.

## Later

10. **Layered and modular sprites.** Paperdoll layers (body, head, hair,
    clothing) and Aseprite files with layers intact, so a character creator
    can be built on top. *Community.* Needs a layered subject contract first.
11. **Playable browser preview.** Test idle, movement, jump, and attack as a
    controller in the browser before export. *Product spec; community praise
    for the competitor's version.*
12. **Props, items, tilesets, and HUD.** Adjacent asset types users ask the
    same tool for. Out of the core promise; revisit once the character set is
    excellent.

## Pricing and onboarding signals

Not features, but recorded because they decided purchases in the thread: let
people see quality before paying (a first generation must be free or cheap),
show the credit cost of an action before it runs, offer team seats, and ship
short tutorials for the non-obvious controls. *Community.*

## Delivered

- Ten motion presets with authored prompts and a recommended set; one-action
  set generation with per-motion failure isolation (Slice 1).
- Shared opening pose with an approval gate before video spend (Slice 4).
- Frame editor: reorder, holds, flip, duration and FPS, resample to 4/8/16/32
  frames at 256/512/1024 px cells (Slices 2 and 5).
- Alpha key-out as a deterministic repair; per-motion PNG, WebM, and GIF
  downloads; ingest of existing clips and sheets (Slice 5).
- Reference images with start/context/end roles, style, and script per motion
  (Slice 4). The competitor's "storyboard" feature is this.
- Target adapter interface with the first customer's game as the reference
  adapter; deep-link binding and automatic release delivery
  (`docs/superpowers/specs/2026-09-02-target-adapter-interface.md`).
