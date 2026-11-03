"""Which executors can run a frame-pinned leg, checked against real templates."""

from dataclasses import replace
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from outbound_gpu_worker_pool.comfy import ComfyWorkflowPlugin, TemplateRegistry
from outbound_gpu_worker_pool.plugins import PluginRequestRejected

from magic_video_editor import desktop_studio as desktop
from magic_video_editor.flights import media
from magic_video_editor.local_studio import LocalAssetStore

PRESETS = Path(__file__).resolve().parents[2] / "local" / "presets"


def pinned_pool_registry() -> TemplateRegistry:
    return TemplateRegistry.from_directory(Path(str(files("outbound_gpu_worker_pool") / "templates")))


def flight_lease():
    first, last = "outputs/movies/f/takes/t.first-768x448.png", "outputs/movies/f/takes/t.last-768x448.png"
    return SimpleNamespace(
        capability_id=media.CAPABILITY_ID, contract_version=1,
        payload={"prompt": "Fly.", "width": 768, "height": 448, "length": 124, "steps": 20, "seed": 1, "fps": 24,
                 "images": {"first_frame": first, "last_frame": last}},
        input_keys=(first, last),
    )


@pytest.mark.parametrize("preset", ["balanced", "quality", "cache", "sol"])
def test_desktop_presets_bind_the_last_frame(preset):
    registry = TemplateRegistry.from_directory(PRESETS / preset)
    assert media.registry_supports_scene_flights(registry)
    graph = registry.template(media.CAPABILITY_ID).graph
    assert graph["104"]["class_type"] == "MiniMaxH3ImageToVideo"
    assert graph["104"]["inputs"]["last_frame"] == ["201", 0]
    # The base 20-step graph: no LightX2V adapter on the flight capability.
    assert graph["9"]["inputs"]["steps"] == 20
    assert not any(node["class_type"] == "LoraLoaderModelOnly" for node in graph.values())


def test_pinned_standard_worker_template_pins_the_last_frame():
    assert media.pinned_pool_supports_scene_flights() is True
    template = pinned_pool_registry().template(media.CAPABILITY_ID)
    slots = {slot.name: slot for slot in template.image_slots}
    assert slots["last_frame"].node_id == "201" and not slots["last_frame"].required
    graph = template.graph
    assert graph["201"]["class_type"] == "LoadImage"
    assert graph["104"]["class_type"] == "MiniMaxH3ImageToVideo"
    assert graph["104"]["inputs"]["last_frame"] == ["201", 0]
    # The standard 20-step base graph, as the flight recipe states.
    assert graph["9"]["inputs"]["steps"] == media.STEPS
    assert not any(node["class_type"] == "LoraLoaderModelOnly" for node in graph.values())


async def test_a_hosted_worker_validates_a_two_pin_flight_job():
    async with httpx.AsyncClient() as client:
        plugin = ComfyWorkflowPlugin(pinned_pool_registry(), base_url="http://127.0.0.1:1", client=client)
        validated = plugin.validate(flight_lease())
    assert validated.inputs["images"] == flight_lease().payload["images"]
    assert (validated.inputs["width"], validated.inputs["height"], validated.inputs["length"]) == (768, 448, 124)


async def test_balanced_desktop_validates_a_flight_job_and_reports_it(monkeypatch):
    monkeypatch.setenv("MAGIC_DESKTOP_PRESET", "balanced")
    monkeypatch.setenv("MAGIC_DESKTOP_ATTENTION", "ck")
    pool = desktop.DesktopPool(LocalAssetStore())
    validated = pool.plugin.validate(flight_lease())
    assert set(validated.inputs["images"]) == {"first_frame", "last_frame"}
    await pool.plugin._client.aclose()
    app = desktop.create_desktop_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
        config = (await client.get("/studio/generation-config")).json()
        caps = (await client.get("/studio/scene-flights/capabilities")).json()
    assert config["scene_flight"]["available"] is True
    assert (config["scene_flight"]["width"], config["scene_flight"]["height"]) == (768, 448)
    assert caps["generation_available"] is True


async def test_a_template_without_the_slot_rejects_the_flight():
    standard = pinned_pool_registry().template(media.CAPABILITY_ID)
    first_frame_only = replace(
        standard, image_slots=tuple(s for s in standard.image_slots if s.name != media.LAST_FRAME_SLOT)
    )
    registry = TemplateRegistry([first_frame_only])
    assert not media.registry_supports_scene_flights(registry)
    async with httpx.AsyncClient() as client:
        plugin = ComfyWorkflowPlugin(registry, base_url="http://127.0.0.1:1", client=client)
        with pytest.raises(PluginRequestRejected):
            plugin.validate(flight_lease())


async def test_turbo_offers_no_flights(monkeypatch):
    monkeypatch.setenv("MAGIC_DESKTOP_PRESET", "turbo")
    app = desktop.create_desktop_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
        config = (await client.get("/studio/generation-config")).json()
    assert config["scene_flight"]["available"] is False
