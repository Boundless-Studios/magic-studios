from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from outbound_gpu_worker_pool.contracts import PoolWorkerView, PoolWorkerStatus
from outbound_gpu_worker_pool.machine_profiles import (
    LIGHTX8, STANDARD, RECIPE_KEY, CONFIG_KEY, LAST_FRAME_FEATURE_KEY, describe_profile,
)
from magic_video_editor.machine_defaults import HostedMachineDefaults
from magic_video_editor.studio.models import MotionGenerationSettings


def worker(worker_id='mine', tenant='owner', profile=LIGHTX8, **overrides):
    recipe = describe_profile(profile)
    row = PoolWorkerView(worker_id, PoolWorkerStatus.ONLINE, datetime.now(UTC),
                         (recipe['capability_id'],), (), None, False, tenant,
                         vram_mb=recipe['min_vram_mb'],
                         runtime_versions={RECIPE_KEY: recipe['recipe_version'], CONFIG_KEY: 'config-v1'})
    return replace(row, **overrides)


class Pool:
    def __init__(self, *rows): self.rows = rows
    async def worker_views(self, **kwargs): return self.rows


def resolver(*rows, tenant='owner'):
    async def tenant_for(owner): return tenant
    return HostedMachineDefaults(Pool(*rows), tenant_for)


async def test_discovery_is_tenant_scoped_and_resolution_records_real_eight_steps():
    defaults = resolver(worker(), worker('other', 'someone-else'), worker('house', None))
    discovered = await defaults.discover('owner')
    assert [r['worker_id'] for r in discovered['workers']] == ['mine']
    settings = await defaults.resolve('owner', MotionGenerationSettings())
    assert settings.steps == 8
    assert settings.provider_parameters['worker_id'] == 'mine'
    assert settings.provider_parameters['execution_profile'] == 'h3-w4a8-16gb-lightx8'
    with pytest.raises(ValueError, match='No compatible'):
        await defaults.resolve('owner', MotionGenerationSettings(provider_parameters={'worker_id': 'other'}))


async def test_house_scope_never_exposes_other_tenants():
    discovered = await resolver(worker(), worker('house', None), tenant=None).discover('visitor')
    assert [r['worker_id'] for r in discovered['workers']] == ['house']


@pytest.mark.parametrize('changes', [
    {'draining': True}, {'vram_mb': 15000},
    {'last_heartbeat_at': datetime.now(UTC)-timedelta(minutes=3)},
    {'runtime_versions': {RECIPE_KEY: 'changed', CONFIG_KEY: 'config-v1'}},
])
async def test_unavailable_or_changed_workers_fail_closed(changes):
    defaults = resolver(worker(**changes))
    assert (await defaults.discover('owner'))['selected'] is None
    with pytest.raises(ValueError, match='No compatible'):
        await defaults.resolve('owner', MotionGenerationSettings())


async def test_stale_preflight_and_explicit_wrong_steps_are_rejected():
    defaults = resolver(worker())
    with pytest.raises(ValueError, match='configuration changed'):
        await defaults.resolve('owner', MotionGenerationSettings(provider_parameters={'configuration_version':'old'}))
    with pytest.raises(ValueError, match='eight steps'):
        await defaults.resolve('owner', MotionGenerationSettings(steps=20))
    with pytest.raises(ValueError, match='512x512'):
        await defaults.resolve('owner', MotionGenerationSettings(width=768))


async def test_standard_default_is_twenty_steps_and_bound_separately():
    settings = await resolver(worker(profile=STANDARD)).resolve('owner', MotionGenerationSettings())
    assert settings.steps == 20
    assert 'execution_profile' not in settings.provider_parameters
    assert settings.provider_parameters['recipe_version'] == describe_profile(STANDARD)['recipe_version']


async def test_recipe_cannot_change_when_selected_machine_is_offline():
    defaults = resolver(worker(last_heartbeat_at=datetime.now(UTC)-timedelta(minutes=3)),
                        worker('standard', profile=STANDARD))
    settings = MotionGenerationSettings(provider_parameters={'worker_id':'mine'})
    with pytest.raises(ValueError, match='No compatible'):
        await defaults.resolve('owner', settings)


async def test_dispatch_binds_actual_settings_and_failure_never_runs_a_fallback():
    from studio.test_motion_pool import _Pool, _service_with_motion, PROJECT
    from magic_video_editor.contracts import JobStatus
    pool = _Pool()
    service, jobs, dispatcher, project = await _service_with_motion(pool)
    from magic_video_editor.studio.models import MotionSpecification
    project = await service.add_motion(PROJECT, project.revision,
        MotionSpecification(motion_id='idle', name='Idle', kind='preset', preset='idle', loop_policy='loop'), 'user-1')
    defaults = resolver(worker(tenant='user-1'), tenant='user-1')
    service.machine_defaults = defaults
    record = await service.generate_motion(PROJECT, project.revision, 'idle', 'user-1', 'bound')
    project = await service.get_project(PROJECT, 'user-1')
    submitted = pool.submissions[0]
    assert record.payload['steps'] == submitted.payload['steps'] == 8
    assert record.payload['requested_settings'] == {}
    assert submitted.requirements.worker_ids == ('mine',)
    assert submitted.requirements.runtime_versions[CONFIG_KEY] == 'config-v1'
    assert submitted.requirements.min_vram_mb == 16000
    assert submitted.capability_id == describe_profile(LIGHTX8)['capability_id']
    assert dispatcher.job_ids == []
    pool.fail_submit = True
    from magic_video_editor.studio.service import MotionGenerationUnavailable
    with pytest.raises(MotionGenerationUnavailable, match='no fallback'):
        await service.generate_motion(PROJECT, project.revision, 'idle', 'user-1', 'unavailable')
    assert dispatcher.job_ids == []
    assert any(r.status == JobStatus.FAILED for r in jobs.records.values())


@pytest.mark.parametrize('settings', [MotionGenerationSettings(width=64), MotionGenerationSettings(steps=60), MotionGenerationSettings(duration_seconds=20)])
async def test_verified_standard_requests_never_silently_clamp(settings):
    with pytest.raises(ValueError):
        await resolver(worker(profile=STANDARD)).resolve('owner', settings)


async def test_verified_standard_duration_is_not_clamped_to_old_six_second_bound():
    from magic_video_editor.api_models import GenerateMotionPayloadV1
    from magic_video_editor.studio.service import _motion_pool_payload
    settings = await resolver(worker(profile=STANDARD)).resolve('owner', MotionGenerationSettings(duration_seconds=15))
    payload = GenerateMotionPayloadV1(subject_key='sources/subject.png', motion_id='walk', prompt='walk', loop=True,
        requested_revision=1, subject_revision_id='subject', subject_artifact_key='sources/subject.png',
        subject_sha256='a'*64, motion_spec_digest='b'*64, duration_seconds=15,
        provider_parameters=settings.provider_parameters)
    mapped, _ = _motion_pool_payload(payload)
    assert mapped['length']==360


async def test_changed_standard_recipe_cannot_masquerade_as_a_legacy_worker():
    row = worker(profile=STANDARD, runtime_versions={RECIPE_KEY:'unknown-new-version', CONFIG_KEY:'new'})
    with pytest.raises(ValueError, match='No compatible'):
        await resolver(row).resolve('owner', MotionGenerationSettings())


async def test_legacy_standard_remains_unbound_but_keeps_twenty_steps():
    row = worker(profile=STANDARD, runtime_versions={})
    result = await resolver(row).resolve('owner', MotionGenerationSettings())
    assert result.steps == 20 and not result.provider_parameters


def last_frame_worker(worker_id='mine', **overrides):
    """A standard worker whose profile check declared last-frame support."""
    row = worker(worker_id, profile=STANDARD, **overrides)
    return replace(row, runtime_versions={**row.runtime_versions, LAST_FRAME_FEATURE_KEY: '1'})


async def test_discovery_reports_last_frame_and_can_require_it():
    defaults = resolver(worker('a-plain', profile=STANDARD), last_frame_worker('b-loop'))
    discovered = await defaults.discover('owner')
    features = {r['worker_id']: r['features']['last_frame'] for r in discovered['workers']}
    assert features == {'a-plain': False, 'b-loop': True}
    assert discovered['selected']['worker_id'] == 'a-plain'
    assert (await defaults.discover('owner', needs_last_frame=True))['selected']['worker_id'] == 'b-loop'
    assert (await resolver(worker(profile=STANDARD)).discover('owner', needs_last_frame=True))['selected'] is None


async def test_closed_loop_resolves_only_on_a_standard_worker_reporting_last_frame():
    pinned = {'worker_id': 'mine', 'recipe_version': describe_profile(STANDARD)['recipe_version'],
              'configuration_version': 'config-v1', 'closed_loop': True}
    with pytest.raises(ValueError, match='last-frame support'):
        await resolver(worker(profile=STANDARD)).resolve(
            'owner', MotionGenerationSettings(provider_parameters=pinned))
    settings = await resolver(last_frame_worker()).resolve(
        'owner', MotionGenerationSettings(provider_parameters=pinned))
    assert settings.provider_parameters['closed_loop'] is True
    assert settings.steps == 20


def test_unpinned_standard_closed_loop_is_still_rejected_at_parse_time():
    with pytest.raises(ValueError, match='last-frame conditioning'):
        MotionGenerationSettings(provider_parameters={'closed_loop': True})


async def _dispatch_standard(pool_worker, *, closed_loop):
    from studio.test_motion_pool import _Pool, _service_with_motion, PROJECT
    from magic_video_editor.studio.models import MotionSpecification
    pool = _Pool()
    service, jobs, dispatcher, project = await _service_with_motion(pool)
    from io import BytesIO
    from PIL import Image
    subject = BytesIO(); Image.new('RGBA', (64, 64), (200, 40, 40, 255)).save(subject, format='PNG')
    subject_revision = project.manifest.subject.revisions[-1]
    await service._assets.write_once(subject_revision.artifact_key, subject.getvalue(), 'image/png')
    project = await service.add_motion(PROJECT, project.revision,
        MotionSpecification(motion_id='idle', name='Idle', kind='preset', preset='idle', loop_policy='loop'), 'user-1')
    service.machine_defaults = resolver(replace(pool_worker, tenant_id='user-1'), tenant='user-1')
    parameters = {'worker_id': 'mine', 'recipe_version': describe_profile(STANDARD)['recipe_version'],
                  'configuration_version': 'config-v1', **({'closed_loop': True} if closed_loop else {})}
    await service.generate_motion(PROJECT, project.revision, 'idle', 'user-1', 'run',
                                  settings=MotionGenerationSettings(provider_parameters=parameters))
    return pool.submissions[0]


async def test_standard_closed_loop_job_requires_the_last_frame_feature_to_lease():
    submitted = await _dispatch_standard(last_frame_worker(), closed_loop=True)
    assert submitted.payload['images']['last_frame'] == submitted.payload['images']['first_frame']
    assert submitted.requirements.runtime_versions[LAST_FRAME_FEATURE_KEY] == '1'
    assert submitted.requirements.worker_ids == ('mine',)


async def test_ordinary_standard_jobs_never_require_the_feature():
    # A worker registered before the feature existed keeps serving ordinary jobs.
    submitted = await _dispatch_standard(worker(profile=STANDARD), closed_loop=False)
    assert LAST_FRAME_FEATURE_KEY not in submitted.requirements.runtime_versions
    assert 'last_frame' not in submitted.payload.get('images', {})
    assert set(submitted.requirements.runtime_versions) == {RECIPE_KEY, CONFIG_KEY}


@pytest.mark.parametrize(('changes', 'reason'), [
    ({'draining': True}, 'It is shutting down'),
    ({'vram_mb': 12282}, 'Motions need a 16 GB card and this one has 12 GB'),
    ({'last_heartbeat_at': datetime.now(UTC)-timedelta(minutes=3)}, 'It is offline'),
    ({'runtime_versions': {RECIPE_KEY: describe_profile(LIGHTX8)['recipe_version']}},
     'It did not report its configuration; update the worker'),
])
async def test_an_unavailable_machine_says_why(changes, reason):
    defaults = resolver(worker(**changes))
    [row] = (await defaults.discover('owner'))['workers']
    assert row['reason'] == reason
    with pytest.raises(ValueError, match=f'mine: {reason}.*Higgsfield key'):
        await defaults.resolve('owner', MotionGenerationSettings())


async def test_core_operators_share_no_compute_in_discovery():
    """Without an extension, an operator's work is their own: discovery is scoped to
    the tenant the service resolved, never widened to every machine."""
    from magic_video_editor.studio.operators import OperatorAccess

    class Admin(OperatorAccess):
        async def is_admin(self, actor_id: str) -> bool:
            return True

    async def lookup(worker_id): return None
    access = Admin(users=None, admin_emails=(), enrolment=SimpleNamespace(worker_by_id=lookup))
    async def tenant_for(actor): return None
    defaults = HostedMachineDefaults(Pool(worker('house', None), worker('theirs', 'someone')), tenant_for, access)
    assert [r['worker_id'] for r in (await defaults.discover('admin'))['workers']] == ['house']
