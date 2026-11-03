import subprocess

import pytest

from magic_video_editor.studio.models import MotionCandidate, MotionSpecification, FrameSlot, LoopPolicy
from magic_video_editor.studio.quality import inspect_motion_artifact
from magic_video_editor.studio.service import motion_spec_digest
from .test_motion_references import _service_with_approved_subject


def source_video():
    return subprocess.run([
        'ffmpeg','-v','error','-f','lavfi','-i','color=c=0x00B140:s=16x16:r=24',
        '-frames:v','107','-c:v','libx264','-pix_fmt','yuv420p','-f','mp4',
        '-movflags','frag_keyframe+empty_moov','pipe:1',
    ],check=True,capture_output=True).stdout


async def test_native_source_alpha_and_preview_keep_actual_24fps_not_requested_four_seconds():
    from hashlib import sha256
    service, store, assets, project = await _service_with_approved_subject()
    project = await service.add_motion('project-1',project.revision,MotionSpecification(motion_id='walk',name='Walk',preset='walk'),'user-1')
    source = source_video()
    metadata = inspect_motion_artifact(source)
    assert metadata.frame_count == 107 and metadata.frame_rate == '24/1'
    assert metadata.time_base is not None
    assert metadata.duration_ms == pytest.approx(107 * 1000 / 24)
    # Emulate a pre-fix candidate whose duration used the requested four seconds.
    candidate = MotionCandidate(candidate_id='source',artifact_key='outputs/source.mp4',sha256=sha256(source).hexdigest(),
        duration_ms=4000,metadata=metadata, motion_spec_digest=motion_spec_digest(project.manifest.motions[0].specification))
    await assets.write_once(candidate.artifact_key,source,'video/mp4')
    project = await store.append('project-1',project.revision,project.manifest.with_motion(project.manifest.motions[0].model_copy(update={
        'candidates':(candidate,), 'selected_candidate_id':'source',
    })))
    preview = await service.download_candidate_preview('project-1','walk','source','webm','user-1')
    measured = inspect_motion_artifact(assets.assets[preview.key])
    assert measured.frame_count == 107 and measured.frame_rate == '24/1'
    assert measured.duration_ms == pytest.approx(metadata.duration_ms)
    project = await service.derive_alpha('project-1',project.revision,'walk','source','user-1')
    alpha = project.manifest.motions[0].candidates[-1]
    assert alpha.duration_ms == round(metadata.duration_ms)
    assert alpha.metadata.frame_rate == '24/1' and alpha.metadata.frame_count == 107
    assert alpha.motion_spec_digest == candidate.motion_spec_digest
    alpha_preview = await service.download_candidate_preview('project-1','walk',alpha.candidate_id,'webm','user-1')
    assert assets.assets[alpha_preview.key] == assets.assets[alpha.artifact_key]
    # Explicit operator edits retain their chosen timing rather than native timing.
    project = await service.create_motion_revision('project-1',project.revision,'walk',alpha.candidate_id,'user-1',
        ordered_frames=tuple(FrameSlot(index=i) for i in range(4)),frame_duration_ms=100,loop_policy=LoopPolicy.LOOP)
    revised = project.manifest.motions[0].candidates[-1]
    preview = await service.download_candidate_preview('project-1','walk',revised.candidate_id,'webm','user-1')
    revised_metadata = inspect_motion_artifact(assets.assets[preview.key])
    assert revised_metadata.frame_count == 4 and revised_metadata.frame_rate == '10/1'
