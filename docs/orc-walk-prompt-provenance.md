# Orc Blood Berserker: illustration and motion prompt provenance

Research snapshot: October 16, 2026. This note records source text and a proposed server policy; it does not implement changes or report a new render evaluation. Cross-repository links require the sibling `under-fire` and `under-fire-assets` checkouts. Line references below are plain text because local Markdown viewers do not consistently support source line anchors.

## Current implementation note (October 17)

The source audit below describes the pre-feature compiler. It is historical evidence,
not the current interface. The checked-in character-view implementation now resolves
supplied view references and explicit screen-space walk headings on the server,
with reciprocal stance/recovery, fixed-root and projection-preservation guidance.
See [the accepted plan](plans/character-view-sprite-production.md). This closes the
prompt-contract gap; it does not establish reliable generation or temporal quality.
The isolated integration source still turned despite its earlier reference constraints,
and no temporal evaluator qualified. The original provenance and dispatch text below
remain unchanged for comparison.

## What is established

The recovered illustration prompt is an eight-facing **idle turnaround**, not a walk or attack prompt. The pack explicitly says animation follows the idle art. The art ledger identifies five animation references cut from `art_inbox/05_units_orc/blood_berserker.png`. The curated archive says that named original image was not found, so the recovered prompt and documented filename are a provenance trail, not a hash-verified binding to the unavailable original illustration. The available five-view reference and later export records remain separate evidence. [Prompt pack README, lines 3–7](../../under-fire/docs/art/codex_pack_orc/README.md); [art ledger, lines 2412–2462](../../under-fire/docs/art/ART_LEDGER.md); [curated archive README](../../under-fire-assets/units/orc-blood-berserker/README.md).

Orc Hero uses `art: "blood_berserker"`; this is shared artwork rather than evidence of a separate hero illustration prompt. [Hero definitions, lines 24–25](../../under-fire/game/data/heroes.json).

## Original still illustration prompt

Exact text for `blood_berserker.png` at 1536×1024, in [the generated unit prompt pack, lines 116–118](../../under-fire/docs/art/codex_pack_orc/05_units_orc.md):

> 8-direction turnaround sprite sheet: a 4-column x 2-row grid of equal cells, the SAME character in the SAME idle standing pose, SAME size and SAME ground line in every cell, one figure per cell, no grid lines drawn. Cell order (left-to-right, top row then bottom row): 1 facing the viewer (toward screen bottom); 2 facing screen lower-left; 3 facing screen left (profile); 4 facing screen upper-left (back three-quarter); 5 facing away from the viewer (toward screen top, back view); 6 facing screen upper-right (back three-quarter); 7 facing screen right (profile); 8 facing screen lower-right. Identical costume, colours, proportions and equipment in all eight cells. Subject: a Blood Berserker — a towering scarred orc in a horned skull helm and spiked iron shoulder plates, twin serrated axes, war paint in broad stripes. Team colour: paint every team-colour area (banners, sashes, tabards, cloth trim, shield faces, roof pennants) as FLAT PURE MAGENTA #FF00FF with only light shading — magenta must appear nowhere else. Keep team-colour areas clearly visible but no more than about 15% of the subject. Premium hand-painted fantasy RTS game asset in the spirit of Age of Empires II: Definitive Edition and Warcraft III Reforged: rich painterly textures, crisp readable silhouette, warm key light from the UPPER LEFT, soft ambient occlusion, saturated but natural colours, fine detail that still reads when shrunk to a small sprite. Camera: high three-quarter top-down RTS view looking down about 35 degrees, orthographic (no perspective distortion, no fisheye). ONE isolated subject, centred, generous empty margin on all sides, nothing cropped. Background: perfectly flat uniform chroma green #00B140 — no ground plane, no grass, no cast shadow on the background, no vignette, no text, no labels, no border, no watermark.


The generator corroborates its assembly: `FACINGS`, subject description, `TEAM`, then `STYLE`. The subject is distinct from the adjacent ordinary Berserker, whose prompt describes a two-handed cleaver rather than twin axes. [Generator constants, lines 21–44; subjects, lines 187–190; assembly, lines 581–611](../../under-fire/tools/make_codex_prompt_pack.py).

## Selected legacy walk directives

These are verbatim `motions[].direction` values for the selected `walk` entries in five final-source manifests. They are persisted export directives, **not standalone receipts of the exact provider request**. Facing names below come from the export folders; the manifests' generic `facing` field should not be substituted for these authored view descriptions. Each heading links to its primary manifest.

### [N](../../under-fire-assets/units/orc-blood-berserker/03-final-source-exports/N/manifest.json)

> Static camera that does not move at all: no zoom in, no zoom out. The same armoured orc berserker from the first frame, seen from directly behind, face never visible, marks time like a soldier: only the knees and boots move, his boots always land on the same two spots; from the waist up he is frozen like a statue and his head stays at the same place in the picture. One serrated axe stays hanging low in each hand. There are only ever TWO axes; they never double and never vanish. He never turns; he stays the same size in the same spot; appearance and colours never change. Seamless loop.

### [NW](../../under-fire-assets/units/orc-blood-berserker/03-final-source-exports/NW/manifest.json)

> Static camera that does not move at all: no zoom in, no zoom out. The same armoured orc berserker from the first frame, his back toward the viewer, turned away toward screen upper left, face never visible, marks time like a soldier: only the knees and boots move, his boots always land on the same two spots; from the waist up he is frozen like a statue and his head stays at the same place in the picture. One serrated axe stays hanging low in each hand. There are only ever TWO axes; they never double and never vanish. He never turns; he stays the same size in the same spot; appearance and colours never change. Seamless loop.

### [S](../../under-fire-assets/units/orc-blood-berserker/03-final-source-exports/S/manifest.json)

> Static camera that does not move at all: no zoom in, no zoom out. The same armoured orc berserker from the first frame, facing the viewer, marks time like a soldier: only the knees and boots move, his boots always land on the same two spots and stay well above the bottom of the frame; from the waist up he is frozen like a statue and his head stays at the same place in the picture. The figure stays exactly as tall as in the first frame; he never comes closer. One serrated axe stays hanging low in each hand. There are only ever TWO axes; they never double and never vanish. He never turns; appearance and colours never change. Seamless loop.

### [SW](../../under-fire-assets/units/orc-blood-berserker/03-final-source-exports/SW/manifest.json)

> Static camera that does not move at all: no zoom in, no zoom out. The same armoured orc berserker from the first frame, in three-quarter view turned toward screen lower left, steps on the spot without travelling: only the knees and boots move; the figure stays exactly as tall as in the first frame and keeps exactly the same angle. One serrated axe stays hanging low in each hand, close to his legs. There are only ever TWO axes, one in each hand; they never double and never vanish. He never turns; he stays the same size in the same spot; appearance and colours never change. Seamless loop.

### [W](../../under-fire-assets/units/orc-blood-berserker/03-final-source-exports/W/manifest.json)

> Static camera that does not move at all: no zoom in, no zoom out. The same armoured orc berserker from the first frame, in strict side profile facing screen left, marches in place like on a treadmill with short brisk steps; he never walks away and never gets smaller. One serrated axe stays hanging low in each hand, close to his legs. There are only ever TWO axes, one in each hand; they never double and never vanish. He never turns; he stays the same size in the same spot; appearance and colours never change. Seamless loop.

The older prompts are more directed than the current default, but their direction sometimes describes the wrong gait: N, NW and S explicitly request soldier-like marking time and a statue-like upper body; SW restricts movement to knees and boots; W requests short brisk marching steps. **Hypothesis:** those constraints encourage marching, shuffling or restricted stride rather than a natural forward treadmill walk. The text supports this mechanism; it does not establish exclusive causation. The October 14 checkpoint independently records owner-reported diagonal skipping, N/S marching and incomplete jerky E/W stride. [N manifest](../../under-fire-assets/units/orc-blood-berserker/03-final-source-exports/N/manifest.json); [quality checkpoint, lines 11–13](../../under-fire/docs/art/UNIT_ART_QUALITY_STATUS.md).

The ledger also documents rejected drifting/clipped takes and substantial windowing: selected one-cycle windows S20–39, SW20–39, W17–33, NW20–40 and N20–39. The archive identifies W's retained 17 frames as an incomplete stride and N/S source motion as marching. Therefore frame selection and source mechanics must be assessed alongside wording; timing or recentering alone cannot create absent stride poses. These are documented prior findings, not a fresh playback verdict. [Art ledger, lines 2412–2462](../../under-fire/docs/art/ART_LEDGER.md); [curated archive README](../../under-fire-assets/units/orc-blood-berserker/README.md).

## October 16 rebuild versus the built-in preset

The rebuild's `submitted_actions` records S, SW, W, NW and N using this exact action:

> walks forward in place with a full alternating left-right stride, low swinging feet, heel-to-toe weight transfer; never high-knee marches. Keeps reference facing and two axes, one per hand, held low

E, SE and NE record `walk: null`, meaning no custom walk action in that record. The top-level `actions.walk: null` expresses the later built-in policy; it must not be used to rewrite the first five submissions as default-prompt history. The run's `prompt_policy` explicitly preserves that distinction. [Rebuild run record, `submitted_actions`, `actions`, and `prompt_policy`](../../under-fire-assets/units/orc-blood-berserker/10-magic-studios-rebuild-2026-10-16/run.json).

At this source snapshot the built-in default is `walks in place`, inside `The character {action} — a clean, seamless walk cycle.` Thus its uncustomized base text is:

> The character walks in place — a clean, seamless walk cycle.

The final prompt assembler adds optional style, screenplay and chroma backdrop clauses. It does not itself read direction/facing fields or append direction-specific walking constraints. This is source inspection, not verification of any historical provider payload. [Preset defaults/templates, lines 50–70](../src/magic_video_editor/studio/presets.py); [prompt assembler, lines 3990–4003](../src/magic_video_editor/studio/service.py).

The longer rebuild action asks for alternating stride and heel-to-toe transfer and rejects high knees, unlike the older march directives. It still relies on “Keeps reference facing” rather than explicitly describing the screen-space heading. The built-in base sentence specifies neither gait mechanics nor root/camera/facing constraints. These comparisons concern the cited text, not demonstrated render quality. [Rebuild run record](../../under-fire-assets/units/orc-blood-berserker/10-magic-studios-rebuild-2026-10-16/run.json); [preset source](../src/magic_video_editor/studio/presets.py).

## Persisted server specification and export boundary

A read-only live-API audit supplied by the orchestrator records N revision 12, NW revision 11 and NE revision 11. N/NW persist the longer action above inside `The character {action} — a clean, seamless walk cycle.`; NE persists action `walks in place` and the default base prompt. All three have null style/script. This corroborates current persisted specifications, not a complete historical GPU request receipt. [API audit](../../under-fire-assets/units/orc-blood-berserker/10-magic-studios-rebuild-2026-10-16/walk-prompt-audit.json).

The orchestrator also supplied a read-only extraction of completed desktop dispatch-ledger `JobRecord.payload.prompt` values. NW job `5eb07e98-81d6-4057-9fb5-5efb33bfb51d` and N job `37b524d9-29a3-4e49-852a-e718edc35120` contain the full longer action, the clean/seamless walk clause, and the chroma paragraph below. NE job `074b551f-605d-49f2-9b9e-c9b105e7e9e8` contains the short default base sentence and that same paragraph. All three records have status `completed`. This is stronger historical evidence than the current project specification: it establishes the **persisted dispatch payload**, while remaining distinct from a provider graph receipt or proof of visual quality. [Dispatch prompt audit](../../under-fire-assets/units/orc-blood-berserker/10-magic-studios-rebuild-2026-10-16/walk-dispatch-prompt-audit.json).

> Render against a flat chroma green (#00B140) backdrop with even lighting, no shadows on the backdrop, and no green spill on the subject.

The default sheet packer fits each **whole image canvas** into its cell with `ImageOps.contain`; it does not crop to a moving foreground/weapon bounding box or register feet. Equal-size 512×512 input canvases consequently receive the same canvas fit, preserving movement within the source frame. Pivot registration writes PNG metadata rather than translating pixels. The optional normalized packer explicitly uses one shared affine transform measured from comparable resting poses, rather than resizing around each frame's silhouette. This source evidence does not support attributing drift to per-frame weapon-box recentering in these packers; generation, chosen frames and loop behavior still require playback inspection. [Default packer, lines 388–415](../src/magic_video_editor/studio/exports.py); [pivot registration, lines 728–739](../src/magic_video_editor/studio/exports.py); [normalization, lines 12–30](../src/magic_video_editor/studio/normalization.py).

## What Auto Walk validates

The native analyzer samples poses, foreground bounds and edge contact, then ranks candidate walk intervals using pose/seam similarity and an activity threshold. It does not track foot contact, foot slip, root position or a compass travel axis. A `complete` suggestion means an interval passed those heuristics; it is not acceptance of straight, grounded treadmill walking. The returned result explicitly requires visual review. This separates the server’s automatic interval selection from the stronger walking contract proposed below. [Frame analysis and cycle ranking, lines 23–214](../src/magic_video_editor/studio/frame_analysis.py); [walk-cycle test, line 21 onward](../tests/studio/test_frame_analysis.py).

## Attack comparison

The legacy selected attacks are highly constrained, often to one short low chop or a forward lean with hidden blades; exact texts are retained below. The ledger records edited S chops and reused rear-facing lean segments with overhead/clipped portions removed. By comparison, the rebuild's common action is `swings each of the axes in their hand to attack`; S has a custom quick dual-axe slash with follow-through and return. The current attack template additionally asks for wind-up, one committed strike, follow-through, return/hold, fixed camera, reference facing, constant size and full weapon containment. More explicit wording is therefore already server-owned for attacks, but that does not establish that the resulting animation satisfies it. [Legacy manifests below]; [art ledger](../../under-fire/docs/art/ART_LEDGER.md); [rebuild run record](../../under-fire-assets/units/orc-blood-berserker/10-magic-studios-rebuild-2026-10-16/run.json); [attack template, line 73](../src/magic_video_editor/studio/presets.py).

## Recommended server-owned walk contract (proposal)

Bake the shared walking contract into the server preset so callers can request a walk without rebuilding gait instructions. Resolve the selected view into an explicit screen-space facing clause, while preserving the opening reference's camera and identity. State a **forward treadmill walk with a stationary root**: alternating full strides along the facing axis, a supporting foot moving backward through stance and the other swinging forward, low foot clearance and natural weight transfer. Keep average body position, scale and camera fixed while allowing natural pelvic/torso movement and modest vertical motion. Preserve equipment and full-body containment. Ask for a repeatable full left/right cycle with a compatible loop seam. Avoid inheriting “boots land on the same two spots,” “only knees and boots move,” or a frozen waist-up statue from the legacy directives. This is a proposed interpretation of the observed wording and recorded defects, not a validated prompt guarantee. [Legacy walk directives above]; [quality checkpoint](../../under-fire/docs/art/UNIT_ART_QUALITY_STATUS.md); [current short walk template](../src/magic_video_editor/studio/presets.py).

Verify any change using controlled real renders through the supported server path: hold opening image, seed, machine recipe, resolution, duration and decode/export path constant; retain resolved prompts/settings and immutable source identities. Compare representative front, rear, side and diagonal views in actual playback, then inspect the extracted loop and final sprite export for full alternating strides, stable facing/root, grounding, readable feet, equipment continuity, containment and seam. A still contact sheet cannot establish cadence or a seamless motion loop. Keep source-gait and export/window defects separate, and preserve the user's selected assets during the comparison. This is a proposed validation protocol; no new generation or runtime acceptance is claimed here.

## Appendix: exact selected legacy attack directives

### [N](../../under-fire-assets/units/orc-blood-berserker/03-final-source-exports/N/manifest.json)

> Static camera that does not move at all: no zoom in, no zoom out. The same armoured orc berserker from the first frame, seen from directly behind, face never visible, alone on an empty background. Both arms low and close to his body: he leans forward and chops both axes down in front of him so the blades dip out of sight beyond his body, then straightens with the axes hanging low as in the first frame. The axes never rise above his shoulders, never swing out sideways and stay fully inside the frame; no motion blur. Nothing else is ever in the picture. There are only ever TWO axes, one in each hand; they never double and never vanish. He never turns; he stays the same size in the same spot; appearance and colours never change.

### [NW](../../under-fire-assets/units/orc-blood-berserker/03-final-source-exports/NW/manifest.json)

> Static camera that does not move at all: no zoom in, no zoom out. The same armoured orc berserker from the first frame, his back toward the viewer, turned away toward screen upper left, face never visible, alone on an empty background. Both arms low and close to his body: he leans forward and chops both axes down in front of him so the blades dip out of sight beyond his body, then straightens with the axes hanging low as in the first frame. The axes never rise above his shoulders and stay fully inside the frame; no motion blur. Nothing else is ever in the picture. There are only ever TWO axes, one in each hand; they never double and never vanish. He never turns; he stays the same size in the same spot; appearance and colours never change.

### [S](../../under-fire-assets/units/orc-blood-berserker/03-final-source-exports/S/manifest.json)

> Static camera that does not move at all: no zoom in, no zoom out. The same armoured orc berserker from the first frame, facing the viewer, alone on an empty background. He lifts the axe in his right hand only to waist height and makes one short chop straight down beside his right thigh, close to his body, then holds still as in the first frame; his other arm does not move. The axes never rise above his belt, never swing out sideways and stay fully inside the frame; slow and clear, no motion blur. Nothing else is ever in the picture. There are only ever TWO axes, one in each hand; they never double and never vanish. His body stays exactly the same size; he never turns; appearance and colours never change.

### [SW](../../under-fire-assets/units/orc-blood-berserker/03-final-source-exports/SW/manifest.json)

> Static camera that does not move at all: no zoom in, no zoom out. The same armoured orc berserker from the first frame, in three-quarter view turned toward screen lower left, alone on an empty background. He lifts the axe in his right hand (the one at screen left) only to waist height and makes one short chop straight down in front of his right thigh, close to his body, then holds still as in the first frame; his other arm does not move. The axes never rise above his belt, never swing out sideways and stay fully inside the frame; slow and clear, no motion blur. Nothing else is ever in the picture. There are only ever TWO axes, one in each hand; they never double and never vanish. He never turns; he stays the same size in the same spot; appearance and colours never change.

### [W](../../under-fire-assets/units/orc-blood-berserker/03-final-source-exports/W/manifest.json)

> Static camera that does not move at all: no zoom in, no zoom out. The same armoured orc berserker from the first frame, in strict side profile facing screen left, alone on an empty background; he stays in strict side profile for the whole clip. Both arms low and close to his body: he makes one short low upward slash with the near axe in front of him at waist height toward screen left, then lowers it back to where it was in the first frame. The axes never rise above his chest and stay fully inside the frame; no motion blur. Nothing else is ever in the picture: no target, no props. There are only ever TWO axes, one in each hand; they never double and never vanish. He never turns; he stays the same size in the same spot; appearance and colours never change.
