from __future__ import annotations

import os
import subprocess
from pathlib import Path

from django.conf import settings

from .capabilities import fasttree_capability, resolve_executable
from .mafft import ToolExecutionError, ToolUnavailableError

SUPPORTED_DNA_MODELS = {'jc', 'gtr'}
SUPPORTED_PROTEIN_MODELS = {'jtt', 'wag', 'lg'}


def run_fasttree(
    input_path: Path,
    output_path: Path,
    *,
    alphabet: str,
    model: str,
    threads: int,
    timeout: int,
) -> dict:
    executable = resolve_executable(settings.FASTTREE_EXECUTABLE)
    if executable is None:
        raise ToolUnavailableError('FastTree executable is not available')
    normalized_alphabet = alphabet.strip().lower()
    normalized_model = model.strip().lower()
    if normalized_alphabet == 'dna':
        if normalized_model not in SUPPORTED_DNA_MODELS:
            raise ValueError('DNA FastTree model must be one of: gtr, jc')
        model_arguments = ['-nt'] + (['-gtr'] if normalized_model == 'gtr' else [])
    elif normalized_alphabet == 'protein':
        if normalized_model not in SUPPORTED_PROTEIN_MODELS:
            raise ValueError('protein FastTree model must be one of: jtt, lg, wag')
        model_arguments = [] if normalized_model == 'jtt' else [f'-{normalized_model}']
    else:
        raise ValueError('FastTree input alphabet must be dna or protein')
    if not 1 <= threads <= settings.MAX_TOOL_THREADS:
        raise ValueError(
            f'threads must be between 1 and {settings.MAX_TOOL_THREADS}'
        )

    command = [
        executable,
        '-quiet',
        '-nopr',
        *model_arguments,
        str(input_path),
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        environment = os.environ.copy()
        environment['OMP_NUM_THREADS'] = str(threads)
        with output_path.open('wb') as output_stream:
            completed = subprocess.run(
                command,
                stdout=output_stream,
                stderr=subprocess.PIPE,
                check=False,
                env=environment,
                timeout=timeout,
            )
    except subprocess.TimeoutExpired as exc:
        output_path.unlink(missing_ok=True)
        raise ToolExecutionError(
            f'FastTree exceeded the execution timeout of {timeout} seconds'
        ) from exc
    except OSError as exc:
        output_path.unlink(missing_ok=True)
        raise ToolExecutionError('FastTree could not be started') from exc
    if completed.returncode != 0:
        output_path.unlink(missing_ok=True)
        raise ToolExecutionError(f'FastTree exited with code {completed.returncode}')
    if not output_path.is_file() or output_path.stat().st_size == 0:
        output_path.unlink(missing_ok=True)
        raise ToolExecutionError('FastTree produced an empty tree')
    if output_path.stat().st_size > settings.MAX_TREE_OUTPUT_BYTES:
        output_path.unlink(missing_ok=True)
        raise ToolExecutionError('FastTree output exceeds the configured size limit')
    return {
        'executable': executable,
        'version': fasttree_capability().get('version', ''),
        'alphabet': normalized_alphabet,
        'model': normalized_model,
        'threads': threads,
    }
