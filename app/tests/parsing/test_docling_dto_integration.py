import os
import sys

# app/ 디렉토리를 sys.path에 추가 (tests/ 하위에서 실행해도 config나 documents를 찾을 수 있도록)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from pathlib import Path

from documents.parsers.docling_parser import parse_document_entry, ParseResult

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def isolated_deployment(tmp_path, monkeypatch):
    """Do not inherit the operator's enabled capabilities or cached plan."""
    from capabilities.deployment import clear_deployment_snapshot_cache, load_deployment_snapshot
    from installation.deployment import build_module_deployment_plan, write_deployment_plan

    plan = build_module_deployment_plan(["documents"], disabled_provider_roles=("embedding", "llm"))
    path = write_deployment_plan(plan, repo_root=tmp_path)
    monkeypatch.setenv("DOTORI_DEPLOYMENT_PLAN_PATH", str(path))
    clear_deployment_snapshot_cache()
    snapshot = load_deployment_snapshot()
    assert snapshot.is_enabled("documents.extract")
    assert not snapshot.is_enabled("documents.semantic_search")
    yield
    clear_deployment_snapshot_cache()


def test_parse_document_entry_with_real_markdown_file(tmp_path: Path):
    """
    실제 markdown 파일을 생성하고,
    parse_document_entry()를 통해 ParseResult가 정상 생성되는지 확인한다.
    """

    sample_file = tmp_path / "sample.md"
    sample_file.write_text(
        "# 제목\n\n"
        "이 문서는 docling parser 통합 테스트용 문서입니다.\n\n"
        "## 섹션 1\n"
        "여기에 본문 내용이 들어갑니다.\n\n"
        "- 항목 1\n"
        "- 항목 2\n",
        encoding="utf-8",
    )

    result = parse_document_entry(str(sample_file))

    assert isinstance(result, ParseResult)

    # 기본 정보
    assert result.file_path == str(sample_file)
    assert result.file_ext == ".md"
    assert result.parser_mode in ("convert_string_md", "convert")

    # chunks
    assert isinstance(result.chunks, list)
    assert len(result.chunks) > 0

    first_chunk = result.chunks[0]
    assert first_chunk.chunk_index >= 0
    assert isinstance(first_chunk.serialized_text, str)
    assert first_chunk.serialized_text.strip() != ""
    # Extract-only parsing must not require an embedding model/tokenizer.
    assert first_chunk.tokens is None

    # ConversionResult 기반 추가 정보
    # 값이 항상 채워진다고 단정하지 말고, 타입 위주로 확인
    assert result.parser_backend == "docling"
    assert result.status is None or isinstance(result.status, str)
    assert result.errors is None or isinstance(result.errors, list)
    assert result.timings is None or isinstance(result.timings, dict)

    assert isinstance(result.backend_metadata, dict)
    assert result.backend_metadata.get("timestamp") is None or isinstance(result.backend_metadata["timestamp"], str)
    assert result.backend_metadata.get("input_info") is None or isinstance(result.backend_metadata["input_info"], dict)
    assert result.backend_metadata.get("version_info") is None or isinstance(result.backend_metadata["version_info"], dict)

    # 출력 결과 눈으로 볼 수 있도록 프린트 (pytest -s 로 실행할 때 표시됨)
    print("\n\n======== [ 파싱 결과 예시 ] ========")
    print(result.model_dump_json(indent=2))
    print("==================================\n")


def test_parse_document_entry_preserves_markdown_table_code_spans(tmp_path: Path):
    sample_file = tmp_path / "markdown-code-spans.md"
    sample_file.write_text(
        "# Runtime policy\n\n"
        "Use `serving_concurrency` for admission.\n\n"
        "| Key | Value |\n"
        "|---|---|\n"
        "| preset | `speed`/`balanced` |\n"
        "| expression | ``literal ` backtick`` |\n"
        "| alternatives | `A | B & C` |\n"
        "| path | `data/config/<scope>/llm_runtime.json` |\n\n"
        "~~~bash\n"
        "python install.py --run\n"
        "~~~\n\n"
        "After code block.\n",
        encoding="utf-8",
    )

    result = parse_document_entry(str(sample_file))
    parsed_text = "\n".join(chunk.serialized_text for chunk in result.chunks)

    for expected in (
        "serving_concurrency",
        "speed",
        "balanced",
        "literal ` backtick",
        "A | B & C",
        "data/config/<scope>/llm_runtime.json",
        "python install.py --run",
        "After code block.",
    ):
        assert expected in parsed_text


def test_parse_document_entry_with_file(tmp_path: Path):
    sample_file = Path(__file__).resolve().parents[1] / "test_files" / "hwpx_test.hwpx"
    
    if not sample_file.exists():
        pytest.skip(f"Test file not found: {sample_file}")

    result = parse_document_entry(str(sample_file))

    assert isinstance(result, ParseResult)
    assert result.file_ext == ".hwpx"
    assert result.parser_mode == "convert_string_md"
    
    # chunks
    assert isinstance(result.chunks, list)
    assert len(result.chunks) > 0

    first_chunk = result.chunks[0]
    assert first_chunk.chunk_index >= 0
    assert isinstance(first_chunk.serialized_text, str)
    assert first_chunk.serialized_text.strip() != ""
    # Extract-only parsing must not require an embedding model/tokenizer.
    assert first_chunk.tokens is None

    # print("\n\n======== [ 파싱 결과 예시 (.hwpx) ] ========")
    # print(result.model_dump_json(indent=2))
    # print("==================================\n")


if __name__ == "__main__":
    pytest.main([__file__, "-s", "-k", "test_parse_document_entry_with_file"])
