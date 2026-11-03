"""Tenant-scoped machine discovery and recipe resolution before dispatch."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from outbound_gpu_worker_pool.machine_profiles import (
    CONFIG_KEY, LAST_FRAME_FEATURE_KEY, RECIPE_KEY, LIGHTX8, STANDARD, describe_profile,
)
from magic_video_editor.studio.models import MotionGenerationSettings


def _unavailable_reason(view, profile, recent: bool) -> str:
    """Why this machine cannot take a motion now, in the words the UI shows."""
    from magic_video_editor.machine_features import MOTIONS, gb, reason

    if view.draining:
        return 'It is shutting down'
    if profile is None or (view.vram_mb or 0) < profile['min_vram_mb']:
        why = reason(view, MOTIONS)
        if why is None:
            why = 'its motion recipe is not one this studio has verified; update the worker'
        return why[0].upper() + why[1:]
    if not view.runtime_versions.get(CONFIG_KEY):
        return 'It did not report its configuration; update the worker'
    if not recent or view.status.value not in ('online', 'busy'):
        return 'It is offline'
    return f"It is not available ({view.status.value}, {gb(view.vram_mb or 0)})"


class HostedMachineDefaults:
    def __init__(self, pool, tenant_for, operators=None):
        self.pool = pool
        self.tenant_for = tenant_for
        self.operators = operators

    async def discover(self, owner_id: str, worker_id: str | None = None, *,
                       needs_last_frame: bool = False, only_profile: str | None = None) -> dict[str, Any]:
        tenant = await self.tenant_for(owner_id)
        operators = self.operators
        if (operators is not None and tenant in operators.shared_tenants
                and await operators.shared_tenant(owner_id) == tenant):
            # Shared work is taken by the machines the operator access says may
            # serve it; their immutable registry tenants stay unchanged.
            views = await operators.shared_worker_views(self.pool)
        else:
            views = await self.pool.worker_views(tenant_id=tenant)
            # The pool's None argument means all tenants; enforce house-only here.
            views = [v for v in views if v.tenant_id == tenant]
        rows = []
        for view in views:
            if operators is not None:
                enrolled = await operators.enrolment.worker_by_id(view.worker_id)
                if enrolled is not None and enrolled.revoked:
                    continue
            if worker_id and view.worker_id != worker_id:
                continue
            versions = view.runtime_versions
            profile = next((describe_profile(p) for p in (LIGHTX8, STANDARD)
                            if describe_profile(p)['recipe_version'] == versions.get(RECIPE_KEY)
                            and describe_profile(p)['capability_id'] in view.capability_ids), None)
            recent = view.last_heartbeat_at is not None and (datetime.now(UTC)-view.last_heartbeat_at).total_seconds() <= 90
            eligible = bool(profile and versions.get(CONFIG_KEY) and recent and not view.draining
                            and view.status.value in ('online', 'busy')
                            and (view.vram_mb or 0) >= profile['min_vram_mb'])
            rows.append({'worker_id': view.worker_id, 'status': view.status.value,
                         'available': eligible, 'profile': profile,
                         'configuration_version': versions.get(CONFIG_KEY),
                         'capability_ids': list(view.capability_ids),
                         'legacy_standard': not versions.get(RECIPE_KEY) and describe_profile(STANDARD)['capability_id'] in view.capability_ids,
                         'legacy_available': not versions.get(RECIPE_KEY) and recent and not view.draining
                             and (view.vram_mb or 0) >= 24000
                             and describe_profile(STANDARD)['capability_id'] in view.capability_ids,
                         'runtime_versions': versions,
                         # Declared by the worker's own profile check, never inferred here.
                         'features': {'last_frame': versions.get(LAST_FRAME_FEATURE_KEY) == '1'},
                         'reason': None if eligible else _unavailable_reason(view, profile, recent)})
        candidates = sorted((r for r in rows if r['available']
                             and (not needs_last_frame or r['features']['last_frame'])
                             and (only_profile is None or r['profile']['profile'] == only_profile)),
                            key=lambda r: (r['status'] != 'online', r['worker_id']))
        selected = candidates[0] if candidates else None
        return {'scope': 'hosted', 'selection_policy': 'owned tenant, otherwise house; online before busy, then worker ID; no cross-recipe fallback',
                'workers': rows, 'selected': selected,
                'settings': self.settings_for(selected) if selected else None}

    @staticmethod
    def settings_for(selected):
        profile = selected['profile']
        parameters = {'worker_id': selected['worker_id'], 'recipe_version': profile['recipe_version'],
                      'configuration_version': selected['configuration_version']}
        if profile['profile'] == LIGHTX8:
            parameters.update(execution_profile='h3-w4a8-16gb-lightx8', fps=24)
        return {'preset': 'standard', 'backdrop': 'chroma_green', 'steps': profile['steps'], 'width': 512, 'height': 512, 'duration_seconds': 4,
                'seed': 42, 'provider_parameters': parameters}

    async def resolve(self, owner_id: str, settings: MotionGenerationSettings) -> MotionGenerationSettings:
        params = settings.provider_parameters
        closed_loop = params.get('closed_loop') is True
        discovered = await self.discover(owner_id, params.get('worker_id'))
        selected = discovered['selected']
        if selected is None:
            bound = any(params.get(k) for k in ('worker_id', 'recipe_version', 'configuration_version', 'execution_profile'))
            legacy = any(r['legacy_available'] for r in discovered['workers'])
            if legacy and not bound:
                return settings
            reasons = ' '.join(f"{r['worker_id']}: {r['reason']}." for r in discovered['workers'] if r['reason'])
            raise ValueError(' '.join(filter(None, (
                'No compatible online machine with a verified recipe.', reasons,
                'Configure/enroll a machine and refresh generation settings, or run motions '
                'with your Higgsfield key from the Machines page.'))))
        defaults = self.settings_for(selected)
        for key in ('recipe_version', 'configuration_version'):
            if params.get(key) is not None and params[key] != defaults['provider_parameters'][key]:
                raise ValueError('Machine configuration changed; refresh generation settings before submitting')
        if closed_loop and selected['profile']['profile'] == STANDARD and not selected['features']['last_frame']:
            raise ValueError('The selected machine does not report last-frame support; update the worker before closed-loop generation')
        expected = defaults['provider_parameters'].get('execution_profile')
        if params.get('execution_profile') is not None and params['execution_profile'] != expected:
            raise ValueError('Requested execution profile does not match the selected machine')
        if selected['profile']['profile'] == LIGHTX8:
            if 'steps' in settings.model_fields_set and settings.steps != 8:
                raise ValueError('This machine requires exactly eight steps; refresh generation settings')
            if settings.width > 512 or settings.height > 512 or settings.duration_seconds > 4:
                raise ValueError('This machine supports at most 512x512 and four requested seconds')
            if params.get('fps', 24) != 24:
                raise ValueError('This machine requires 24 FPS')
        bounds = selected['profile']['bounds']
        steps = settings.steps if 'steps' in settings.model_fields_set else defaults['steps']
        fps = params.get('fps', 24)
        if isinstance(fps, bool) or not isinstance(fps, int) or not 8 <= fps <= 30:
            raise ValueError('FPS must be an integer between 8 and 30')
        if not 256 <= settings.width <= bounds['width'] or not 256 <= settings.height <= bounds['height']:
            raise ValueError('Dimensions are outside the selected machine bounds')
        if settings.duration_seconds > bounds['duration_seconds'] or not 17 <= settings.duration_seconds * fps <= 362:
            raise ValueError('Duration/frame count is outside the selected machine bounds')
        if selected['profile']['profile'] == STANDARD and not 4 <= steps <= 40:
            raise ValueError('Standard workflow supports 4–40 steps')
        updates = {'provider_parameters': {**params, **defaults['provider_parameters']}}
        if 'steps' not in settings.model_fields_set:
            updates['steps'] = defaults['steps']
        return MotionGenerationSettings.model_validate({**settings.model_dump(), **updates})
