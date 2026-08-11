from __future__ import annotations

import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from django.conf import settings


def resolve_executable(configured_value: str) -> str | None:
    candidate = configured_value.strip()
    if not candidate:
        return None
    path = Path(candidate)
    if path.is_absolute():
        return str(path) if path.is_file() else None
    return shutil.which(candidate)


@lru_cache(maxsize=8)
def _probe(executable: str, version_argument: str) -> dict:
    resolved = resolve_executable(executable)
    if resolved is None:
        return {
            'available': False,
            'reason': 'executable_not_found',
        }
    try:
        completed = subprocess.run(
            [resolved, version_argument],
            capture_output=True,
            check=False,
            text=True,
            timeout=settings.TOOL_PROBE_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            'available': False,
            'reason': type(exc).__name__,
        }
    version = (completed.stdout or completed.stderr).strip().splitlines()
    return {
        'available': completed.returncode == 0,
        'version': version[0][:200] if version else '',
        'reason': '' if completed.returncode == 0 else 'version_probe_failed',
    }


def mafft_capability() -> dict:
    return _probe(settings.MAFFT_EXECUTABLE, '--version')


def fasttree_capability() -> dict:
    return _probe(settings.FASTTREE_EXECUTABLE, '-help')
