from magic_video_editor.targets import TargetConfig, TargetRegistry

from .test_contract_dummy import DummyTargetAdapter


def test_explicit_registration_for_tests() -> None:
    registry = TargetRegistry()
    registry.register(DummyTargetAdapter())

    assert registry.get("dummy") is not None
    assert registry.get("unregistered-target") is None
    assert registry.systems() == ("dummy",)


def test_config_for_requires_both_url_and_token() -> None:
    registry = TargetRegistry()

    assert registry.config_for("dummy", env={}) is None
    assert (
        registry.config_for(
            "dummy",
            env={"MAGIC_VIDEO_EDITOR_TARGET_DUMMY_URL": "https://dummy.example"},
        )
        is None
    )
    assert registry.config_for(
        "dummy",
        env={
            "MAGIC_VIDEO_EDITOR_TARGET_DUMMY_URL": "https://dummy.example",
            "MAGIC_VIDEO_EDITOR_TARGET_DUMMY_TOKEN": "tok",
        },
    ) == TargetConfig(url="https://dummy.example", token="tok")


def test_delivery_configured_reflects_config_for() -> None:
    registry = TargetRegistry()
    env = {
        "MAGIC_VIDEO_EDITOR_TARGET_DUMMY_URL": "https://dummy.example",
        "MAGIC_VIDEO_EDITOR_TARGET_DUMMY_TOKEN": "tok",
    }

    assert registry.delivery_configured("dummy", env=env) is True
    assert registry.delivery_configured("dummy", env={}) is False
