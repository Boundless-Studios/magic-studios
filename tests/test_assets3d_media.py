"""GLB structure reading: the header, chunks and counts, with hand-built files."""

from __future__ import annotations

import json
import struct

import pytest

from magic_video_editor.assets3d.media import GlbError, inspect_glb


def glb(document: dict | None = None, binary: bytes | None = b"\0" * 8, *, extra: bytes = b"") -> bytes:
    """A binary glTF 2.0 file with this JSON document and an optional BIN chunk."""
    if document is None:
        document = triangle()
    payload = json.dumps(document).encode()
    payload += b" " * (-len(payload) % 4)
    chunks = struct.pack("<II", len(payload), 0x4E4F534A) + payload
    if binary is not None:
        binary += b"\0" * (-len(binary) % 4)
        chunks += struct.pack("<II", len(binary), 0x004E4942) + binary
    chunks += extra
    return struct.pack("<4sII", b"glTF", 2, 12 + len(chunks)) + chunks


def triangle(**overrides) -> dict:
    """One indexed triangle list of 4 triangles over 6 vertices, plus an unindexed
    mesh of 2 triangles, a line strip, one skin and two animations."""
    document = {
        "asset": {"version": "2.0"},
        "buffers": [{"byteLength": 8}],
        "accessors": [
            {"count": 6, "type": "VEC3", "componentType": 5126, "min": [-1, 0, -0.5], "max": [1, 2, 0.5]},
            {"count": 12, "type": "SCALAR", "componentType": 5123},
            {"count": 6, "type": "VEC3", "componentType": 5126, "min": [0, -3, 0], "max": [4, 1, 1]},
            {"count": 5, "type": "VEC3", "componentType": 5126},
        ],
        "meshes": [
            {"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]},
            {"primitives": [
                {"attributes": {"POSITION": 2}, "mode": 4},
                {"attributes": {"POSITION": 3}, "mode": 3},
            ]},
        ],
        "nodes": [{"mesh": 0}, {"mesh": 1}, {}, {}],
        "materials": [{}],
        "textures": [{"source": 0}],
        "images": [{"bufferView": 0, "mimeType": "image/png"}],
        "skins": [{"joints": [2, 3]}],
        "animations": [{"channels": [], "samplers": []}, {"channels": [], "samplers": []}],
    }
    document.update(overrides)
    return document


def test_counts_indexed_and_unindexed_triangles_skins_and_animations():
    measured = inspect_glb(glb())
    assert measured.meshes == 2
    assert measured.primitives == 3
    # 12 indices / 3 + 6 unindexed positions / 3; the line strip is not a face.
    assert measured.triangles == 4 + 2
    assert measured.other_primitives == 1
    assert measured.vertices == 6 + 6 + 5
    assert (measured.nodes, measured.materials, measured.textures, measured.images) == (4, 1, 1, 1)
    assert (measured.skins, measured.joints, measured.animations) == (1, 2, 2)
    # The union of POSITION min/max; the accessor without bounds is skipped.
    assert measured.bounds.min == [-1, -3, -0.5]
    assert measured.bounds.max == [4, 2, 1]


def test_a_shared_position_accessor_counts_its_vertices_once():
    document = triangle(meshes=[{"primitives": [
        {"attributes": {"POSITION": 0}, "indices": 1},
        {"attributes": {"POSITION": 0}},
    ]}])
    measured = inspect_glb(glb(document))
    assert measured.vertices == 6
    assert measured.triangles == 4 + 2


def test_a_json_only_glb_with_no_meshes_is_valid():
    measured = inspect_glb(glb({"asset": {"version": "2.0"}}, binary=None))
    assert measured.meshes == measured.triangles == measured.vertices == 0
    assert measured.bounds is None


def test_unknown_chunks_after_bin_are_skipped():
    extra = struct.pack("<II", 4, 0x12345678) + b"abcd"
    assert inspect_glb(glb(extra=extra)).triangles == 6


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"", "too short"),
        (b"PK\x03\x04" + b"\0" * 40, "not a binary glTF"),
        (b"glTF" + struct.pack("<II", 1, 40) + b"\0" * 28, "only glTF 2.0"),
    ],
)
def test_not_a_glb_is_refused(content, message):
    with pytest.raises(GlbError, match=message):
        inspect_glb(content)


def test_a_truncated_or_padded_file_is_refused():
    content = glb()
    with pytest.raises(GlbError, match="truncated"):
        inspect_glb(content[:-4])
    with pytest.raises(GlbError, match="truncated"):
        inspect_glb(content + b"\0\0\0\0")
    # The header agrees with the file, but a chunk claims more than is there.
    lying = bytearray(content)
    struct.pack_into("<I", lying, 12, 10_000)
    with pytest.raises(GlbError, match="runs past the end"):
        inspect_glb(bytes(lying))


def test_the_first_chunk_must_be_json_and_valid():
    binary_first = struct.pack("<II", 4, 0x004E4942) + b"\0" * 4
    with pytest.raises(GlbError, match="first chunk is not JSON"):
        inspect_glb(struct.pack("<4sII", b"glTF", 2, 12 + len(binary_first)) + binary_first)
    bad = b"{not json"
    bad += b" " * (-len(bad) % 4)
    chunk = struct.pack("<II", len(bad), 0x4E4F534A) + bad
    with pytest.raises(GlbError, match="not valid JSON"):
        inspect_glb(struct.pack("<4sII", b"glTF", 2, 12 + len(chunk)) + chunk)
    with pytest.raises(GlbError, match="version 2"):
        inspect_glb(glb({"asset": {"version": "1.0"}}))


def test_oversized_input_is_refused_before_parsing():
    with pytest.raises(GlbError, match="larger than"):
        inspect_glb(glb(), max_bytes=16)


def test_dangling_references_and_external_files_are_refused():
    with pytest.raises(GlbError, match="does not exist"):
        inspect_glb(glb(triangle(meshes=[{"primitives": [{"attributes": {"POSITION": 9}}]}])))
    with pytest.raises(GlbError, match="does not exist"):
        inspect_glb(glb(triangle(meshes=[{"primitives": [{"attributes": {"POSITION": 0}, "indices": True}]}])))
    with pytest.raises(GlbError, match="external file"):
        inspect_glb(glb(triangle(buffers=[{"byteLength": 8, "uri": "model.bin"}])))
    with pytest.raises(GlbError, match="external image"):
        inspect_glb(glb(triangle(images=[{"uri": "albedo.png"}])))
    # Buffer 0 without a uri is the BIN chunk, which must then exist.
    with pytest.raises(GlbError, match="does not contain"):
        inspect_glb(glb(binary=None))
    with pytest.raises(GlbError, match="valid `count`"):
        inspect_glb(glb(triangle(accessors=[{"count": -1}, {"count": 3}, {"count": 3}, {"count": 3}])))


def raw_glb(json_text: str) -> bytes:
    """A GLB around JSON text exactly as given, valid or not."""
    payload = json_text.encode()
    payload += b" " * (-len(payload) % 4)
    chunk = struct.pack("<II", len(payload), 0x4E4F534A) + payload
    return struct.pack("<4sII", b"glTF", 2, 12 + len(chunk)) + chunk


@pytest.mark.parametrize(
    ("json_text", "message"),
    [
        # A non-object accessor behind POSITION on a line primitive.
        ('{"asset":{"version":"2.0"},"accessors":[[1]],'
         '"meshes":[{"primitives":[{"attributes":{"POSITION":0},"mode":1}]}]}', "not an object"),
        # A 400-digit integer in min, which float() cannot hold.
        ('{"asset":{"version":"2.0"},"accessors":[{"count":3,"min":[' + "9" * 400 + ',0,0],"max":[1,1,1]}],'
         '"meshes":[{"primitives":[{"attributes":{"POSITION":0}}]}]}', "too long"),
        # Past Python's integer-string limit, which is not a JSONDecodeError.
        ('{"asset":{"version":"2.0"},"x":' + "9" * 5000 + "}", "too long"),
        # Nesting deep enough to exhaust the parser's recursion, under the container cap.
        ('{"asset":{"version":"2.0"},"x":' + "[" * 100_000 + "]" * 100_000 + "}", "nested too deeply"),
        # More containers than any model needs, refused before parsing.
        ('{"asset":{"version":"2.0"},"nodes":[' + "{}," * 200_000 + "{}]}", "more objects and arrays"),
    ],
)
def test_malformed_json_is_a_glb_error_never_a_crash(json_text, message):
    with pytest.raises(GlbError, match=message):
        inspect_glb(raw_glb(json_text))


def test_an_overflowing_float_bound_is_ignored():
    measured = inspect_glb(raw_glb(
        '{"asset":{"version":"2.0"},"accessors":[{"count":3,"min":[1e999999,0,0],"max":[1,1,1]}],'
        '"meshes":[{"primitives":[{"attributes":{"POSITION":0}}]}]}'
    ))
    assert measured.triangles == 1 and measured.bounds is None


def test_the_json_chunk_is_limited_to_4_mb():
    filler = "x" * (4 * 1024 * 1024)
    with pytest.raises(GlbError, match="larger than 4 MB"):
        inspect_glb(raw_glb('{"asset":{"version":"2.0"},"extras":"' + filler + '"}'))


def test_a_stored_glb_is_read_by_headers_and_json_only_and_hashed_in_pieces(monkeypatch):
    from hashlib import sha256

    from magic_video_editor.assets3d import media

    monkeypatch.setattr(media, "PIECE_BYTES", 1024)
    content = glb(binary=b"\1" * 10_000)
    reads: list[tuple[int, int]] = []

    def read(start, end):
        reads.append((start, end))
        return content[start:end]

    measured = media.inspect_stored(read, len(content))
    assert measured.triangles == 6
    # The header, the two chunk headers and the JSON chunk; never the BIN payload.
    assert max(end - start for start, end in reads) < 2_000
    assert all(end <= len(content) - 10_000 for _, end in reads)
    reads.clear()
    assert media.digest(read, len(content)) == sha256(content).hexdigest()
    assert max(end - start for start, end in reads) == 1024


def test_many_empty_chunks_are_refused_after_a_few_reads():
    empty = struct.pack("<II", 0, 0x12345678)
    content = glb(extra=empty * 10_000)
    reads: list[tuple[int, int]] = []

    def read(start, end):
        reads.append((start, end))
        return content[start:end]

    from magic_video_editor.assets3d.media import inspect_stored

    with pytest.raises(GlbError, match="more than 8 chunks"):
        inspect_stored(read, len(content))
    # The header, eight chunk headers and the JSON chunk: not one read per chunk.
    assert len(reads) <= 10
    # A few extension chunks are still fine.
    assert inspect_glb(glb(extra=empty * 6)).triangles == 6
