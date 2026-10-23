"""Single initial-work producer using the existing input resolver."""
from capabilities.input_resolver import resolve_work


def publish_initial_work(descriptor, enabled_capabilities, *, publisher, **kwargs):
    work = resolve_work(descriptor, enabled_capabilities)
    for request in work:
        publisher(request.kind, request.payload, **kwargs)
    return work
