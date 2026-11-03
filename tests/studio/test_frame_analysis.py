import asyncio
import base64
import io
import math

from PIL import Image, ImageDraw

from fastapi.testclient import TestClient

from magic_video_editor.app import create_app
from magic_video_editor.memory import InMemoryRuntime, MemoryAssetStore, MemoryJobStore
from magic_video_editor.studio.frame_analysis import analyze_candidate_frames, suggest_cycle
from magic_video_editor.studio.models import MediaKind


def analysis(values, *, clipped=()):
    return dict(candidate_id='candidate', source_sha256='a' * 64, fps=24,
                frames=[dict(pose=[v] * 768, edge_contact=i in clipped) for i, v in enumerate(values)])


def test_walk_selects_one_stride_and_excludes_matching_endpoint():
    result = suggest_cycle(analysis([.1 + .08 * math.sin(i * 2 * math.pi / 24) for i in range(75)]), 'walk', 12)
    assert result['status'] == 'complete'
    assert result['end'] - result['start'] == 24
    assert len(result['ordered_frames']) == 12
    assert result['ordered_frames'][-1] < result['end']
    assert result['frame_duration_ms'] == 83
    assert result['loop_policy'] == 'loop'
    assert result['visual_review_required'] is True


def test_attack_requires_recovery_and_flags_edge_contact():
    incomplete = [.1] * 6 + [.1 + i * .01 for i in range(20)] + [.3] * 30
    proposal = suggest_cycle(analysis(incomplete), 'attack', 12)
    assert proposal['status'] == 'incomplete'
    assert proposal['loop_policy'] == 'once'
    # A little anticipation adds path length beyond a simple out-and-back.
    complete = [.1] * 3 + [.13, .1] + [.1 + i * .01 for i in range(16)] + [.25 - i * .01 for i in range(16)] + [.1] * 8
    result = suggest_cycle(analysis(complete), 'attack', 12)
    assert result['status'] == 'complete'
    assert result['loop_policy'] == 'once'
    clipped = suggest_cycle(analysis(complete, clipped=(15,)), 'attack', 12)
    assert clipped['status'] == 'incomplete'
    assert clipped['edge_clipped_frames'] == [15]
    assert clipped['loop_policy'] == 'once'
    assert clipped['ordered_frames']


def test_attack_matches_the_weapon_and_body_across_a_later_single_strike():
    """A coarse pose can miss the axe; the first cycle ends with it displaced."""
    def frame(x, y):
        image = Image.new('RGBA', (64, 64))
        draw = ImageDraw.Draw(image)
        draw.rectangle((27, 12, 38, 57), fill=(150, 90, 45, 255))
        draw.rectangle((x, y, x + 13, y + 13), fill=(225, 225, 225, 255))
        output = io.BytesIO()
        image.save(output, format='PNG')
        return dict(pose=[.1] * 768,
                    image='data:image/png;base64,' + base64.b64encode(output.getvalue()).decode(),
                    edge_contact=False)

    locations = {}
    for start, raised, struck, end in ((0, 8, 15, 24), (30, 38, 45, 54)):
        for index in range(start, end + 1):
            if index <= raised:
                progress = (index - start) / (raised - start)
                x, y = 8 + 4 * progress, 28 - 23 * progress
            elif index <= struck:
                progress = (index - raised) / (struck - raised)
                x, y = 12 + 27 * progress, 5 + 34 * progress
            else:
                progress = (index - struck) / (end - struck)
                x, y = 39 + (-31 if start == 30 else 1) * progress, 39 - 11 * progress
            locations[index] = round(x), round(y)
    for index in range(25, 30):
        locations[index] = 40 - (index - 24) * 5, 28
    sample = dict(candidate_id='candidate', source_sha256='a' * 64,
                  fps=24, width=64, height=64,
                  frames=[frame(*locations[index]) for index in range(55)])
    result = suggest_cycle(sample, 'attack', 16)
    assert result['status'] == 'complete'
    assert (result['start'], result['end']) == (30, 54)
    assert result['ordered_frames'][-1] < result['end']
    assert not any('longer' in warning.lower() for warning in result['warnings'])


def test_attack_falls_back_to_long_cycle_only_with_recovery():
    values = [(0, 0)] * 18
    values += [(.2 * i / 22, 0) for i in range(1, 23)]
    values += [(.2, .02 * (i % 2)) for i in range(32)]
    values += [(.2, .2 * i / 16) for i in range(1, 17)]
    values += [(.2 * (1 - i / 17),) * 2 for i in range(1, 18)]
    values += [(0, 0)] * 2

    def sample(points, *, clipped=()):
        result = analysis([0] * len(points), clipped=clipped)
        for frame, point in zip(result['frames'], points):
            frame['pose'] = list(point) * 384
        return result

    result = suggest_cycle(sample(values), 'attack', 16)
    assert result['status'] == 'complete'
    assert result['end'] - result['start'] > round(24 * 1.8)
    assert result['ordered_frames'][-1] < result['end']
    assert result['visual_review_required'] is True
    assert any('longer' in warning.lower() for warning in result['warnings'])

    no_recovery = values[:88] + [(.2, .2)] * 19
    incomplete = suggest_cycle(sample(no_recovery), 'attack', 16)
    assert incomplete['status'] == 'incomplete'
    assert incomplete['ordered_frames'] == []

    clipped = suggest_cycle(sample(values, clipped=(70,)), 'attack', 16)
    assert clipped['status'] == 'incomplete'
    assert clipped['edge_clipped_frames'] == [70]
    assert clipped['ordered_frames']

    unknown_edges = sample(values)
    unknown_edges['frames'][70]['edge_contact'] = None
    unknown = suggest_cycle(unknown_edges, 'attack', 16)
    assert unknown['status'] == 'incomplete'
    assert any('could not be measured' in warning for warning in unknown['warnings'])



def test_body_calibration_uses_chosen_ready_frame_and_one_fixed_transform():
    import base64
    import io
    from PIL import Image, ImageDraw
    from magic_video_editor.studio.frame_analysis import calibrate_body
    def frame(size, box):
        image = Image.new('RGBA', (size, size))
        ImageDraw.Draw(image).rectangle(box, fill='red')
        out = io.BytesIO()
        image.save(out, format='PNG')
        return {'image': 'data:image/png;base64,' + base64.b64encode(out.getvalue()).decode()}
    source = dict(candidate_id='a', source_sha256='a'*64, width=100, height=100,
                  frames=[frame(100, (40, 10, 60, 40)), frame(100, (40, 20, 60, 79))])
    reference = dict(candidate_id='w', source_sha256='b'*64, width=100, height=100,
                     frames=[frame(100, (40, 50, 60, 79))])
    result = calibrate_body(source, reference, 1)
    assert result['normalization']['scale'] == .5
    assert result['normalization']['source_anchor_y'] == 79
    assert result['normalization']['target_anchor_y'] == 79
    assert result['source_frame'] == 1
    assert result['visual_review_required'] is True


def test_idle_finds_subtle_full_breath_beyond_walk_window_with_measured_timebase():
    sample = analysis([.1 + .006 * math.sin(index * 2 * math.pi / 72) for index in range(73)])
    sample.update(frame_rate='24/1', time_base='1/24000', fps=99) # rational measured source wins
    result = suggest_cycle(sample, 'idle', 12)
    assert result['status'] == 'complete'
    assert (result['start'], result['end']) == (0, 72)
    assert result['ordered_frames'] == list(range(0, 72, 6))
    assert result['frame_duration_ms'] == 250
    assert result['source_interval_ms'] == 3000
    assert result['source_frame_rate'] == '24/1' and result['source_time_base'] == '1/24000'
    assert result['visual_review_required'] is True
    assert result['loop_policy'] == 'loop'


def test_idle_rejects_frozen_hold_and_partial_breath():
    frozen = suggest_cycle(analysis([.1] * 96), 'idle', 12)
    assert frozen['status'] == 'incomplete' and frozen['ordered_frames'] == []
    assert frozen['loop_policy'] == 'loop'
    # A matching midpoint at half a breath has opposite motion across the seam.
    partial = analysis([.1 + .006 * math.sin(index * 2 * math.pi / 72) for index in range(37)])
    result = suggest_cycle(partial, 'idle', 12)
    assert result['status'] == 'incomplete' and result['ordered_frames'] == []


def test_idle_missing_edge_measurement_and_clipping_keep_proposal_incomplete():
    sample = analysis([.1 + .006 * math.sin(index * 2 * math.pi / 72) for index in range(73)])
    sample['frames'][30]['edge_contact'] = None
    unknown = suggest_cycle(sample, 'idle', 12)
    assert unknown['status'] == 'incomplete' and unknown['ordered_frames']
    assert any('could not be measured' in warning for warning in unknown['warnings'])
    sample['frames'][30]['edge_contact'] = True
    clipped = suggest_cycle(sample, 'idle', 12)
    assert clipped['status'] == 'incomplete' and clipped['edge_clipped_frames'] == [30]
    assert clipped['visual_review_required'] is True


def test_loop_suggestion_route_accepts_idle_and_rejects_unrecognized_mode():
    from magic_video_editor.studio.routes import LoopSuggestionRequest
    from pydantic import ValidationError
    import pytest
    assert LoopSuggestionRequest(mode='idle').mode == 'idle'
    with pytest.raises(ValidationError):
        LoopSuggestionRequest(mode='breathe')


def calibration_pair():
    def sample(size, body, *, weapon=False):
        image = Image.new('RGBA', (size,size))
        draw = ImageDraw.Draw(image)
        draw.rectangle(body, fill=(200,60,30,255))
        if weapon:
            draw.rectangle((0,80,36,220),fill=(80,80,80,255))
        out = io.BytesIO(); image.save(out,format='PNG')
        content = out.getvalue()
        return content, dict(candidate_id=f'candidate-{size}',source_sha256=('a' if size==512 else 'b')*64,
            width=size,height=size,frames=[{'image':'data:image/png;base64,'+base64.b64encode(content).decode()}])
    return sample(512,(222,178,265,381),weapon=True), sample(256,(112,49,149,228))


def test_registered_pivot_reparameterizes_rounded_scale_without_changing_render_pixels():
    import pytest
    from magic_video_editor.studio.frame_analysis import calibrate_body
    from magic_video_editor.studio.models import NormalizationSpec
    from magic_video_editor.studio.exports import pack_cell_sheet
    (content,source),(_,reference) = calibration_pair()
    legacy = calibrate_body(source,reference,0)
    registered = calibrate_body(source,{**reference,'registered_pivot':{'x':128,'y':224}},0)
    assert registered['method'] == 'central-body-alpha-registered-pivot-v2'
    assert registered['source_measurement'] == legacy['source_measurement']
    assert registered['reference_measurement'] == legacy['reference_measurement']
    old,new = legacy['normalization'],registered['normalization']
    assert new['scale'] == old['scale'] == .882
    assert (new['target_anchor_x'],new['target_anchor_y']) == (128,224)
    for axis in ('x','y'):
        assert new[f'target_anchor_{axis}']-new['scale']*new[f'source_anchor_{axis}'] == pytest.approx(
            old[f'target_anchor_{axis}']-old['scale']*old[f'source_anchor_{axis}'],abs=1e-12)
    before = Image.open(io.BytesIO(pack_cell_sheet([content],256,normalization=NormalizationSpec(**old))))
    after = Image.open(io.BytesIO(pack_cell_sheet([content],256,normalization=NormalizationSpec(**new))))
    assert after.size == before.size and after.tobytes() == before.tobytes()
    expansion = (after.height-256)//2
    assert expansion > 0
    assert int(after.info['studio_pivot_x']) == 128+expansion
    assert int(after.info['studio_pivot_y']) == 224+expansion
    bounds = after.getchannel('A').getbbox()
    assert min(bounds[:2]) >= 8 and max(bounds[2:]) <= after.height-8


def test_no_pivot_calibration_keeps_original_structure_and_values():
    from magic_video_editor.studio.frame_analysis import calibrate_body
    (_,source),(_,reference) = calibration_pair()
    result = calibrate_body(source,reference,0)
    assert result == dict(normalization=dict(scale=.882,source_anchor_x=244.,source_anchor_y=381.,
        target_anchor_x=131.,target_anchor_y=228.,padding=8,cleanup_green_edges=False,
        reference_candidate_id=reference['candidate_id'],reference_sha256=reference['source_sha256']),
        source_measurement=dict(height=204.,hip_x=244.,feet_y=381.,top=178.),
        reference_measurement=dict(height=180.,hip_x=131.,feet_y=228.,top=49.),
        source_candidate_id=source['candidate_id'],reference_candidate_id=reference['candidate_id'],
        source_sha256=source['source_sha256'],reference_sha256=reference['source_sha256'],
        source_frame=0,reference_frame=0,method='central-body-alpha-v1',visual_review_required=True,
        warnings=['Compare resting poses visually: weapons crossing the central body can bias this measurement.'])


def test_registered_pivot_rejects_invalid_pivot_and_out_of_bounds_virtual_source_anchor():
    import pytest
    from magic_video_editor.studio.frame_analysis import calibrate_body
    (_,source),(_,reference) = calibration_pair()
    with pytest.raises(ValueError,match='pivot is outside'):
        calibrate_body(source,{**reference,'registered_pivot':{'x':257,'y':224}},0)
    # A short reference body and distant in-frame pivot require an invalid virtual anchor.
    image = Image.new('RGBA',(256,256))
    ImageDraw.Draw(image).rectangle((112,10,149,19),fill='red')
    out=io.BytesIO(); image.save(out,format='PNG')
    tiny = {**reference,'registered_pivot':{'x':128,'y':224},
        'frames':[{'image':'data:image/png;base64,'+base64.b64encode(out.getvalue()).decode()}]}
    with pytest.raises(ValueError,match='virtual source anchor outside'):
        calibrate_body(source,tiny,0)
