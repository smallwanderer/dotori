from django.db import models


class ExecutorJobReceipt(models.Model):
    """Small durable receipt for executor HTTP dispatch.

    The receipt deliberately records job identity and terminal metadata only.
    Document contents and vectors remain in their owning tables.
    """

    STATUS_RUNNING = "running"
    STATUS_SUCCEEDED = "succeeded"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = (
        (STATUS_RUNNING, "Running"),
        (STATUS_SUCCEEDED, "Succeeded"),
        (STATUS_FAILED, "Failed"),
    )

    job_id = models.CharField(max_length=64, primary_key=True)
    work_kind = models.CharField(max_length=64)
    target_key = models.CharField(max_length=160)
    payload_fingerprint = models.CharField(max_length=64)
    source_snapshot = models.JSONField(default=dict, blank=True)
    runtime_snapshot = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_RUNNING)
    attempt = models.PositiveIntegerField(default=1)
    result = models.JSONField(default=dict, blank=True)
    error = models.JSONField(default=dict, blank=True)
    retryable = models.BooleanField(default=False)
    followups = models.JSONField(default=list, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["work_kind", "target_key", "status"], name="docai_exec_target_status_idx"),
            models.Index(fields=["status", "updated_at"], name="docai_exec_status_updated_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["work_kind", "target_key"],
                condition=models.Q(status="running"),
                name="docai_exec_one_active_target",
            ),
        ]


class ResourceSnapshot(models.Model):
    """
    운영자가 수동으로 실행하는 `collect_resource_snapshot` 명령이 남기는
    자원 사용량 한 줄 기록. 상시 수집(cron)은 하지 않으므로 row 수가 적어
    별도 보존정책이 필요 없다 (performance-and-reliability.md 참고).

    `service`는 `app`/`embedding-executor`/`db`/`redis`/`dotori-llm` 같은
    컨테이너 식별자이거나, 디스크 여유 공간처럼 컨테이너 단위가 아닌
    항목은 `disk:uploads`/`disk:logs`/`disk:config`처럼 구분한다.

    현재는 docker.sock 마운트 없이 수집 가능한 항목(DB connection 수,
    디스크 여유 공간)만 채워진다. `cpu_percent`/`mem_mb`/`gpu_mem_mb`는
    컨테이너별 `docker stats`/`nvidia-smi` 연동 전까지는 비워둔다.
    """

    service = models.CharField(max_length=64, db_index=True)
    cpu_percent = models.FloatField(null=True, blank=True)
    mem_mb = models.FloatField(null=True, blank=True)
    gpu_mem_mb = models.FloatField(null=True, blank=True)
    db_connections = models.PositiveIntegerField(null=True, blank=True)
    disk_free_mb = models.FloatField(null=True, blank=True)
    collected_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["service", "-collected_at"]),
        ]
        ordering = ["-collected_at"]

    def __str__(self):
        return f"ResourceSnapshot({self.service}) at {self.collected_at.isoformat()}"
