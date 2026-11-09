"""Read a GLB's structure with the standard library: a cheap cross-check, not a renderer.

A binary glTF 2.0 file is a 12-byte header (magic `glTF`, version 2, total length),
a JSON chunk, an optional BIN chunk and possibly chunks of other types, which
readers skip. Everything counted here comes from the JSON chunk's own tables, so no
vertex data is decoded. The engine measures the gates (watertight, UVs, floor);
Studio only checks that a file is the GLB it claims to be and what it holds.

A stored file is never loaded whole. A worker thread reads the header, the chunk
headers and the JSON chunk by byte range, and hashes the file in fixed-size pieces,
so registering a 200 MB GLB holds a few megabytes at a time.
"""

from __future__ import annotations

import asyncio
import json
import math
import struct
from collections.abc import Callable
from hashlib import sha256
from typing import Any

from magic_video_editor.assets3d.models import (
    MAX_MODEL_BYTES,
    Asset3DBounds,
    GlbMeasurement,
    ImageType,
)
from magic_video_editor.contracts import AssetStore

MAGIC = b"glTF"
JSON_CHUNK = 0x4E4F534A
BIN_CHUNK = 0x004E4942
HEADER = struct.Struct("<4sII")
CHUNK_HEADER = struct.Struct("<II")
TRIANGLES = 4
# The JSON chunk holds only the scene's tables; geometry, animation keys and
# textures live in the BIN chunk. A 66-bone character with a dozen clips needs
# about 2,400 channels, 2,400 samplers and 4,800 accessors: roughly 1.5 MB. 4 MiB
# leaves room for that and keeps a hostile chunk's parsed size near 100 MB.
MAX_JSON_BYTES = 4 * 1024 * 1024
# Objects and arrays, counted from the raw bytes before parsing. Parsed, each costs
# far more than its two bytes; a real document of that size has some 20,000.
MAX_JSON_CONTAINERS = 200_000
# glTF numbers are counts, indices and float coordinates; nothing needs more digits.
MAX_INTEGER_DIGITS = 20
# glTF 2.0 defines a JSON chunk and at most one BIN chunk; a few more leave room
# for extension chunks. Each chunk header is a storage range read, so many empty
# chunks must not multiply them.
MAX_CHUNKS = 8
# Read and hash in pieces of this size.
PIECE_BYTES = 8 * 1024 * 1024

# Reads bytes [start, end) of the file being inspected.
Reader = Callable[[int, int], bytes]


class GlbError(ValueError):
    """The bytes are not a self-contained glTF 2.0 binary this service accepts."""


def image_type_of(content: bytes) -> ImageType | None:
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if content[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return None


def _table(document: dict[str, Any], name: str) -> list[Any]:
    value = document.get(name, [])
    if not isinstance(value, list):
        raise GlbError(f"The GLB's `{name}` is not a list.")
    return value


def _accessor(value: Any, accessors: list[Any], what: str) -> dict[str, Any]:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < len(accessors):
        raise GlbError(f"The GLB refers to a {what} that does not exist.")
    accessor = accessors[value]
    if not isinstance(accessor, dict):
        raise GlbError("A GLB accessor is not an object.")
    return accessor


def _count(accessor: dict[str, Any]) -> int:
    count = accessor.get("count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise GlbError("A GLB accessor has no valid `count`.")
    return count


def _vector(value: Any) -> list[float] | None:
    if not isinstance(value, list) or len(value) != 3:
        return None
    try:
        vector = [float(v) for v in value if isinstance(v, int | float) and not isinstance(v, bool)]
    except OverflowError:
        return None
    return vector if len(vector) == 3 and all(math.isfinite(v) for v in vector) else None


def _integer(text: str) -> int:
    if len(text.lstrip("-")) > MAX_INTEGER_DIGITS:
        raise GlbError("The GLB's JSON has a number too long to be a glTF value.")
    return int(text)


def _document(json_bytes: bytes) -> dict[str, Any]:
    if json_bytes.count(b"{") + json_bytes.count(b"[") > MAX_JSON_CONTAINERS:
        raise GlbError("The GLB's JSON has more objects and arrays than a model needs.")
    try:
        document = json.loads(json_bytes.rstrip(b" \x00").decode("utf-8"), parse_int=_integer)
    except GlbError:
        raise
    except (UnicodeDecodeError, ValueError) as e:
        raise GlbError("The GLB's JSON chunk is not valid JSON.") from e
    except RecursionError as e:
        raise GlbError("The GLB's JSON is nested too deeply.") from e
    if not isinstance(document, dict):
        raise GlbError("The GLB's JSON chunk is not a glTF document.")
    return document


def _chunks(read: Reader, size: int) -> tuple[bytes, int | None]:
    """The JSON chunk's bytes and the BIN chunk's length (None when absent), reading
    only the headers and the JSON chunk."""
    if size < HEADER.size + CHUNK_HEADER.size:
        raise GlbError("The file is too short to be a GLB.")
    magic, version, length = HEADER.unpack(read(0, HEADER.size))
    if magic != MAGIC:
        raise GlbError("The file is not a binary glTF (GLB).")
    if version != 2:
        raise GlbError(f"The GLB is version {version}; only glTF 2.0 is supported.")
    if length != size:
        raise GlbError(
            f"The GLB header says {length} bytes but the file has {size}; "
            "it is truncated or has extra data."
        )
    offset = HEADER.size
    json_bytes: bytes | None = None
    bin_length: int | None = None
    position = 0
    while offset < length:
        if position >= MAX_CHUNKS:
            raise GlbError(f"The GLB has more than {MAX_CHUNKS} chunks.")
        if offset + CHUNK_HEADER.size > length:
            raise GlbError("The GLB ends inside a chunk header.")
        chunk_length, chunk_type = CHUNK_HEADER.unpack(read(offset, offset + CHUNK_HEADER.size))
        start = offset + CHUNK_HEADER.size
        end = start + chunk_length
        if end > length:
            raise GlbError("A GLB chunk runs past the end of the file.")
        if position == 0:
            if chunk_type != JSON_CHUNK:
                raise GlbError("The GLB's first chunk is not JSON.")
            if chunk_length > MAX_JSON_BYTES:
                raise GlbError("The GLB's JSON chunk is larger than 4 MB.")
            json_bytes = read(start, end)
        elif chunk_type == BIN_CHUNK:
            if position != 1:
                raise GlbError("The GLB's BIN chunk must directly follow its JSON chunk.")
            bin_length = chunk_length
        elif chunk_type == JSON_CHUNK:
            raise GlbError("The GLB has more than one JSON chunk.")
        # Chunks of other types are extensions' business; readers skip them.
        offset, position = end, position + 1
    if json_bytes is None:
        raise GlbError("The GLB has no JSON chunk.")
    return json_bytes, bin_length


def _measure(document: dict[str, Any], bin_length: int | None) -> GlbMeasurement:
    asset = document.get("asset")
    version = asset.get("version") if isinstance(asset, dict) else None
    if not isinstance(version, str) or not version.startswith("2."):
        raise GlbError("The GLB does not declare glTF version 2.")

    for index, buffer in enumerate(_table(document, "buffers")):
        uri = buffer.get("uri") if isinstance(buffer, dict) else None
        if uri is None:
            if index != 0 or bin_length is None:
                raise GlbError("The GLB refers to a binary buffer it does not contain.")
        elif not isinstance(uri, str) or not uri.startswith("data:"):
            raise GlbError("The GLB refers to an external file; export it self-contained.")
    images = _table(document, "images")
    for image in images:
        uri = image.get("uri") if isinstance(image, dict) else None
        if uri is not None and (not isinstance(uri, str) or not uri.startswith("data:")):
            raise GlbError("The GLB refers to an external image; export it with embedded textures.")

    accessors = _table(document, "accessors")
    meshes = _table(document, "meshes")
    primitives = triangles = other = 0
    positions: dict[int, dict[str, Any]] = {}
    for mesh in meshes:
        for primitive in _table(mesh if isinstance(mesh, dict) else {}, "primitives"):
            if not isinstance(primitive, dict):
                raise GlbError("A GLB mesh primitive is not an object.")
            primitives += 1
            attributes = primitive.get("attributes")
            if not isinstance(attributes, dict):
                raise GlbError("A GLB mesh primitive has no attributes.")
            position = None
            if "POSITION" in attributes:
                position = _accessor(attributes["POSITION"], accessors, "position accessor")
                positions[attributes["POSITION"]] = position
            if primitive.get("mode", TRIANGLES) != TRIANGLES:
                other += 1
                continue
            if "indices" in primitive:
                triangles += _count(_accessor(primitive["indices"], accessors, "index accessor")) // 3
            elif position is not None:
                triangles += _count(position) // 3

    low = [math.inf] * 3
    high = [-math.inf] * 3
    for accessor in positions.values():
        least, most = _vector(accessor.get("min")), _vector(accessor.get("max"))
        if least is None or most is None:
            continue
        low = [min(a, b) for a, b in zip(low, least, strict=True)]
        high = [max(a, b) for a, b in zip(high, most, strict=True)]

    skins = _table(document, "skins")
    joints = 0
    for skin in skins:
        joints += len(_table(skin if isinstance(skin, dict) else {}, "joints"))
    return GlbMeasurement(
        meshes=len(meshes),
        primitives=primitives,
        triangles=triangles,
        other_primitives=other,
        vertices=sum(_count(accessor) for accessor in positions.values()),
        nodes=len(_table(document, "nodes")),
        materials=len(_table(document, "materials")),
        textures=len(_table(document, "textures")),
        images=len(images),
        skins=len(skins),
        joints=joints,
        animations=len(_table(document, "animations")),
        bounds=Asset3DBounds(min=low, max=high) if math.isfinite(low[0]) else None,
    )


def inspect_stored(read: Reader, size: int, *, max_bytes: int = MAX_MODEL_BYTES) -> GlbMeasurement:
    """Validate a GLB and count what it holds, reading it through `read`.

    `triangles` counts triangle-list primitives only (mode 4, the default): the
    indices accessor's count / 3, or POSITION's count / 3 when unindexed. Other
    modes are counted in `other_primitives`. `vertices` sums each distinct POSITION
    accessor once. `joints` sums every skin's joint list. `bounds` is the union of
    the POSITION accessors' declared min/max in each mesh's own space: node
    transforms (translation, rotation, scale, parents) are not applied, so for a
    scene that places meshes with nodes it is not the world-space box.

    The file must be self-contained: buffers and images that point at another file
    are refused, since neither the viewer nor a release pack could follow them.
    Anything malformed is a `GlbError`, never another exception.
    """
    if size > max_bytes:
        raise GlbError(f"The GLB is larger than {max_bytes // (1024 * 1024)} MB.")
    json_bytes, bin_length = _chunks(read, size)
    try:
        return _measure(_document(json_bytes), bin_length)
    except GlbError:
        raise
    except (TypeError, ValueError, AttributeError, KeyError, IndexError, OverflowError, RecursionError) as e:
        # A shape the checks above did not foresee is still the file's fault.
        raise GlbError("The GLB's JSON is not a glTF document Studio can read.") from e


def inspect_glb(content: bytes, *, max_bytes: int = MAX_MODEL_BYTES) -> GlbMeasurement:
    """`inspect_stored` for bytes already in memory."""
    return inspect_stored(lambda start, end: content[start:end], len(content), max_bytes=max_bytes)


def digest(read: Reader, size: int) -> str:
    """The file's SHA-256, read in pieces."""
    hasher = sha256()
    for start in range(0, size, PIECE_BYTES):
        hasher.update(read(start, min(start + PIECE_BYTES, size)))
    return hasher.hexdigest()


class Assets3DMedia:
    """Stored uploads are read, hashed and inspected in a worker thread, two at a
    time. The slot is taken before anything is read, so requests waiting for it
    hold no file bytes."""

    def __init__(self) -> None:
        self.slots = asyncio.Semaphore(2)

    @staticmethod
    def _reader(assets: AssetStore, key: str, size: int, loop: asyncio.AbstractEventLoop) -> Reader:
        def read(start: int, end: int) -> bytes:
            if start >= end:
                return b""
            future = asyncio.run_coroutine_threadsafe(assets.read_range(key, start, end - 1), loop)
            content = future.result()
            if len(content) != end - start:
                raise GlbError("The stored file could not be read completely.")
            return content

        return read

    async def _run[T](self, assets: AssetStore, key: str, size: int, work: Callable[[Reader], T]) -> T:
        async with self.slots:
            read = self._reader(assets, key, size, asyncio.get_running_loop())
            return await asyncio.to_thread(work, read)

    async def glb(self, assets: AssetStore, key: str, size: int) -> tuple[str, GlbMeasurement]:
        """The stored GLB's SHA-256 and measurements; `GlbError` when it is not one."""

        def work(read: Reader) -> tuple[str, GlbMeasurement]:
            if read(0, min(4, size)) != MAGIC:
                raise GlbError("The file is not a binary glTF (GLB).")
            measured = inspect_stored(read, size)
            return digest(read, size), measured

        return await self._run(assets, key, size, work)

    async def image(self, assets: AssetStore, key: str, size: int) -> tuple[str, ImageType | None]:
        """The stored picture's SHA-256 and the type its bytes say it is."""

        def work(read: Reader) -> tuple[str, ImageType | None]:
            return digest(read, size), image_type_of(read(0, min(12, size)))

        return await self._run(assets, key, size, work)

