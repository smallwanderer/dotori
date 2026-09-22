import pytest

from capabilities.input_resolver import InputDescriptor, InputResolutionError, resolve_work
from capabilities.registry import DOCUMENT_EXTRACT, DOCUMENT_SEMANTIC_SEARCH


def descriptor(name, mime=""):
    return InputDescriptor(node_id=7, filename=name, mime_type=mime)


def test_document_selects_one_initial_parse_with_overlapping_capabilities():
    requests = resolve_work(
        descriptor("REPORT.PDF", "application/pdf"),
        (DOCUMENT_EXTRACT, DOCUMENT_SEMANTIC_SEARCH),
    )
    assert [(request.kind, request.payload) for request in requests] == [
        ("parse_document", {"node_id": 7})
    ]


def test_disabled_document_capability_produces_no_work():
    assert resolve_work(descriptor("report.txt", "text/plain"), ()) == ()


def test_unknown_extension_is_rejected():
    with pytest.raises(InputResolutionError) as exc:
        resolve_work(descriptor("archive.zip"), (DOCUMENT_EXTRACT,))
    assert exc.value.code == "UNSUPPORTED_INPUT"


def test_binding_must_reference_registered_work():
    with pytest.raises(InputResolutionError) as exc:
        resolve_work(descriptor("report.pdf"), (DOCUMENT_EXTRACT,), {"document": "missing_work"})
    assert exc.value.code == "WORK_NOT_REGISTERED"
