from django.contrib.auth.models import AbstractBaseUser
from django.core.files.base import File
from django.core.files.storage import default_storage
from django.http import FileResponse
from django.db import IntegrityError, transaction

import dataclasses
import os

from ..models import FileBlob, Node, NodeType, UserStorage
from .utils import calculate_sha256, extract_ext

ALLOWED_EXTENSIONS = {
    ".pdf",
    ".txt",
    ".docx",
    ".xlsx",
    ".pptx",
    ".md",
    ".hwp",
    ".hwpx",
    ".png",
    ".jpg",
    ".jpeg",
    ".html",
}
MAX_UPLOAD_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB


MAX_RENAME_ATTEMPTS = 200


def _storage_record(workspace) -> UserStorage:
    storage, _ = UserStorage.objects.get_or_create(
        workspace=workspace,
        defaults={"user": workspace.created_by},
    )
    return storage


def _quota_error(workspace, size_delta: int) -> str | None:
    storage = _storage_record(workspace)
    if storage.used_size + size_delta > storage.total_size:
        remaining_mb = round(storage.remaining_size / 1024 / 1024, 2)
        return f"Not enough storage space is available. Remaining: {remaining_mb} MB."
    return None


def check_workspace_room(workspace, size_delta: int) -> tuple[bool, str]:
    """Whether `size_delta` more bytes fit in the workspace quota.

    For callers that write through `save_file` and so do not run the rest of
    `validate_upload`. Only the admission check has to be explicit: the
    `FileBlob` signals keep `UserStorage.used_size` current by themselves, in
    both directions, whichever path created or removed the blob.
    """
    error = _quota_error(workspace, size_delta)
    return error is None, error or ""


@dataclasses.dataclass
class UploadValidationResult:
    ok: bool
    warnings: list[str] = dataclasses.field(default_factory=list)
    errors: list[str] = dataclasses.field(default_factory=list)
    duplicate: bool = False


def validate_upload(workspace, owner: AbstractBaseUser, uploaded_file: File) -> UploadValidationResult:
    warnings = []
    errors = []

    if uploaded_file is None:
        return UploadValidationResult(
            ok=False,
            errors=["No file was provided."],
        )

    if uploaded_file.size > MAX_UPLOAD_SIZE:
        return UploadValidationResult(
            ok=False,
            errors=["The file exceeds the 2 GB upload limit."],
        )

    quota_error = _quota_error(workspace, uploaded_file.size)
    if quota_error:
        return UploadValidationResult(ok=False, errors=[quota_error])

    ext = os.path.splitext(uploaded_file.name)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return UploadValidationResult(
            ok=False,
            errors=["This file type is not supported."],
        )

    content_type = getattr(uploaded_file, "content_type", None)
    if not content_type:
        warnings.append("The file MIME type could not be verified.")

    sha256 = calculate_sha256(uploaded_file)
    duplicate = FileBlob.objects.filter(node__workspace=workspace, sha256=sha256).exists()
    if duplicate:
        warnings.append("A file with the same content already exists.")

    return UploadValidationResult(
        ok=True,
        warnings=warnings,
        errors=errors,
        duplicate=duplicate,
    )


def _name_candidates(name: str):
    stem, ext = os.path.splitext(name)
    yield name
    for counter in range(1, MAX_RENAME_ATTEMPTS + 1):
        yield f"{stem} ({counter}){ext}"


def save_file(workspace, owner: AbstractBaseUser, file: File, description: str, parent=None, ai_processing_enabled: bool = True) -> Node:
    sha256 = calculate_sha256(file)
    original_name = file.name

    for candidate_name in _name_candidates(original_name):
        file.seek(0)
        try:
            with transaction.atomic():
                node = Node.objects.create(
                    workspace=workspace,
                    owner=owner,
                    parent=parent,
                    name=candidate_name,
                    ext=extract_ext(candidate_name),
                    node_type=NodeType.FILE,
                    description=description,
                    ai_processing_enabled=ai_processing_enabled,
                )

                FileBlob.objects.create(
                    node=node,
                    file=file,
                    original_name=original_name,
                    size=file.size,
                    mime_type=getattr(file, "content_type", ""),
                    sha256=sha256,
                )
        except IntegrityError:
            continue
        return node

    raise ValueError("Could not find an available name for this file.")


def _coerce_node(file_or_node) -> Node:
    if isinstance(file_or_node, Node):
        node = file_or_node
    elif isinstance(file_or_node, FileBlob):
        node = file_or_node.node
    else:
        raise TypeError("Expected a Node or FileBlob instance.")

    if node.node_type != NodeType.FILE:
        raise ValueError("The target node is not a file.")
    if not hasattr(node, "blob"):
        raise ValueError("The file node does not have a blob attached.")

    return node


def delete_file(file_or_node):
    node = _coerce_node(file_or_node)
    blob = node.blob
    file_name = blob.file.name if blob.file else ""

    with transaction.atomic():
        if file_name and default_storage.exists(file_name):
            default_storage.delete(file_name)
        node.delete()


def open_file(file_or_node, mode: str = "rb"):
    node = _coerce_node(file_or_node)
    if not node.blob.file:
        raise FileNotFoundError("The file blob is missing from storage.")
    return node.blob.file.open(mode)


def get_download_response(file_or_node):
    node = _coerce_node(file_or_node)
    return FileResponse(
        open_file(node),
        as_attachment=True,
        filename=node.blob.original_name,
    )


def get_file(file_or_node):
    return _coerce_node(file_or_node)


def get_files(workspace):
    return Node.objects.filter(workspace=workspace, node_type=NodeType.FILE, trashed=False).order_by("-created_at")
