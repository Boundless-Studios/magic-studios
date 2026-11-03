# Supplied-view production

## Establish the project

Discover current tool schemas before mutation. Call `get_setup_capabilities` and `get_setup` on an existing project before editing. Setup schema version 1 supports `single_view`, `side_view`, and `isometric`; `unassigned` preserves legacy projects. An older server without these tools does not support atomic setup or neutral single-view semantics: keep its existing path or update it rather than inventing compatibility.

Use `single_view` / stable view ID `single` for one supplied image with no declared compass heading. Use `side_view` for declared left/right headings and `isometric` for selected compass headings. Templates are suggestions for the explicit request; they do not require a full grid or trigger generation. Eight-direction recommendations are eight walks, four cardinal attacks and South Idle, with Death optional. Any subset, no motions, and Idle/Death-only sets are valid.

For a new project, upload the character once with `upload_setup_reference_base64`. This stages verified project-owned bytes without changing the active subject or views. Bind the returned reference revision as both the identity reference and the uploaded view's opening reference in setup when the user confirms that artwork. Existing owned subject/view revisions can also be reused. Later view replacement preserves the identity anchor. Stage actual matching client bytes for other directions and use the returned reference revision ID; do not send raw storage keys as reference bindings. A missing reference remains a requested draft and cannot generate.

One declarative setup contains schema version, convention, stable views, each view's explicit heading/name/reference binding, and its requested motion definitions. Keep stable view and motion IDs when changing a heading; IDs identify history, while heading describes supplied art. Use the server's current setup as the starting point so an unrelated direction or animation is not accidentally omitted.

```text
get_setup_capabilities()
get_setup(project_id=P)
preview_setup(project_id=P, expected_revision=REV, desired_setup=DESIRED)
# Inspect impacts, affected take counts and blocked operations.
apply_setup(project_id=P, expected_revision=REV, plan_token=TOKEN,
            confirmed_impacts=REVIEWED_IMPACT_IDS)
get_setup(project_id=P)
```

Omitting an existing view or motion from the desired setup removes it from the active request. Confirm only impacts authorized by the user's requested edit. Clearing a reference instead retains the direction and motions as reference-needed drafts. A heading collision needs explicit replacement or cancellation; never merge histories or mirror pixels silently. Active affected jobs block structural edits until settled or explicitly cancelled through a supported cancellation path. A stale plan requires reading current state and previewing again; do not blindly retry apply. Setup never queues generation. A `recovery_required` dispatch reservation is different from an active job: only after establishing that the original dispatch has stopped, use `abandon_setup_dispatch` with the current revision and explicit stopped-dispatch confirmation. The server fences that operation permanently; it never resubmits generation. Refresh setup and preview the intended edit again. Do not abandon an active/completed job or attest that it stopped without evidence.

Use returned stable motion IDs for subsequent generation/editing. Keep supplied image projection, foreshortening, equipment and asymmetry. Declared headings describe screen-forward direction:

| Heading | Screen-forward direction |
| --- | --- |
| `N` | Away/up |
| `NE` | Away/right |
| `E` | Right |
| `SE` | Toward/right |
| `S` | Toward/down |
| `SW` | Toward/left |
| `W` | Left |
| `NW` | Away/left |
| `left` / `right` | Left / right |

A diagonal heading does not require a 45-degree pose. A neutral single view makes no screen-heading claim. Correct an assignment when art conflicts; setup does not rotate it. Replacing a reference or heading preserves prior takes/selections as stale history and refreshes current specifications. Removing content preserves historical manifest revisions and publications under existing retention policy; it does not promise undo.

Legacy imperative tools remain usable for existing compatible projects: `configure_sprite_layout`, `set_character_view`, `upload_view_reference_base64`, `add_view_motions`, and `update_view_motion`. Discover their current schemas. Omitting `motions` from `add_view_motions` adds idle/walk/attack; pass an explicit list for only requested actions. Omitted update fields preserve existing values; `clear_reference=true` explicitly clears a reference. Prefer preview/apply for structural correction or several related changes so a failure cannot leave a partially edited setup.

Action intent stays concise, for example “swings each axe to attack.” Omit the walk action override to use the native forward in-place preset. Server operations own motion mechanics.

## Dispatch and resume

Honor an explicitly requested GPU through [worker discovery and pinning](workflow.md#choose-a-hosted-worker), including its returned configuration/affinity settings. Record requested, resolved and actually executed machine/profile separately. Without a requested worker, use the server's authorized default. Never silently move a pinned job.

Generate one returned motion ID first, then settle bounded groups. Persist the returned `request_id` (including one generated by the server). Use `request_id` when supported for submission idempotency; keep it stable for the same request, and inspect the operation ledger after timeout/reconnect before submitting anything again. Idempotency is not a retry budget.

Before dispatch, persist a run manifest outside the skill package: project/view/motion IDs, approved identity and view reference hashes, current server/build, requested matrix, selected worker/configuration, attempt limit, processing-repair limit and accepted operation IDs. Append each attempt's immutable dispatched prompt, resolved settings, job/source/spec identities, elapsed time/cost where returned, failure and disposition. Resume from this record; reads do not replay interrupted jobs. Keep failed attempts. Read the task's generation and processing-repair budget and persist those bounds before attempting work. Stop at the bound or an unresolved server/worker fault and report the missing criterion.

## Extract and prepare evidence

Follow [native extraction and calibration](workflow.md). Suggestions propose a window; inspect and explicitly save it. Idle needs a plausible resting cycle; attack needs preparation, strike, follow-through and recovery; walk needs a complete reciprocal forward stride. Use actual source FPS/timebase, returned indices/holds and saved revision timing. Requested generation duration is not final cycle duration. Calibrate against an explicit same-view body reference with one fixed clip-wide transform; retain reference IDs/hashes, expanded geometry and exported pivots. Null pivots remain unverified registration.

Freeze a per-view/action evidence packet containing source, alpha and final artifact identities/hashes, supplied reference, full ordered source interval, selected indices/holds, actual timing, saved revision, loop boundary, body reference, geometry and pivots. Include native-scale light/dark final previews plus authentic normal-speed playback of the selected revision. Keep source and final timing linked. Contact sheets diagnose phases; stills and calculated duration alone cannot establish observed cadence.

## Independent acceptance and repair

Locate `unit-art-acceptance` in the caller environment and read its asset-only procedure and brief. That skill owns the rubric; this package does not copy or weaken it. Supply a fresh independent judge only the anonymous current evidence, declared view/action contract, intended scale and authoritative rubric. Exclude builder explanations, historical verdicts and operator calibration labels/answer keys. Record which artifacts and actual viewing method the judge used.

Existing credentials do not authorize a separately billed evaluator; use only the user's authorized review route.

A video evaluator must actually accept and inspect temporal media and demonstrate useful positive/negative timing controls before closing cadence; discovering a provider name or attaching a video path is insufficient. Until a route is qualified, temporal acceptance is UNVERIFIED. Studio gains no LLM dependency. Preserve separate fidelity, support/anchor, gait/action phases, cadence and technical verdicts for every requested view/action. No required FAIL or UNVERIFIED permits a game-ready claim.

Classify an observed defect before spending another attempt: native processing/alpha/registration repair, supplied reference or source correction, timing adjustment, or bounded regeneration. Rebuild affected evidence and rejudge it independently. Retain user-selected assets and historical evidence. Asset-only review establishes no engine import, ring placement, simulation-speed foot sliding or runtime transitions; those require their own runtime journey.
