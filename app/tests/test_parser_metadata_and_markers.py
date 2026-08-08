from enum import Enum
from pathlib import Path

import pytest

from document_ai.parsers.hwp_parser import _normalize_extracted_text
from document_ai.parsers.text_utils import serialize_meta


pytestmark = pytest.mark.unit


class Label(Enum):
    PICTURE = "picture"


def test_serialize_meta_recursively_produces_json_values():
    source = {
        "headings": ("One", "Two"),
        "item": {
            "label": Label.PICTURE,
            "path": Path("/tmp/source.pdf"),
        },
    }

    result = serialize_meta(source)

    assert result == {
        "headings": ["One", "Two"],
        "item": {
            "label": "picture",
            "path": "/tmp/source.pdf",
        },
    }
    assert serialize_meta(result) == result


def test_hwp_standalone_image_marker_is_removed_but_caption_is_kept():
    text = "<그림>\n그림 1. 시스템 전체 구성도\n본문"

    assert _normalize_extracted_text(text) == "그림 1. 시스템 전체 구성도\n본문"
