from __future__ import annotations

import subprocess
from pathlib import Path

from django.conf import settings

from .capabilities import mafft_capability, resolve_executable

SUPPORTED_STRATEGIES = {'auto', 'linsi', 'ginsi', 'einsi'}


class ToolUnavailableError(RuntimeError):
    pass


class ToolExecutionError(RuntimeError):
    pass


def run_mafft(
    input_path: Path,
    output_path: Path,
    *,
    strategy: str,
    threads: int,
    timeout: int,
) -> dict:
    executable = resolve_executable(settings.MAFFT_EXECUTABLE)
    if executable is None:
        raise ToolUnavailableError('MAFFT executable is not available')
    normalized_strategy = strategy.strip().lower()
    if normalized_strategy not in SUPPORTED_STRATEGIES:
        raise ValueError(
            f'MAFFT strategy must be one of: {", ".join(sorted(SUPPORTED_STRATEGIES))}'
        )
    if not 1 <= threads <= settings.MAX_TOOL_THREADS:
        raise ValueError(
            f'threads must be between 1 and {settings.MAX_TOOL_THREADS}'
        )

    strategy_arguments = {
        'auto': ['--auto'],
        'linsi': ['--localpair', '--maxiterate', '1000'],
        'ginsi': ['--globalpair', '--maxiterate', '1000'],
        'einsi': ['--genafpair', '--maxiterate', '1000'],
    }
    command = [
        executable,
        *strategy_arguments[normalized_strategy],
        '--thread',
        str(threads),
        '--inputorder',
        str(input_path),
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output_path.open('wb') as output_stream:
            completed = subprocess.run(
                command,
                stdout=output_stream,
                stderr=subprocess.PIPE,
                check=False,
                timeout=timeout,
            )
    except subprocess.TimeoutExpired as exc:
        output_path.unlink(missing_ok=True)
        raise ToolExecutionError(
            f'MAFFT exceeded the execution timeout of {timeout} seconds'
        ) from exc
    except OSError as exc:
        output_path.unlink(missing_ok=True)
        raise ToolExecutionError('MAFFT could not be started') from exc

    stderr = completed.stderr.decode('utf-8', errors='replace')
    if completed.returncode != 0:
        output_path.unlink(missing_ok=True)
        raise ToolExecutionError(f'MAFFT exited with code {completed.returncode}')
    if not output_path.is_file() or output_path.stat().st_size == 0:
        output_path.unlink(missing_ok=True)
        raise ToolExecutionError('MAFFT produced an empty alignment')
    if output_path.stat().st_size > settings.MAX_ALIGNMENT_OUTPUT_BYTES:
        output_path.unlink(missing_ok=True)
        raise ToolExecutionError(
            'MAFFT alignment exceeds the configured output size limit'
        )
    return {
        'executable': executable,
        'version': mafft_capability().get('version', ''),
        'strategy': normalized_strategy,
        'threads': threads,
        'stderr_tail': stderr[-1000:],
    }
