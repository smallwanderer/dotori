from django.db import models


class NodeType(models.TextChoices):
    FILE = "file", "File"
    FOLDER = "directory", "Directory"


class FileStatus(models.TextChoices):
    UPLOADED = "uploaded", "Uploaded"
    READY = "ready", "Ready"
    FAILED = "failed", "Failed"


class FileLanguage(models.TextChoices):
    KOREAN = "ko", "Korean"
    ENGLISH = "en", "English"
    CHINESE = "zh", "Chinese"
    JAPANESE = "ja", "Japanese"
    OTHER = "other", "Other"


class AIStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PROCESSING = "processing", "Processing"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"


class RAGStage(models.TextChoices):
    QUEUED = "queued", "Queued"
    SEARCHING = "searching", "Searching"
    GENERATING = "generating", "Generating"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
