from __future__ import annotations

import hashlib
import json
import mimetypes
from datetime import timedelta
from pathlib import Path

from cis_elements.parser import process_plantcare_attachments
from cis_elements.services import collect_results, submit_prediction
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .fasta import parse_and_normalize_fasta, summarize_fasta_alignment
from .local_tools.fasttree import run_fasttree
from .local_tools.mafft import (
    ToolExecutionError,
    ToolUnavailableError,
    run_mafft,
)
from .models import AnalysisJob, Artifact
from .newick import summarize_newick
from .services import (
    add_event,
    artifact_payload,
    create_workflow_child,
)


def _set_state(job: AnalysisJob, status: str, stage: str, progress: int | None):
    if job.status == AnalysisJob.Status.CANCELLED:
        return False
    job.status = status
    job.stage = stage
    job.progress = progress
    fields = ['status', 'stage', 'progress', 'updated_at']
    if status == AnalysisJob.Status.RUNNING and job.started_at is None:
        job.started_at = timezone.now()
        fields.append('started_at')
    job.save(update_fields=fields)
    add_event(job, 'status_changed', f'Job entered {status}/{stage}')
    return True


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_kind(path: Path, structured_path: Path | None = None) -> str:
    if structured_path is not None and path.resolve() == structured_path.resolve():
        return 'plantcare_structured_result'
    if path.suffix.lower() == '.tab':
        return 'plantcare_table'
    if path.suffix.lower() in {'.html', '.htm'}:
        return 'plantcare_report'
    if path.name.lower().endswith(('.tar.gz', '.tgz', '.tar')):
        return 'plantcare_archive'
    return 'plantcare_attachment'


def _register_artifact(
    job: AnalysisJob,
    path: Path,
    *,
    structured_path: Path | None = None,
    kind: str | None = None,
    media_type: str | None = None,
    metadata: dict | None = None,
) -> Artifact:
    artifact_root = Path(settings.ARTIFACT_ROOT).resolve()
    resolved_path = path.resolve()
    if not resolved_path.is_relative_to(artifact_root):
        raise ValueError('artifact path is outside ARTIFACT_ROOT')
    storage_path = str(resolved_path.relative_to(artifact_root))
    defaults = {
        'kind': kind or _artifact_kind(resolved_path, structured_path),
        'filename': resolved_path.name,
        'media_type': media_type
        or mimetypes.guess_type(resolved_path.name)[0]
        or 'application/octet-stream',
        'size': resolved_path.stat().st_size,
        'sha256': _sha256(resolved_path),
        'metadata': metadata or {},
    }
    artifact, _ = Artifact.objects.update_or_create(
        job=job,
        storage_path=storage_path,
        defaults=defaults,
    )
    return artifact


def _public_result(
    provider_result: dict,
    artifacts: list[Artifact],
    summary: dict,
) -> dict:
    return {
        'ref': provider_result.get('ref', ''),
        'subject': provider_result.get('subject', ''),
        'date': provider_result.get('date', ''),
        'summary': summary,
        'artifacts': [artifact_payload(artifact) for artifact in artifacts],
    }


def _error_details(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, TimeoutError):
        return 'PROVIDER_TIMEOUT', 'PlantCARE result collection timed out'
    if isinstance(exc, ValueError):
        return 'CAPABILITY_UNAVAILABLE', str(exc)
    return 'PROVIDER_EXECUTION_FAILED', 'PlantCARE analysis failed'


def _fail_job(job: AnalysisJob, error_code: str, error_message: str, exc=None):
    if job.status == AnalysisJob.Status.CANCELLED:
        return
    job.status = AnalysisJob.Status.FAILED
    job.stage = 'failed'
    job.progress = None
    job.error_code = error_code
    job.error_message = error_message
    job.finished_at = timezone.now()
    job.lease_expires_at = None
    job.save(
        update_fields=[
            'status',
            'stage',
            'progress',
            'error_code',
            'error_message',
            'finished_at',
            'lease_expires_at',
            'updated_at',
        ]
    )
    details = {'exception_type': type(exc).__name__} if exc is not None else {}
    add_event(job, 'job_failed', error_message, details)


def _complete_job(job: AnalysisJob, provider_result: dict):
    output_files = [Path(path) for path in provider_result.get('attachments', [])]
    output_dir = Path(settings.ARTIFACT_ROOT) / str(job.id)
    processed = process_plantcare_attachments(
        output_files,
        output_dir,
        max_members=settings.PLANTCARE_ARCHIVE_MAX_MEMBERS,
        max_file_size=settings.PLANTCARE_ARCHIVE_MAX_FILE_SIZE,
        max_total_size=settings.PLANTCARE_ARCHIVE_MAX_TOTAL_SIZE,
    )
    all_files = []
    seen = set()
    for path in [
        *output_files,
        *processed['derived_files'],
        processed['structured_path'],
    ]:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        all_files.append(path)
    artifacts = [
        _register_artifact(
            job,
            path,
            structured_path=processed['structured_path'],
        )
        for path in all_files
    ]
    job.result = _public_result(
        provider_result,
        artifacts,
        processed['summary'],
    )
    job.status = AnalysisJob.Status.SUCCEEDED
    job.stage = 'completed'
    job.progress = 100
    job.finished_at = timezone.now()
    job.error_code = ''
    job.error_message = ''
    job.lease_expires_at = None
    job.save(
        update_fields=[
            'result',
            'status',
            'stage',
            'progress',
            'finished_at',
            'error_code',
            'error_message',
            'lease_expires_at',
            'updated_at',
        ]
    )
    add_event(job, 'job_succeeded', 'Analysis job completed')


def _complete_fasta_validation(job: AnalysisJob) -> None:
    from .models import InputArtifact

    input_artifact = InputArtifact.objects.get(
        id=job.parameters['input_artifact_id'],
        kind='fasta_input',
    )
    artifact_root = Path(settings.ARTIFACT_ROOT).resolve()
    input_path = (artifact_root / input_artifact.storage_path).resolve()
    if not input_path.is_relative_to(artifact_root) or not input_path.is_file():
        raise ValueError('FASTA input artifact file is unavailable')
    if input_path.stat().st_size != input_artifact.size:
        raise ValueError('FASTA input artifact size does not match its manifest')
    if _sha256(input_path) != input_artifact.sha256:
        raise ValueError('FASTA input artifact checksum does not match its manifest')

    try:
        text = input_path.read_text(encoding='utf-8-sig')
    except UnicodeDecodeError as exc:
        raise ValueError('FASTA input must be valid UTF-8 text') from exc
    normalized, summary = parse_and_normalize_fasta(
        text,
        alphabet=job.parameters['alphabet'],
        max_records=settings.MAX_FASTA_RECORDS,
        max_sequence_length=settings.MAX_FASTA_SEQUENCE_LENGTH,
        max_total_residues=settings.MAX_FASTA_TOTAL_RESIDUES,
        max_header_length=settings.MAX_FASTA_HEADER_LENGTH,
    )

    output_dir = artifact_root / str(job.id)
    output_dir.mkdir(parents=True, exist_ok=True)
    normalized_path = output_dir / 'normalized.fasta'
    summary_path = output_dir / 'fasta_summary.json'
    normalized_path.write_text(normalized, encoding='utf-8', newline='\n')
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
        newline='\n',
    )
    with transaction.atomic():
        locked_job = AnalysisJob.objects.select_for_update().get(id=job.id)
        if locked_job.status == AnalysisJob.Status.CANCELLED:
            return
        artifacts = [
            _register_artifact(
                locked_job,
                normalized_path,
                kind='normalized_fasta',
                media_type='text/x-fasta',
                metadata={
                    'alphabet': summary['alphabet'],
                    'record_count': summary['record_count'],
                },
            ),
            _register_artifact(
                locked_job,
                summary_path,
                kind='fasta_validation_summary',
                media_type='application/json',
            ),
        ]
        locked_job.result = {
            'summary': summary,
            'input': {
                'input_artifact_id': str(input_artifact.id),
                'filename': input_artifact.filename,
                'sha256': input_artifact.sha256,
            },
            'artifacts': [artifact_payload(artifact) for artifact in artifacts],
        }
        locked_job.status = AnalysisJob.Status.SUCCEEDED
        locked_job.stage = 'completed'
        locked_job.progress = 100
        locked_job.finished_at = timezone.now()
        locked_job.error_code = ''
        locked_job.error_message = ''
        locked_job.lease_expires_at = None
        locked_job.save(
            update_fields=[
                'result',
                'status',
                'stage',
                'progress',
                'finished_at',
                'error_code',
                'error_message',
                'lease_expires_at',
                'updated_at',
            ]
        )
        add_event(
            locked_job,
            'job_succeeded',
            'FASTA validation and normalization completed',
            {
                'record_count': summary['record_count'],
                'alphabet': summary['alphabet'],
            },
        )


def _execute_fasta_validation(job: AnalysisJob) -> dict:
    if not _set_state(job, AnalysisJob.Status.RUNNING, 'validating_fasta', 20):
        return {'job_id': str(job.id), 'status': job.status}
    _complete_fasta_validation(job)
    job.refresh_from_db()
    return {'job_id': str(job.id), 'status': job.status}


def _execute_multiple_sequence_alignment(job: AnalysisJob) -> dict:
    if not _set_state(job, AnalysisJob.Status.RUNNING, 'running_mafft', 20):
        return {'job_id': str(job.id), 'status': job.status}
    source_artifact = Artifact.objects.select_related('job').get(
        id=job.parameters['artifact_id'],
        kind='normalized_fasta',
        job__status=AnalysisJob.Status.SUCCEEDED,
    )
    source_summary = source_artifact.job.result.get('summary', {})
    if source_summary.get('record_count', 0) < 2:
        raise ValueError('multiple sequence alignment requires at least two sequences')
    source_alphabet = source_artifact.metadata.get(
        'alphabet',
        source_summary.get('alphabet', ''),
    )
    if source_alphabet not in {'dna', 'protein'}:
        raise ValueError('source FASTA artifact has no supported alphabet metadata')

    artifact_root = Path(settings.ARTIFACT_ROOT).resolve()
    input_path = (artifact_root / source_artifact.storage_path).resolve()
    if not input_path.is_relative_to(artifact_root) or not input_path.is_file():
        raise ValueError('source FASTA artifact file is unavailable')
    if input_path.stat().st_size != source_artifact.size:
        raise ValueError('source FASTA artifact size does not match its manifest')
    if _sha256(input_path) != source_artifact.sha256:
        raise ValueError('source FASTA artifact checksum does not match its manifest')

    output_dir = artifact_root / str(job.id)
    output_path = output_dir / 'aligned.fasta'
    run_metadata = run_mafft(
        input_path,
        output_path,
        strategy=job.parameters['strategy'],
        threads=job.parameters['threads'],
        timeout=settings.MAFFT_TIMEOUT,
    )
    alignment_text = output_path.read_text(encoding='utf-8-sig')
    summary = summarize_fasta_alignment(
        alignment_text,
        alphabet=source_alphabet,
    )

    with transaction.atomic():
        locked_job = AnalysisJob.objects.select_for_update().get(id=job.id)
        if locked_job.status == AnalysisJob.Status.CANCELLED:
            output_path.unlink(missing_ok=True)
            return {'job_id': str(job.id), 'status': locked_job.status}
        artifact = _register_artifact(
            locked_job,
            output_path,
            kind='aligned_fasta',
            media_type='text/x-fasta',
            metadata={
                'tool': 'MAFFT',
                'tool_version': run_metadata['version'],
                'strategy': run_metadata['strategy'],
                'threads': run_metadata['threads'],
                'source_artifact_id': str(source_artifact.id),
                'alphabet': source_alphabet,
            },
        )
        locked_job.result = {
            'summary': summary,
            'tool': {
                'name': 'MAFFT',
                'version': run_metadata['version'],
                'strategy': run_metadata['strategy'],
                'threads': run_metadata['threads'],
            },
            'source_artifact_id': str(source_artifact.id),
            'artifacts': [artifact_payload(artifact)],
        }
        locked_job.status = AnalysisJob.Status.SUCCEEDED
        locked_job.stage = 'completed'
        locked_job.progress = 100
        locked_job.finished_at = timezone.now()
        locked_job.error_code = ''
        locked_job.error_message = ''
        locked_job.lease_expires_at = None
        locked_job.save(
            update_fields=[
                'result',
                'status',
                'stage',
                'progress',
                'finished_at',
                'error_code',
                'error_message',
                'lease_expires_at',
                'updated_at',
            ]
        )
        add_event(
            locked_job,
            'job_succeeded',
            'Multiple sequence alignment completed with MAFFT',
            {
                'record_count': summary['record_count'],
                'alignment_length': summary['alignment_length'],
            },
        )
    return {'job_id': str(job.id), 'status': AnalysisJob.Status.SUCCEEDED}


def _execute_phylogenetic_tree(job: AnalysisJob) -> dict:
    if not _set_state(job, AnalysisJob.Status.RUNNING, 'running_fasttree', 20):
        return {'job_id': str(job.id), 'status': job.status}
    source_artifact = Artifact.objects.select_related('job').get(
        id=job.parameters['artifact_id'],
        kind='aligned_fasta',
        job__status=AnalysisJob.Status.SUCCEEDED,
    )
    source_summary = source_artifact.job.result.get('summary', {})
    record_count = int(source_summary.get('record_count', 0))
    if record_count < 3:
        raise ValueError('phylogenetic tree requires at least three sequences')
    source_alphabet = source_artifact.metadata.get(
        'alphabet',
        source_summary.get('alphabet', ''),
    )
    if source_alphabet not in {'dna', 'protein'}:
        raise ValueError('aligned FASTA artifact has no supported alphabet metadata')
    requested_model = job.parameters['model']
    model = requested_model
    if requested_model == 'auto':
        model = 'gtr' if source_alphabet == 'dna' else 'lg'
    if source_alphabet == 'dna' and model not in {'jc', 'gtr'}:
        raise ValueError('DNA FastTree model must be one of: auto, gtr, jc')
    if source_alphabet == 'protein' and model not in {'jtt', 'wag', 'lg'}:
        raise ValueError('protein FastTree model must be one of: auto, jtt, lg, wag')

    artifact_root = Path(settings.ARTIFACT_ROOT).resolve()
    input_path = (artifact_root / source_artifact.storage_path).resolve()
    if not input_path.is_relative_to(artifact_root) or not input_path.is_file():
        raise ValueError('aligned FASTA artifact file is unavailable')
    if input_path.stat().st_size != source_artifact.size:
        raise ValueError('aligned FASTA artifact size does not match its manifest')
    if _sha256(input_path) != source_artifact.sha256:
        raise ValueError('aligned FASTA artifact checksum does not match its manifest')

    output_dir = artifact_root / str(job.id)
    output_path = output_dir / 'tree.newick'
    run_metadata = run_fasttree(
        input_path,
        output_path,
        alphabet=source_alphabet,
        model=model,
        threads=job.parameters['threads'],
        timeout=settings.FASTTREE_TIMEOUT,
    )
    tree_text = output_path.read_text(encoding='utf-8-sig')
    summary = summarize_newick(tree_text, expected_leaf_count=record_count)

    with transaction.atomic():
        locked_job = AnalysisJob.objects.select_for_update().get(id=job.id)
        if locked_job.status == AnalysisJob.Status.CANCELLED:
            output_path.unlink(missing_ok=True)
            return {'job_id': str(job.id), 'status': locked_job.status}
        artifact = _register_artifact(
            locked_job,
            output_path,
            kind='phylogenetic_tree_newick',
            media_type='text/x-newick',
            metadata={
                'tool': 'FastTree',
                'tool_version': run_metadata['version'],
                'model': run_metadata['model'],
                'threads': run_metadata['threads'],
                'source_artifact_id': str(source_artifact.id),
                'alphabet': source_alphabet,
            },
        )
        locked_job.result = {
            'summary': summary,
            'tool': {
                'name': 'FastTree',
                'version': run_metadata['version'],
                'model': run_metadata['model'],
                'threads': run_metadata['threads'],
            },
            'source_artifact_id': str(source_artifact.id),
            'artifacts': [artifact_payload(artifact)],
        }
        locked_job.status = AnalysisJob.Status.SUCCEEDED
        locked_job.stage = 'completed'
        locked_job.progress = 100
        locked_job.finished_at = timezone.now()
        locked_job.error_code = ''
        locked_job.error_message = ''
        locked_job.lease_expires_at = None
        locked_job.save(
            update_fields=[
                'result',
                'status',
                'stage',
                'progress',
                'finished_at',
                'error_code',
                'error_message',
                'lease_expires_at',
                'updated_at',
            ]
        )
        add_event(
            locked_job,
            'job_succeeded',
            'Phylogenetic tree completed with FastTree',
            {
                'leaf_count': summary['leaf_count'],
                'model': run_metadata['model'],
            },
        )
    return {'job_id': str(job.id), 'status': AnalysisJob.Status.SUCCEEDED}


def _submit_cis_element_analysis(job: AnalysisJob) -> dict:
    if not _set_state(job, AnalysisJob.Status.RUNNING, 'submitting', 10):
        return {'job_id': str(job.id), 'status': job.status}
    provider_ref = submit_prediction(job.parameters['sequence'])
    job.refresh_from_db()
    if job.status == AnalysisJob.Status.CANCELLED:
        return {'job_id': str(job.id), 'status': job.status}

    job.provider_ref = provider_ref
    job.external_deadline = timezone.now() + timedelta(
        seconds=settings.PLANTCARE_RESULT_TIMEOUT
    )
    job.status = AnalysisJob.Status.WAITING_EXTERNAL
    job.stage = 'waiting_plantcare'
    job.progress = 40
    job.lease_expires_at = None
    job.save(
        update_fields=[
            'provider_ref',
            'external_deadline',
            'status',
            'stage',
            'progress',
            'lease_expires_at',
            'updated_at',
        ]
    )
    add_event(
        job,
        'provider_submitted',
        'PlantCARE request submitted',
        {'provider_ref': provider_ref},
    )
    return {'job_id': str(job.id), 'status': job.status}


def execute_analysis_job(job_id: str) -> dict:
    """Claim and dispatch one business analysis through django-q2."""
    try:
        job = AnalysisJob.objects.get(id=job_id)
    except AnalysisJob.DoesNotExist:
        return {'job_id': job_id, 'status': 'not_found'}

    if job.status == AnalysisJob.Status.CANCELLED:
        return {'job_id': job_id, 'status': job.status}
    now = timezone.now()
    lease_seconds = settings.JOB_EXECUTION_LEASE_SECONDS
    if (
        job.analysis_type
        == AnalysisJob.AnalysisType.MULTIPLE_SEQUENCE_ALIGNMENT
    ):
        lease_seconds = max(lease_seconds, settings.MAFFT_TIMEOUT + 60)
    elif job.analysis_type == AnalysisJob.AnalysisType.PHYLOGENETIC_TREE:
        lease_seconds = max(lease_seconds, settings.FASTTREE_TIMEOUT + 60)
    claimed = AnalysisJob.objects.filter(
        id=job_id,
        status=AnalysisJob.Status.QUEUED,
    ).filter(
        Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now)
    ).update(
        lease_expires_at=now + timedelta(seconds=lease_seconds)
    )
    if not claimed:
        job.refresh_from_db()
        return {'job_id': job_id, 'status': job.status}
    job.refresh_from_db()

    try:
        if job.analysis_type == AnalysisJob.AnalysisType.CIS_ELEMENTS:
            return _submit_cis_element_analysis(job)
        if job.analysis_type == AnalysisJob.AnalysisType.FASTA_VALIDATION:
            return _execute_fasta_validation(job)
        if (
            job.analysis_type
            == AnalysisJob.AnalysisType.MULTIPLE_SEQUENCE_ALIGNMENT
        ):
            return _execute_multiple_sequence_alignment(job)
        if job.analysis_type == AnalysisJob.AnalysisType.PHYLOGENETIC_TREE:
            return _execute_phylogenetic_tree(job)
        raise ValueError(f'unsupported analysis type: {job.analysis_type}')
    except Exception as exc:
        job.refresh_from_db()
        if job.analysis_type == AnalysisJob.AnalysisType.FASTA_VALIDATION:
            error_code = (
                'INVALID_FASTA' if isinstance(exc, ValueError) else 'FASTA_PROCESSING_FAILED'
            )
            error_message = (
                str(exc) if isinstance(exc, ValueError) else 'FASTA processing failed'
            )
        elif (
            job.analysis_type
            == AnalysisJob.AnalysisType.MULTIPLE_SEQUENCE_ALIGNMENT
        ):
            if isinstance(exc, ToolUnavailableError):
                error_code = 'CAPABILITY_UNAVAILABLE'
            elif isinstance(exc, ToolExecutionError):
                error_code = 'TOOL_EXECUTION_FAILED'
            elif isinstance(exc, ValueError):
                error_code = 'INVALID_ALIGNMENT_INPUT'
            else:
                error_code = 'ALIGNMENT_PROCESSING_FAILED'
            error_message = (
                str(exc)
                if isinstance(
                    exc,
                    ToolUnavailableError | ToolExecutionError | ValueError,
                )
                else 'Multiple sequence alignment failed'
            )
        elif job.analysis_type == AnalysisJob.AnalysisType.PHYLOGENETIC_TREE:
            if isinstance(exc, ToolUnavailableError):
                error_code = 'CAPABILITY_UNAVAILABLE'
            elif isinstance(exc, ToolExecutionError):
                error_code = 'TOOL_EXECUTION_FAILED'
            elif isinstance(exc, ValueError):
                error_code = 'INVALID_TREE_INPUT'
            else:
                error_code = 'TREE_PROCESSING_FAILED'
            error_message = (
                str(exc)
                if isinstance(
                    exc,
                    ToolUnavailableError | ToolExecutionError | ValueError,
                )
                else 'Phylogenetic tree inference failed'
            )
        else:
            error_code, error_message = _error_details(exc)
        _fail_job(job, error_code, error_message, exc)
        return {
            'job_id': job_id,
            'status': job.status,
            'error_code': error_code,
        }


def _set_workflow_wait(
    job: AnalysisJob,
    *,
    stage: str,
    progress: int,
) -> None:
    job.status = AnalysisJob.Status.WAITING_DEPENDENCY
    job.stage = stage
    job.progress = progress
    job.lease_expires_at = None
    if job.started_at is None:
        job.started_at = timezone.now()
    job.save(
        update_fields=[
            'status',
            'stage',
            'progress',
            'lease_expires_at',
            'started_at',
            'updated_at',
        ]
    )


def _workflow_step_failure(parent: AnalysisJob, child: AnalysisJob) -> None:
    _fail_job(
        parent,
        'WORKFLOW_STEP_FAILED',
        f'Workflow step {child.workflow_step} failed: '
        f'{child.error_code or child.status}',
    )
    add_event(
        parent,
        'workflow_step_failed',
        parent.error_message,
        {
            'child_job_id': str(child.id),
            'child_error_code': child.error_code,
        },
    )


def _advance_sequence_phylogeny(parent: AnalysisJob) -> str:
    parent.refresh_from_db()
    if parent.status != AnalysisJob.Status.WAITING_DEPENDENCY:
        return parent.status
    children = {child.workflow_step: child for child in parent.child_jobs.all()}
    for step in ('validation', 'alignment', 'tree'):
        child = children.get(step)
        if child is not None and child.status in {
            AnalysisJob.Status.FAILED,
            AnalysisJob.Status.CANCELLED,
        }:
            _workflow_step_failure(parent, child)
            return AnalysisJob.Status.FAILED

    validation = children.get('validation')
    if validation is None:
        validation, _ = create_workflow_child(
            parent,
            step='validation',
            analysis_type=AnalysisJob.AnalysisType.FASTA_VALIDATION,
            parameters={
                'input_artifact_id': parent.parameters['input_artifact_id'],
                'alphabet': parent.parameters['alphabet'],
            },
        )
    if validation.status != AnalysisJob.Status.SUCCEEDED:
        _set_workflow_wait(parent, stage='waiting_validation', progress=10)
        return parent.status

    normalized_fasta = validation.artifacts.filter(kind='normalized_fasta').first()
    if normalized_fasta is None:
        _fail_job(
            parent,
            'WORKFLOW_ARTIFACT_MISSING',
            'Validation step did not produce normalized_fasta',
        )
        return AnalysisJob.Status.FAILED
    alignment = children.get('alignment')
    if alignment is None:
        alignment, _ = create_workflow_child(
            parent,
            step='alignment',
            analysis_type=AnalysisJob.AnalysisType.MULTIPLE_SEQUENCE_ALIGNMENT,
            parameters={
                'artifact_id': str(normalized_fasta.id),
                'strategy': parent.parameters['alignment_strategy'],
                'threads': parent.parameters['threads'],
            },
        )
        add_event(
            parent,
            'workflow_step_completed',
            'Workflow validation step completed',
            {'child_job_id': str(validation.id)},
        )
    if alignment.status != AnalysisJob.Status.SUCCEEDED:
        _set_workflow_wait(parent, stage='waiting_alignment', progress=40)
        return parent.status

    aligned_fasta = alignment.artifacts.filter(kind='aligned_fasta').first()
    if aligned_fasta is None:
        _fail_job(
            parent,
            'WORKFLOW_ARTIFACT_MISSING',
            'Alignment step did not produce aligned_fasta',
        )
        return AnalysisJob.Status.FAILED
    tree = children.get('tree')
    if tree is None:
        tree, _ = create_workflow_child(
            parent,
            step='tree',
            analysis_type=AnalysisJob.AnalysisType.PHYLOGENETIC_TREE,
            parameters={
                'artifact_id': str(aligned_fasta.id),
                'model': parent.parameters['tree_model'],
                'threads': parent.parameters['threads'],
            },
        )
        add_event(
            parent,
            'workflow_step_completed',
            'Workflow alignment step completed',
            {'child_job_id': str(alignment.id)},
        )
    if tree.status != AnalysisJob.Status.SUCCEEDED:
        _set_workflow_wait(parent, stage='waiting_tree', progress=75)
        return parent.status

    tree_artifact = tree.artifacts.filter(kind='phylogenetic_tree_newick').first()
    if tree_artifact is None:
        _fail_job(
            parent,
            'WORKFLOW_ARTIFACT_MISSING',
            'Tree step did not produce phylogenetic_tree_newick',
        )
        return AnalysisJob.Status.FAILED

    with transaction.atomic():
        locked_parent = AnalysisJob.objects.select_for_update().get(id=parent.id)
        if locked_parent.status == AnalysisJob.Status.CANCELLED:
            return locked_parent.status
        locked_parent.result = {
            'workflow': 'sequence_phylogeny',
            'steps': [
                {
                    'step': child.workflow_step,
                    'job_id': str(child.id),
                    'analysis_type': child.analysis_type,
                    'status': child.status,
                }
                for child in (validation, alignment, tree)
            ],
            'summary': {
                'record_count': validation.result.get('summary', {}).get(
                    'record_count'
                ),
                'alphabet': validation.result.get('summary', {}).get('alphabet'),
                'alignment_length': alignment.result.get('summary', {}).get(
                    'alignment_length'
                ),
                'leaf_count': tree.result.get('summary', {}).get('leaf_count'),
            },
            'final_artifact': artifact_payload(tree_artifact),
        }
        locked_parent.status = AnalysisJob.Status.SUCCEEDED
        locked_parent.stage = 'completed'
        locked_parent.progress = 100
        locked_parent.finished_at = timezone.now()
        locked_parent.lease_expires_at = None
        locked_parent.save(
            update_fields=[
                'result',
                'status',
                'stage',
                'progress',
                'finished_at',
                'lease_expires_at',
                'updated_at',
            ]
        )
        add_event(
            locked_parent,
            'job_succeeded',
            'Sequence phylogeny workflow completed',
            {'tree_job_id': str(tree.id)},
        )
    return AnalysisJob.Status.SUCCEEDED


def advance_waiting_workflows() -> dict:
    now = timezone.now()
    candidate_ids = list(
        AnalysisJob.objects.filter(
            analysis_type=AnalysisJob.AnalysisType.SEQUENCE_PHYLOGENY,
            status=AnalysisJob.Status.WAITING_DEPENDENCY,
        )
        .filter(Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now))
        .order_by('updated_at')
        .values_list('id', flat=True)[: settings.WORKFLOW_ADVANCE_BATCH_SIZE]
    )
    claimed_ids = []
    lease = now + timedelta(seconds=settings.WORKFLOW_LEASE_SECONDS)
    for job_id in candidate_ids:
        claimed = AnalysisJob.objects.filter(
            id=job_id,
            status=AnalysisJob.Status.WAITING_DEPENDENCY,
        ).filter(
            Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now)
        ).update(lease_expires_at=lease)
        if claimed:
            claimed_ids.append(job_id)

    completed = 0
    failed = 0
    for job in AnalysisJob.objects.filter(id__in=claimed_ids):
        try:
            status = _advance_sequence_phylogeny(job)
        except Exception as exc:
            job.refresh_from_db()
            _fail_job(
                job,
                'WORKFLOW_ADVANCE_FAILED',
                'Unable to advance sequence phylogeny workflow',
                exc,
            )
            failed += 1
            continue
        if status == AnalysisJob.Status.SUCCEEDED:
            completed += 1
        elif status == AnalysisJob.Status.FAILED:
            failed += 1
    return {
        'checked': len(claimed_ids),
        'completed': completed,
        'failed': failed,
    }


def poll_waiting_external_jobs() -> dict:
    """Collect available PlantCARE mail once for a bounded job batch."""
    now = timezone.now()
    stale_running_jobs = list(
        AnalysisJob.objects.filter(
            status=AnalysisJob.Status.RUNNING,
            lease_expires_at__lte=now,
        )[: settings.PLANTCARE_POLL_BATCH_SIZE]
    )
    for job in stale_running_jobs:
        _fail_job(
            job,
            'WORKER_LEASE_EXPIRED',
            'Analysis worker stopped before completing the task stage',
        )

    expired_jobs = list(
        AnalysisJob.objects.filter(
            status=AnalysisJob.Status.WAITING_EXTERNAL,
            external_deadline__lte=now,
        ).filter(
            Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now)
        )[: settings.PLANTCARE_POLL_BATCH_SIZE]
    )
    for job in expired_jobs:
        _fail_job(job, 'PROVIDER_TIMEOUT', 'PlantCARE result collection timed out')

    remaining_capacity = max(
        settings.PLANTCARE_POLL_BATCH_SIZE
        - len(expired_jobs)
        - len(stale_running_jobs),
        0,
    )
    candidate_ids = list(
        AnalysisJob.objects.filter(
            status=AnalysisJob.Status.WAITING_EXTERNAL,
            external_deadline__gt=now,
        ).filter(
            Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now)
        )
        .exclude(provider_ref='')
        .order_by('last_polled_at', 'created_at')
        .values_list('id', flat=True)[:remaining_capacity]
    )
    claimed_ids = []
    poll_lease = now + timedelta(seconds=settings.JOB_POLL_LEASE_SECONDS)
    for job_id in candidate_ids:
        claimed = AnalysisJob.objects.filter(
            id=job_id,
            status=AnalysisJob.Status.WAITING_EXTERNAL,
        ).filter(
            Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now)
        ).update(lease_expires_at=poll_lease)
        if claimed:
            claimed_ids.append(job_id)
    jobs = list(AnalysisJob.objects.filter(id__in=claimed_ids))
    if not jobs:
        return {
            'checked': 0,
            'completed': 0,
            'expired': len(expired_jobs),
            'stale_running': len(stale_running_jobs),
        }

    ref_to_output_dir = {
        job.provider_ref: Path(settings.ARTIFACT_ROOT) / str(job.id) for job in jobs
    }
    try:
        results = collect_results(ref_to_output_dir)
    except ValueError as exc:
        for job in jobs:
            _fail_job(job, 'CAPABILITY_UNAVAILABLE', str(exc), exc)
        return {
            'checked': len(jobs),
            'completed': 0,
            'expired': len(expired_jobs),
            'failed': len(jobs),
        }
    except Exception as exc:
        for job in jobs:
            job.last_polled_at = now
            job.lease_expires_at = None
            job.save(
                update_fields=['last_polled_at', 'lease_expires_at', 'updated_at']
            )
            add_event(
                job,
                'provider_poll_failed',
                'PlantCARE result check failed; the scheduler will retry',
                {'exception_type': type(exc).__name__},
            )
        return {
            'checked': len(jobs),
            'completed': 0,
            'expired': len(expired_jobs),
            'stale_running': len(stale_running_jobs),
            'retryable_error': True,
        }

    completed = 0
    for job in jobs:
        job.last_polled_at = now
        provider_result = results.get(job.provider_ref)
        if provider_result is None:
            job.lease_expires_at = None
            job.save(
                update_fields=[
                    'last_polled_at',
                    'lease_expires_at',
                    'updated_at',
                ]
            )
            continue
        job.save(update_fields=['last_polled_at', 'updated_at'])
        job.refresh_from_db()
        if job.status != AnalysisJob.Status.WAITING_EXTERNAL:
            continue
        _set_state(job, AnalysisJob.Status.RUNNING, 'collecting_result', 80)
        try:
            _complete_job(job, provider_result)
            completed += 1
        except Exception as exc:
            _fail_job(
                job,
                'RESULT_PROCESSING_FAILED',
                'PlantCARE result processing failed',
                exc,
            )

    return {
        'checked': len(jobs),
        'completed': completed,
        'expired': len(expired_jobs),
        'stale_running': len(stale_running_jobs),
    }
