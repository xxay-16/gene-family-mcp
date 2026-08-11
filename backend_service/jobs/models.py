import uuid

from django.db import models


class AnalysisJob(models.Model):
    class AnalysisType(models.TextChoices):
        CIS_ELEMENTS = 'cis_elements', 'Cis-element analysis'

    class Status(models.TextChoices):
        QUEUED = 'queued', 'Queued'
        RUNNING = 'running', 'Running'
        WAITING_EXTERNAL = 'waiting_external', 'Waiting for external result'
        SUCCEEDED = 'succeeded', 'Succeeded'
        FAILED = 'failed', 'Failed'
        CANCELLED = 'cancelled', 'Cancelled'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    analysis_type = models.CharField(max_length=64, choices=AnalysisType.choices)
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.QUEUED,
        db_index=True,
    )
    stage = models.CharField(max_length=64, default='queued')
    progress = models.PositiveSmallIntegerField(null=True, blank=True)
    parameters = models.JSONField(default=dict)
    result = models.JSONField(default=dict, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    error_message = models.TextField(blank=True)
    queue_task_id = models.CharField(max_length=64, blank=True, db_index=True)
    idempotency_key = models.CharField(max_length=128, blank=True, db_index=True)
    provider_ref = models.CharField(max_length=128, blank=True, db_index=True)
    external_deadline = models.DateTimeField(null=True, blank=True, db_index=True)
    last_polled_at = models.DateTimeField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['analysis_type', 'idempotency_key'],
                condition=~models.Q(idempotency_key=''),
                name='unique_analysis_idempotency_key',
            )
        ]


class AnalysisEvent(models.Model):
    job = models.ForeignKey(
        AnalysisJob,
        on_delete=models.CASCADE,
        related_name='events',
    )
    event_type = models.CharField(max_length=64)
    message = models.TextField(blank=True)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'id']


class Artifact(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(
        AnalysisJob,
        on_delete=models.CASCADE,
        related_name='artifacts',
    )
    kind = models.CharField(max_length=64)
    filename = models.CharField(max_length=255)
    storage_path = models.CharField(max_length=1024)
    media_type = models.CharField(max_length=255, default='application/octet-stream')
    size = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'filename']
        constraints = [
            models.UniqueConstraint(
                fields=['job', 'storage_path'],
                name='unique_job_artifact_path',
            )
        ]
