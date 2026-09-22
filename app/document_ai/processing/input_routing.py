"""Adapt stored file metadata to the Core input resolver."""

from capabilities.input_resolver import InputDescriptor, resolve_work


def resolve_blob_work(blob, enabled_capabilities):
    return resolve_work(
        InputDescriptor(
            node_id=blob.node_id,
            filename=blob.original_name or blob.file.name,
            mime_type=blob.mime_type or "",
        ),
        enabled_capabilities,
    )
