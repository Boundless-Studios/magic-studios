# Video evaluation rubric and policy

Status: practice, effective 2026-09-18. Applies to every assembled movie
before it is sent to a person, released, or called done.

## Why

The first automatic scenes were judged by numbers (transcript, join diffs,
loudness) and sent out with doubled pictures and dialogue running 1.5 s early.
Measurements were missing for the faults a viewer sees first. This rubric
names every check, its measurement, its threshold and whether it gates.

## Policy

1. Every assembly produces an **evaluation report** (`evaluation.json`) and a
   **Jev packet** (`jev-packet.json`): structured facts only, no images.
2. **Jev** (the evaluator; it has no image model) applies this rubric to the
   packet and returns a verdict per criterion with reasons, and one overall
   verdict: `pass`, `fail`, or `waive-requested`.
3. A cut with any failed **gate** is not sent, released or called done. A
   human may waive a gate with a one-line reason recorded in the report.
4. Reports and Jev's verdict are kept next to the cut (proof folder) and, once
   the Studio produces them, on the assembly record.
5. Thresholds below are calibrated on the accepted WYRM v5 trailer and the v7
   Studio cut; changing one is a documented decision, not a tweak in a run.

## Criteria

Each criterion: what is measured → how → threshold → gate?

### A. Script fidelity — gate
- Per spoken line, word error rate of the ASR transcript against the planned
  line (after normalising case, punctuation and numerals): **≤ 0.15**.
- Extra speech not in the plan: **≤ 3 words** per take (no invented sentences,
  no stage directions read aloud).
- Missing lines: **none** (every planned line has a match with WER ≤ 0.5).
- Silent shots: ASR words **≤ 2** and no vocalisation (RMS speech-band energy
  below −45 dBFS for ≥ 95 % of the shot).

### B. Audio/video synchronisation — gate
- Per clip: audio stream duration ≥ video duration − **50 ms** (a short track
  shifts everything after it).
- Per dialogue take: the take's sound located in the assembled cut by envelope
  cross-correlation against where its picture starts (correlation ≥ 0.3):
  **|Δ| ≤ 80 ms**. Word onsets from ASR are not used for this: they jitter by
  hundreds of milliseconds, more under music.
- Mouth motion vs speech envelope cross-correlation lag (`analyze_av mouth`):
  **|lag| ≤ 2 frames** at 24 fps, correlation **≥ 0.3**, for every dialogue
  take.

### C. Voice binding — gate
- Per utterance, speaker-embedding similarity to the character's own reference
  is the highest of all references and exceeds the runner-up by **≥ 0.10**.
- **≥ 95 %** of utterances pass; no character misbound twice in a row.

### D. Continuity — gate on chained joins, advisory on designed cuts
- Join measure: mean absolute difference of 96×54 grey frames either side of
  the cut. Chained joins (`continuation_of` set): **≤ 0.012**. Designed cuts
  (new setup): reported, not gated.
- Within-take jumps: largest frame-to-frame difference inside any take
  **≤ 0.03** (no mid-take resets or identity swaps).
- Within-take drift: largest difference between any frame and the take's
  first frame, for dialogue takes, **≤ 0.04**. A gradual relight or morph
  never jumps between neighbours but drifts (accepted WYRM takes 0.005–0.015,
  good Thornwick takes ≤ 0.025, a relit take 0.106). Motion shots may travel
  and are reported, not gated.

### E. Picture integrity — gate
- No split screen / collage in any plate or take: a straight horizontal or
  vertical band of near-uniform colour spanning **≥ 85 %** of the frame, or
  a frame whose halves correlate **≥ 0.9** with each other, fails.
- No letterbox/pillarbox band inside the picture (same detector at the edges).
- Frame sharpness (Laplacian variance) within **0.3×–3×** of the plate's, per
  take, sampled once per second.

### F. Identity consistency — gate when a face model is available, else Jev
- Per character, face-embedding similarity between shots **≥ 0.6**. Without a
  face model: Jev compares the per-shot descriptions in the packet against the
  cast sheet and flags contradictions (hair, skin, clothing, build).

### G. Sound — gate
- Integrated loudness **−16 ± 1 LUFS**, true peak **≤ −1.0 dBTP**.
- No silence (< −50 dBFS) longer than **4.0 s** inside a dialogue chain
  (calibrated: the accepted WYRM v7 scored cut holds a 3.9 s pause).
- Cues, when present, at least **6 dB** below speech while speech plays.

### H. Length — advisory
- Total duration within **±15 %** of the target when one was set.

### I. Scene sense — Jev, gate
Given the shot plan and the packet's per-shot facts (who speaks, when, what
the plate shows as described in text, measured motion), Jev judges: does the
edit read as one scene in one place; are the right characters present in each
shot; do props appear when the plan says they do; does anything contradict the
plan. Score **1–5** with reasons; **≥ 4** passes.

## The Jev packet

`jev-packet.json` carries only text and numbers:

```
{
  "movie": {"id", "title", "target_seconds", "duration_seconds"},
  "cast": [{"character_id", "name", "appearance", "voice"}],
  "shots": [{"shot_id", "continuation_of", "planned_lines": [...],
             "opening_prompt", "plate": {"description", "collage": bool},
             "take": {"duration", "audio_duration", "first_word_offset_ms",
                      "mouth_lag_frames", "mouth_corr", "max_internal_jump"},
             "transcript": [{"text", "start", "end", "speaker_best",
                             "similarity": {...}}],
             "line_wer": [...], "extra_words": n}],
  "joins": [{"index", "at_seconds", "diff", "chained": bool}],
  "sound": {"lufs", "true_peak", "longest_silence"},
  "rubric": {criterion: {"measured", "threshold", "pass", "gate"}}
}
```

Plate and keyframe **descriptions** are produced by a vision-capable model
(through the same OpenRouter route the director uses) from the image, so Jev
reasons over words. They are evidence, not verdicts.

## Regression fixtures

The evaluator is itself under test. `--expect pass` on Thornwick take 5 final
(movie `4835ed32…`, cut `thornwick-take5-final.mp4`, the first cut to pass
every gate with no waiver) and `--expect fail` on Thornwick take 3 must both
hold before a change to a threshold or a measurement lands. The evaluator's
timeline mirrors the assembler's: each clip's picture length minus the
dissolve overlap into the next (floored to frames, capped at half of either
neighbour); a dissolved join is measured at its midpoint, a hard cut at the
largest neighbour step within a frame of the computed time. Without that,
every clip after a dissolve read as a quarter-second early.
A criterion the evaluator cannot measure is listed under `not_measured` and
is never reported as a pass. If any shot's selected take is missing from the
evaluation set, the verdict is `incomplete` — not pass, not fail — and the
report cannot satisfy a fixture or a release. A retake of one shot invalidates
its continuations; every shot in the final cut must be re-fetched and
measured before a verdict is read.

Plates are also described in words by a vision model
(`describe_plates.py` → `plates-described.json`): how many people are in the
frame and what each looks like, set against the cast the shot needs. This is
the evidence for criterion F when no face model is available.

## Where it runs

- Today: `tools/evaluate_cut.py` on the analysis host produces both files for
  a movie folder (takes, plates, cut, plan).
- Target: the Studio computes the report at assembly (the seam gate already
  lives there) and the Review step shows it; a release requires a passing
  report or a recorded waiver.
