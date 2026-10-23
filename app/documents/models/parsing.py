from django.db import models

from config.enums import AIStatus


class DocumentParseResult(models.Model):
    """
    문서 파싱 결과의 운용용 요약 모델
    
    Column Example:
        - `parser_name = "docling"`
        - `parser_mode = "convert_string_md"`
        - `status = "SUCCESS"`
        - `input_format = "md"`
        - `input_document_hash = "abc123..."`
        - `input_page_count = 5`
        - `result_page_count = 0`
        - `chunk_count = 12`
        - `timings = {...}`
        - `errors = []`
        - `parsed_at = 2026-04-06T15:00:00`
    
    Metadata Example:
        {
          "parser_version": "2.82.0",
          "tokenizer_name": "BAAI/bge-m3",
          "max_tokens": 1024,
          "file_ext": ".hwpx"
        }
    """

    node = models.OneToOneField(
        "files.Node",
        on_delete=models.CASCADE,
        related_name="parse_result",
    )

    # Parser Identification
    parser_name = models.CharField(
        max_length=64, default="docling"
    )
    parser_mode = models.CharField(
        max_length=64, blank=True
    )

    # Parser Status
    status = models.CharField(
        max_length=32,
        choices=AIStatus.choices,
        default=AIStatus.PENDING,
        db_index=True,
    )

    # Document Information
    input_format = models.CharField(max_length=32, blank=True)
    input_document_hash = models.CharField(max_length=64, blank=True)

    # Pages Statistics
    input_page_count = models.PositiveIntegerField(null=True, blank=True)
    result_page_count = models.PositiveIntegerField(null=True, blank=True)

    # Chunk Statistics
    chunk_count = models.PositiveIntegerField(default=0)

    # NULL is deliberate for legacy rows: never invent historical provenance.
    parse_revision = models.UUIDField(null=True, blank=True, editable=False)

    # One server-wide embedding contract applies to the whole document.
    # Keeping this marker here avoids repeating a generation FK on every
    # chunk embedding row.
    embedding_generation_id = models.CharField(max_length=96, blank=True)
    embedding_runtime_fingerprint = models.CharField(max_length=64, blank=True)

    # Execution Result
    timings = models.JSONField(default=dict, blank=True)
    errors = models.JSONField(default=list, blank=True)

    # trace_id, queue_wait_ms, parse_processing_ms 등 앱 레벨 성능 계측치.
    # `timings`(Docling 자체 내부 프로파일링)과는 별개.
    performance_metrics = models.JSONField(default=dict, blank=True)
    
    # Optional Debug / Reproducibility Metadata
    metadata = models.JSONField(default=dict, blank=True)

    # AI Summary and Tags
    summary = models.TextField(blank=True, default="")
    auto_tags = models.JSONField(default=list, blank=True)

    # Management Fields
    parsed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Recovery Tracking
    recovery_attempts = models.PositiveIntegerField(
        default=0,
        help_text="복구 태스크가 재큐잉한 누적 횟수. MAX 초과 시 복구 후보에서 제외됩니다.",
    )
    last_recovered_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="마지막으로 복구 태스크가 재큐잉한 시각.",
    )

    class Meta:
        indexes = [
            models.Index(fields=["parser_name"], name="docs_parse_parser_name_idx"),
            models.Index(fields=["input_document_hash"], name="docs_parse_input_hash_idx"),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.node.name} - ParseResult"

    def to_dict(self):
        return {
            "id": self.id,
            "node_id": self.node_id,
            "parser_name": self.parser_name,
            "parser_mode": self.parser_mode,
            "status": self.status,
            "input_format": self.input_format,
            "input_document_hash": self.input_document_hash,
            "input_page_count": self.input_page_count,
            "result_page_count": self.result_page_count,
            "chunk_count": self.chunk_count,
            "timings": self.timings,
            "errors": self.errors,
            "performance_metrics": self.performance_metrics,
            "metadata": self.metadata,
            "summary": self.summary,
            "auto_tags": self.auto_tags,
            "parsed_at": self.parsed_at.isoformat() if self.parsed_at else None,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

class DocumentChunk(models.Model):
    parse_result = models.ForeignKey(
        "documents.DocumentParseResult",
        on_delete=models.CASCADE,
        related_name="chunks",
        db_index=False,
    )

    chunk_index = models.PositiveIntegerField()
    text = models.TextField()
    token_count = models.PositiveIntegerField(null=True, blank=True)

    section_title = models.CharField(max_length=255, blank=True)
    page_from = models.PositiveIntegerField(null=True, blank=True)
    page_to = models.PositiveIntegerField(null=True, blank=True)

    chunk_meta = models.JSONField(default=dict, blank=True)

    status = models.CharField(
        max_length=32,
        choices=AIStatus.choices,
        default=AIStatus.PENDING,
        db_index=True,
    )
    error_message = models.JSONField(default=dict, blank=True)

    # trace_id, queue_wait_ms, embedding_processing_ms 등 앱 레벨 성능 계측치.
    performance_metrics = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Recovery Tracking
    recovery_attempts = models.PositiveIntegerField(
        default=0,
        help_text="임베딩 복구 태스크가 재큐잉한 누적 횟수. MAX 초과 시 복구 후보에서 제외됩니다.",
    )
    last_recovered_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="마지막으로 임베딩 복구 태스크가 재큐잉한 시각.",
    )

    class Meta:
        ordering = ["chunk_index"]
        constraints = [
            models.UniqueConstraint(
                fields=["parse_result", "chunk_index"],
                name="uniq_chunk_index_per_parse_result",
            )
        ]

    def __str__(self):
        return f"{self.parse_result.node.name} - Chunk {self.chunk_index}"

    def to_dict(self):
        return {
            "id": self.id,
            "parse_result_id": self.parse_result_id,
            "chunk_index": self.chunk_index,
            "text": self.text,
            "section_title": self.section_title,
            "page_from": self.page_from,
            "page_to": self.page_to,
            "token_count": self.token_count,
            "chunk_meta": self.chunk_meta,
            "performance_metrics": self.performance_metrics,
            "created_at": self.created_at.isoformat(),
        }
