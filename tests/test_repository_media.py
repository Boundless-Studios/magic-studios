"""Art and media stay out of Git.

The hosted site's media come from a pinned private release at deploy time
(scripts/site_media.json, scripts/stage_site_media.py), and everything removed
for open-sourcing is preserved in the private `art-archive-2026-11-02` release.
A new image, video or sound needs a deliberate edit to the allowlist below.
"""
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Moved to releases on 2026-11-02; .gitignore keeps them out as well.
REMOVED = (
    'web/public/experiments/*',
    'web/public/wyrm/*',
    'docs/proofs/*',
    'web/src/features/welcome/media/*',
    'web/sticky-*.png',
)
MEDIA_SUFFIXES = frozenset({'.mp4', '.webm', '.gif', '.jpg', '.jpeg', '.png', '.webp', '.mov', '.wav', '.mp3', '.ogg'})
# Exact paths, never globs, for a small test fixture or icon that must stay in Git. Empty:
# the Playwright flows draw their character upload (web/e2e/subjectImage.ts).
ALLOWED_MEDIA: frozenset[str] = frozenset()


def _tracked() -> list[str]:
    if shutil.which('git') is None:
        pytest.skip('git is not available')
    result = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, capture_output=True, check=False)
    if result.returncode != 0:
        pytest.skip('not a git checkout: ' + result.stderr.decode(errors='replace').strip())
    return [path for path in result.stdout.decode().split('\0') if path]


def test_removed_art_paths_stay_untracked():
    tracked = _tracked()
    assert tracked, 'git ls-files listed nothing'
    assert [path for path in tracked if any(fnmatchcase(path, pattern) for pattern in REMOVED)] == []


def test_no_media_is_tracked_outside_the_allowlist():
    tracked = _tracked()
    media = {path for path in tracked if PurePosixPath(path).suffix.lower() in MEDIA_SUFFIXES}
    assert sorted(media - ALLOWED_MEDIA) == [], 'media belong in a private release, not Git'
    # A stale entry would quietly allow a file to come back under that name.
    assert sorted(ALLOWED_MEDIA - media) == []
