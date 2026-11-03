"""Optional extensions, loaded from installed packages.

The service is complete without any. A self-hosted Studio has its operators
(`MAGIC_VIDEO_EDITOR_ADMIN_EMAILS`), the machines people enroll, the house
machines and the sign-in allowlist. An extension adds what one deployment needs
beyond that; the hosted magicstudios.ai service adds tips, access requests and
its admin compute cluster this way.

An extension is a package that registers a factory in the
`magic_video_editor.extensions` entry-point group. Only the names listed in
`MAGIC_VIDEO_EDITOR_EXTENSIONS` (separated by commas or spaces) are loaded, so installing a
package changes nothing by itself, and a listed name that is not installed stops
the service from starting: a deployment that relies on an extension never runs
without it by accident. The factory receives an `ExtensionContext` and returns an
`Extension` saying what it contributes; every contribution is optional.
"""
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import APIRouter

    from magic_video_editor.contracts import RuntimeLifecycle
    from magic_video_editor.studio.auth import StudioAuthStore
    from magic_video_editor.studio.enrolment import StudioEnrolmentStore
    from magic_video_editor.studio.operators import OperatorAccess

ENTRY_POINT_GROUP = "magic_video_editor.extensions"
EXTENSIONS_ENV = "MAGIC_VIDEO_EDITOR_EXTENSIONS"


@dataclass(frozen=True)
class ExtensionContext:
    """What an extension is built from. `database_url` is None for the in-memory
    local and test runtimes; `serves_studio` is false for the worker role, which
    serves no Studio routes."""

    values: Mapping[str, str]
    users: StudioAuthStore
    enrolment: StudioEnrolmentStore | None
    operators: OperatorAccess
    database_url: str | None = None
    serves_studio: bool = True


@dataclass(frozen=True)
class RouterContext:
    """The built services an extension's routes may use."""

    studio_service: Any
    auth: Any
    api_keys: Any
    operators: OperatorAccess | None


@dataclass(frozen=True)
class Extension:
    name: str
    # Replaces the default operator access, for example to share an operator
    # compute tenant. At most one loaded extension may set it.
    operators: OperatorAccess | None = None
    # Builds the coordinator's pool job store from the database URL, for
    # extensions that change who may lease what. At most one may set it.
    pool_jobs: Callable[[str], Any] | None = None
    # Started after, and stopped before, the core components.
    components: Sequence[RuntimeLifecycle] = ()
    # Called with the owner id when an account is deleted.
    owner_erasers: Sequence[Callable[[str], Awaitable[None]]] = ()
    # Run by the hourly retention sweep; each returns how many rows it purged,
    # reported under its key.
    purges: Mapping[str, Callable[[], Awaitable[int]]] = field(default_factory=dict)
    # Routers served by the API role, built once the Studio services exist.
    routers: Callable[[RouterContext], Iterable[APIRouter]] | None = None
    # Public route documentation for the API reference (`RouteDoc` by method and
    # path), its sections (tag name to description), and routes under /studio
    # that callers never use.
    route_docs: Mapping[tuple[str, str], Any] = field(default_factory=dict)
    route_tags: Mapping[str, str] = field(default_factory=dict)
    hidden_routes: frozenset[tuple[str, str]] = frozenset()


class ExtensionError(RuntimeError):
    pass


def requested_extensions(values: Mapping[str, str]) -> tuple[str, ...]:
    """Names separated by commas or whitespace (the image build passes a
    space-separated list)."""
    names = re.split(r"[,\s]+", values.get(EXTENSIONS_ENV, ""))
    return tuple(dict.fromkeys(name for name in names if name))


def load_extensions(context: ExtensionContext) -> tuple[Extension, ...]:
    names = requested_extensions(context.values)
    if not names:
        return ()
    available = {entry.name: entry for entry in entry_points(group=ENTRY_POINT_GROUP)}
    missing = [name for name in names if name not in available]
    if missing:
        raise ExtensionError(f"{EXTENSIONS_ENV} names extensions that are not installed: {', '.join(missing)}")
    loaded = tuple(available[name].load()(context) for name in names)
    for attribute in ("operators", "pool_jobs"):
        if sum(getattr(extension, attribute) is not None for extension in loaded) > 1:
            raise ExtensionError(f"more than one extension sets {attribute}")
    return loaded


def extension_operators(extensions: Sequence[Extension], default: OperatorAccess) -> OperatorAccess:
    return next((extension.operators for extension in extensions if extension.operators is not None), default)


class ExtensionLifecycle:
    """Starts extension components in order and stops them in reverse, for a
    runtime with no lifecycle of its own (the local Studio)."""

    def __init__(self, components: Sequence[RuntimeLifecycle]) -> None:
        self._components = tuple(components)

    async def start(self) -> None:
        for component in self._components:
            await component.start()

    async def stop(self) -> None:
        for component in reversed(self._components):
            await component.stop()
