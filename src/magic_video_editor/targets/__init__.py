"""The target adapter contract: everything a downstream game needs from the
studio, and everything the studio hands back, in one pluggable interface.

Nothing in this module or its callers may know the name of a downstream
game. A reference adapter ships in this repository, discovered the same way
a third-party adapter would be: through the `magic_video_editor.targets`
entry-point group.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from importlib.metadata import EntryPoint, entry_points
from typing import Protocol, runtime_checkable

ENTRY_POINT_GROUP = "magic_video_editor.targets"

_ENV_PREFIX = "MAGIC_VIDEO_EDITOR_TARGET_"


@dataclass(frozen=True)
class TargetSlot:
    """One slot in a target's own vocabulary. `accepts` lists every neutral
    `semantic_slot` value that maps onto this target slot; `winner` names
    which of those is authoritative when more than one is present at once."""

    name: str
    accepts: tuple[str, ...]
    winner: str

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("target slot name must not be empty")
        if not self.accepts:
            raise ValueError("target slot must accept at least one neutral slot")
        if self.winner not in self.accepts:
            raise ValueError("target slot winner must be one of its accepted slots")


@dataclass(frozen=True)
class TargetDescriptor:
    """A target's identity: what it is called, what it can be bound to, and
    the vocabulary of slots it understands. `package_version` names the
    format version of the archive members `export_files` produces."""

    system: str
    display_name: str
    entity_kinds: tuple[str, ...]
    slots: tuple[TargetSlot, ...]
    package_version: str = "1"

    def __post_init__(self) -> None:
        if not self.system:
            raise ValueError("target descriptor system must not be empty")
        if not self.entity_kinds:
            raise ValueError("target descriptor must declare at least one entity kind")

    def slot_for(self, semantic_slot: str) -> TargetSlot | None:
        for slot in self.slots:
            if semantic_slot in slot.accepts:
                return slot
        return None


@dataclass(frozen=True)
class NeutralMotion:
    """One released motion, in the studio's own neutral vocabulary -- never a
    target-specific shape. `sheet_bytes` is the already-packed PNG strip
    (order/holds/facing baked in) an adapter would reference by
    `sheet_member_path`."""

    motion_id: str
    semantic_slot: str | None
    animation_type: str
    candidate_id: str
    sha256: str
    frame_duration_ms: int
    frames: tuple[int, ...]
    columns: int
    frame_width: int
    frame_height: int
    loop_mode: str
    ping_pong: bool
    sheet_member_path: str
    sheet_bytes: bytes
    pivot_x: int | None = None
    pivot_y: int | None = None


@dataclass(frozen=True)
class NeutralExternalSubject:
    entity_kind: str
    entity_id: str
    display_name: str


@dataclass(frozen=True)
class NeutralPackage:
    """The studio's own PNG+JSON v2 payload. Adapters read it; they never see
    `AnimationProjectManifest`."""

    release_id: str
    project_id: str
    manifest_digest: str
    release_digest: str
    motions: tuple[NeutralMotion, ...]
    external_subject: NeutralExternalSubject | None = None


@dataclass(frozen=True)
class TargetConfig:
    url: str
    token: str


@dataclass(frozen=True)
class DeliveryRequest:
    release_id: str
    system: str
    archive: bytes
    config: TargetConfig


class DeliveryOutcome(StrEnum):
    DELIVERED = "delivered"
    FAILED = "failed"


type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)


@dataclass(frozen=True)
class DeliveryReceipt:
    """What a target reported back, in a shape the studio core can store and
    render without knowing anything about the target. `summary` is a short
    human-readable line ("6 sheets, 6 bindings"); `details` is the target's
    own structured response, opaque JSON as far as the studio is concerned."""

    summary: str
    details: Mapping[str, JsonValue]


@dataclass(frozen=True)
class DeliveryResult:
    """`delivered` results carry an adapter-neutral `DeliveryReceipt`; `failed`
    results carry a `message` and whether the caller should retry. Adapters
    never raise out of `deliver` -- every failure is one of these."""

    outcome: DeliveryOutcome
    receipt: DeliveryReceipt | None = None
    message: str | None = None
    retryable: bool = False

    @classmethod
    def delivered(cls, receipt: DeliveryReceipt) -> DeliveryResult:
        return cls(outcome=DeliveryOutcome.DELIVERED, receipt=receipt)

    @classmethod
    def failed(cls, message: str, *, retryable: bool) -> DeliveryResult:
        return cls(outcome=DeliveryOutcome.FAILED, message=message, retryable=retryable)

    @property
    def is_delivered(self) -> bool:
        return self.outcome is DeliveryOutcome.DELIVERED


@dataclass(frozen=True)
class Handoff:
    entity_kind: str
    entity_id: str
    display_name: str
    semantic_slot: str | None


@runtime_checkable
class TargetAdapter(Protocol):
    descriptor: TargetDescriptor

    def slot_for(self, semantic_slot: str) -> TargetSlot | None: ...

    def export_files(self, package: NeutralPackage) -> Mapping[str, bytes]:
        """Extra files the adapter wants in the export zip, under `<system>/`;
        the neutral files are always present and never rewritten."""
        ...

    async def deliver(self, delivery: DeliveryRequest) -> DeliveryResult: ...

    def parse_handoff(self, params: Mapping[str, str]) -> Handoff | None:
        """Parse and validate an inbound handoff URL's parameters -- a
        `{target, entity_kind, entity_id, name, slot}` query -- into a
        `Handoff`, or return `None`. Returns `None` for an unknown entity
        kind or slot -- an adapter never invents a value it was not given."""
        ...


class TargetRegistry:
    """Adapters, keyed by `descriptor.system`. Populated either by discovering
    installed entry points (`from_entry_points`) or explicit `register` calls,
    the latter primarily for tests."""

    def __init__(self) -> None:
        self._adapters: dict[str, TargetAdapter] = {}

    def register(self, adapter: TargetAdapter) -> None:
        self._adapters[adapter.descriptor.system] = adapter

    def get(self, system: str) -> TargetAdapter | None:
        return self._adapters.get(system)

    def __contains__(self, system: str) -> bool:
        return system in self._adapters

    def systems(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def adapters(self) -> tuple[TargetAdapter, ...]:
        return tuple(self._adapters[system] for system in self.systems())

    def config_for(
        self, system: str, *, env: Mapping[str, str] | None = None
    ) -> TargetConfig | None:
        """Reads `MAGIC_VIDEO_EDITOR_TARGET_<SYSTEM>_URL` and `..._TOKEN`. Both
        are required -- a partial pair is treated as unconfigured rather than
        guessed at."""
        source = env if env is not None else os.environ
        prefix = f"{_ENV_PREFIX}{system.upper()}_"
        url = source.get(f"{prefix}URL")
        token = source.get(f"{prefix}TOKEN")
        if not url or not token:
            return None
        return TargetConfig(url=url, token=token)

    def delivery_configured(
        self, system: str, *, env: Mapping[str, str] | None = None
    ) -> bool:
        return self.config_for(system, env=env) is not None

    @classmethod
    def from_entry_points(cls, *, group: str = ENTRY_POINT_GROUP) -> TargetRegistry:
        registry = cls()
        for entry_point in entry_points(group=group):
            registry.register(_load_adapter(entry_point))
        return registry


def _load_adapter(entry_point: EntryPoint) -> TargetAdapter:
    loaded = entry_point.load()
    adapter = loaded() if callable(loaded) else loaded
    if not isinstance(adapter, TargetAdapter):
        raise TypeError(
            f"entry point {entry_point.name!r} did not produce a TargetAdapter"
        )
    return adapter
