"""The 3D engine's `mesh` and `clean` stages on the hosted pool.

Studio never runs a stage itself. It resolves one pinned worker on the engine's
machine profile (`mesh3d-24gb-standard`), journals the job on the asset, submits
it with the worker, recipe and configuration pinned, and folds the attested
result into a candidate. See docs/design/3d-studio.md, "Integration contract".

Generation is off unless `MAGIC_VIDEO_EDITOR_3D_GENERATION_ENABLED` is true, and
even then nothing is offered while the profile is unqualified: the pool refuses
an unqualified profile at enrolment, registration and dispatch, so Studio says
so up front instead of queueing a job no machine may take. There is no fallback
to another profile, ever.

Settings are checked against the pool's own declared recipe (`engine_recipes`):
the mesh stage takes `resolution` (512 or 1024; the engine would run a requested
768 at 1024) and `seed`; the clean stage exactly one of `quality_tier` or
`target_triangles`.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from outbound_gpu_worker_pool import machine_profiles
from outbound_gpu_worker_pool.contracts import JobRequirements, JobSubmission
from outbound_gpu_worker_pool.engine_recipes import (
    MAX_SEED,
    MESH3D,
    MESH3D_CLEAN_CAPABILITY,
    MESH3D_MESH_CAPABILITY,
    EngineRecipe,
    EngineStage,
)

from magic_video_editor.assets3d.models import Asset3DGate, Asset3DGateCheck, Asset3DOperation, Stage

GENERATION_ENV = "MAGIC_VIDEO_EDITOR_3D_GENERATION_ENABLED"


def generation_enabled(values: Mapping[str, str]) -> bool:
    """On only for an explicit `true` (any case, surrounding spaces ignored):
    unset, empty, `false`, `0`, `1` or anything else leaves stages off."""
    return values.get(GENERATION_ENV, "").strip().lower() == "true"

PROFILE = MESH3D
ENGINE_STAGES: tuple[Stage, ...] = ("mesh", "clean")
CAPABILITIES: dict[str, str] = {"mesh": MESH3D_MESH_CAPABILITY, "clean": MESH3D_CLEAN_CAPABILITY}
# What a request may leave out, filled in and recorded before submission.
DEFAULT_RESOLUTION = 1024
DEFAULT_QUALITY_TIER = "standard"

DISABLED = "not available yet: 3D generation is disabled"
UNQUALIFIED = "not available yet: the 3D engine profile is not qualified"
NO_POOL = "not available yet: this server has no worker pool"
PUBLIC_NEEDS_OWN_MACHINE = (
    "not available yet: connect your own machine with the 3D engine profile; "
    "public accounts never use house machines"
)
NO_WORKER = "not available yet: no machine you can use serves the 3D engine profile"


class StageUnavailable(Exception):
    """The stage cannot run for this owner now; the message says why."""


def engine_recipe() -> EngineRecipe:
    spec = machine_profiles.PROFILES[PROFILE]
    assert spec.engine is not None
    return spec.engine


def engine_stage(stage: str) -> EngineStage:
    found = engine_recipe().stage_for(CAPABILITIES[stage])
    assert found is not None
    return found


def checked_settings(stage: str, requested: Mapping[str, Any]) -> dict[str, Any]:
    """The settings as they will be submitted: the request's, checked against the
    recipe's declared domains, with the defaults it left out filled in. Raises
    ValueError with the reason."""
    spec = engine_stage(stage)
    domains = dict(spec.settings)
    unknown = sorted(set(requested) - set(domains))
    if unknown:
        raise ValueError(f"The {stage} stage takes no setting {', '.join(unknown)}.")
    settings = dict(requested)
    if stage == "mesh":
        settings.setdefault("resolution", DEFAULT_RESOLUTION)
        # A seed the caller did not choose is still chosen here, recorded and
        # attested, so the run can be repeated.
        settings.setdefault("seed", secrets.randbelow(MAX_SEED + 1))
    if spec.one_of_settings and not any(name in settings for name in spec.one_of_settings):
        settings[spec.one_of_settings[0]] = DEFAULT_QUALITY_TIER
    for name, value in settings.items():
        kind, values = domains[name]
        if kind == "choices":
            if isinstance(value, bool) or value not in values:
                raise ValueError(f"{name} must be one of {', '.join(map(str, values))}.")
        elif isinstance(value, bool) or not isinstance(value, int) or not values[0] <= value <= values[1]:
            raise ValueError(f"{name} must be a whole number from {values[0]} to {values[1]}.")
    missing = [name for name in spec.required_settings if name not in settings]
    if missing:
        raise ValueError(f"The {stage} stage needs {', '.join(missing)}.")
    if spec.one_of_settings and sum(name in settings for name in spec.one_of_settings) != 1:
        raise ValueError(f"Give exactly one of {' or '.join(spec.one_of_settings)}.")
    return settings


def requirements(operation: Asset3DOperation) -> JobRequirements:
    """Pinned to the resolved worker and the recipe and configuration it
    registered, under the keys it registered them with."""
    return JobRequirements(
        min_vram_mb=operation.min_vram_mb,
        worker_ids=(operation.worker_id,),
        runtime_versions={
            operation.recipe_key: operation.recipe_version,
            operation.configuration_key: operation.configuration_version,
        },
    )


def submission(asset_id: str, operation: Asset3DOperation) -> JobSubmission:
    """The same job every time it is (re)submitted: one job id and idempotency key."""
    return JobSubmission(
        job_id=operation.job_id,
        idempotency_key=f"asset3d-stage:{asset_id}:{operation.request_id}",
        capability_id=operation.capability_id,
        input_keys=tuple(dict.fromkeys(operation.inputs.values())),
        output_key=operation.output_key,
        payload={"inputs": dict(operation.inputs), **operation.settings},
        tenant_id=operation.tenant_id,
        requirements=requirements(operation),
        execution_deadline_seconds=operation.deadline_seconds,
    )


def attested(record: Any) -> dict[str, Any]:
    """The worker's diagnostics as the coordinator accepted them at completion
    (runtime versions and effective inputs are verified against the job there)."""
    metadata = getattr(record, "execution_metadata", None) or {}
    diagnostics = metadata.get("diagnostics") if isinstance(metadata, dict) else None
    return diagnostics if isinstance(diagnostics, dict) else {}


def gate_from(diagnostics: Mapping[str, Any], stage: str) -> Asset3DGate | None:
    """The engine's gate. A gate the worker had to leave out of its bounded
    diagnostics, or one Studio cannot read, counts as failed: unknown never passes."""
    gate = diagnostics.get("gate")
    if gate is None:
        return None
    if not isinstance(gate, dict) or not isinstance(gate.get("passed"), bool):
        return Asset3DGate(
            name=stage, passed=False,
            checks=[Asset3DGateCheck(id="gate_unreadable", passed=False)],
        )
    raw = gate.get("checks")
    checks = [
        Asset3DGateCheck(
            id=str(check["id"])[:120], passed=check["passed"],
            value=check.get("value"), threshold=check.get("threshold"),
        )
        for check in (raw if isinstance(raw, list) else [])
        if isinstance(check, dict) and isinstance(check.get("passed"), bool) and "id" in check
    ]
    return Asset3DGate(name=str(gate.get("name") or stage)[:120], passed=gate["passed"], checks=checks)


def provenance_from(record: Any, operation: Asset3DOperation) -> dict[str, Any]:
    """What ran, from the attested record only: never the requested settings."""
    diagnostics = attested(record)
    metadata = getattr(record, "execution_metadata", None) or {}
    return {
        "capability_id": record.capability_id,
        "profile": operation.profile,
        "recipe_version": operation.recipe_version,
        "configuration_version": operation.configuration_version,
        "worker_id": getattr(record, "executed_by", None),
        "job_id": record.job_id,
        "request_id": operation.request_id,
        "plugin": {"id": metadata.get("plugin_id"), "version": metadata.get("plugin_version")},
        "model": {"id": metadata.get("model_id"), "version": metadata.get("model_version")},
        "engine": diagnostics.get("engine"),
        "effective_inputs": diagnostics.get("effective_inputs"),
        "engine_effective_inputs": diagnostics.get("engine_effective_inputs"),
        "engine_measured": diagnostics.get("measured"),
        "runtime_versions": diagnostics.get("runtime_versions"),
        "timings_s": diagnostics.get("timings_s"),
    }


@dataclass
class StageEngine:
    """Whether stages may run, and where. `pool` and `machines` are attached when
    the app has a worker pool; `own_tenant_for` says whether an owner has a
    machine of their own (public accounts may use only that)."""

    enabled: bool = False
    pool: Any = None
    machines: Any = None
    own_tenant_for: Callable[[str], Awaitable[str | None]] | None = None

    def blocked(self) -> str | None:
        """Why no stage can run for anyone, or None."""
        if not self.enabled:
            return DISABLED
        if machine_profiles.unusable_reason(PROFILE) is not None:
            return UNQUALIFIED
        if self.pool is None or self.machines is None:
            return NO_POOL
        return None

    async def _owner_blocked(self, owner: str, public: bool) -> str | None:
        reason = self.blocked()
        if reason is None and public:
            own = await self.own_tenant_for(owner) if self.own_tenant_for is not None else None
            if own is None:
                return PUBLIC_NEEDS_OWN_MACHINE
        return reason

    async def availability(self, owner: str, public: bool) -> dict[str, dict[str, Any]]:
        reason = await self._owner_blocked(owner, public)
        if reason is not None:
            return {stage: {"available": False, "reason": reason} for stage in ENGINE_STAGES}
        result: dict[str, dict[str, Any]] = {}
        for stage in ENGINE_STAGES:
            found = await self.machines.discover_engine(owner, PROFILE, CAPABILITIES[stage])
            selected = found["selected"]
            result[stage] = (
                {"available": True, "reason": None, "worker_id": selected["worker_id"]}
                if selected is not None else {"available": False, "reason": NO_WORKER}
            )
        return result

    async def pin(self, owner: str, public: bool, stage: str) -> dict[str, Any]:
        """The worker, keys and versions to pin, or StageUnavailable."""
        reason = await self._owner_blocked(owner, public)
        if reason is not None:
            raise StageUnavailable(reason)
        found = await self.machines.discover_engine(owner, PROFILE, CAPABILITIES[stage])
        selected = found["selected"]
        if selected is None:
            details = " ".join(f"{w['worker_id']}: {w['reason']}." for w in found["workers"] if w["reason"])
            raise StageUnavailable(" ".join(filter(None, (NO_WORKER + ".", details))))
        return {**selected, "tenant_id": found["tenant_id"], "min_vram_mb": found["min_vram_mb"]}
