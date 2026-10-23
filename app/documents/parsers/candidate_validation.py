"""Docling/tokenizer diagnostics; never load an embedding model or persist work."""
import hashlib
import json
import re

from .tokenizers import load_runtime_tokenizer, wrap_chunk_tokenizer


def parser_report_digest(report):
    data = {k: v for k, v in report.items() if k != "report_digest"}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False,
                                   separators=(",", ":")).encode()).hexdigest()


def validate_parser_candidate(runtime, *, document_file=None, headroom=128, max_chunks=64):
    report = {"schema_version": 1, "kind": "parser_candidate", "passed": False,
              "candidate": runtime.model_dump(mode="json"), "checks": {}, "chunks": [],
              "failures": [], "remaining_checks": ["embedding_execution", "production_applicability", "retrieval_quality"]}
    stage = "tokenizer_loading"
    try:
        loaded = load_runtime_tokenizer(runtime, allow_fallback=False)
        report["tokenizer"] = loaded.identity()
        observed = loaded.observed_revision
        if observed and re.fullmatch(r"[0-9a-f]{7,40}", runtime.tokenizer_revision) and not observed.startswith(runtime.tokenizer_revision):
            raise ValueError("Loaded tokenizer revision differs from the candidate")
        if not observed:
            report["remaining_checks"].append("tokenizer_artifact_identity")
        report["checks"][stage] = True
        stage = "chunking"
        if headroom < 0 or max_chunks <= 0:
            raise ValueError("headroom must be nonnegative and max_chunks must be positive")
        budget = runtime.max_tokens - headroom
        if budget <= 0:
            raise ValueError("Chunk token budget must be positive")
        tokenizer = wrap_chunk_tokenizer(loaded, max_tokens=budget)
        from docling.chunking import HybridChunker
        from documents.parsers.docling_parser import CustomSerializerProvider
        from documents.parsers.text_utils import normalize_extracted_text, _restore_protected_table_entities
        if document_file is None:
            from docling_core.types.doc import DoclingDocument, DocItemLabel
            document = DoclingDocument(name="Dotori candidate fixture")
            document.add_heading(text="Candidate document", level=1)
            for index in range(6):
                document.add_text(label=DocItemLabel.TEXT, text=f"Section {index}. " +
                    "Dotori stores local documents and retrieves relevant passages. " * 8)
            report["source"] = {"kind": "built_in_document"}
            report["remaining_checks"].append("file_conversion")
        else:
            from .config import get_converter
            converted = get_converter().convert(document_file)
            status = getattr(converted.status, "value", str(converted.status))
            if str(status).lower() != "success":
                raise ValueError(f"Document conversion did not fully succeed: {status}")
            document = converted.document
            report["source"] = {"kind": "file", "name": document_file.name,
                                "sha256": hashlib.sha256(document_file.read_bytes()).hexdigest()}
        chunker = HybridChunker(tokenizer=tokenizer, merge_peers=True,
                                serializer_provider=CustomSerializerProvider())
        for chunk in chunker.chunk(document):
            text = normalize_extracted_text(_restore_protected_table_entities(chunker.contextualize(chunk=chunk)))
            if not text:
                continue
            if len(report["chunks"]) >= max_chunks:
                raise ValueError("Candidate document exceeds max_chunks; use a smaller fixture or raise the explicit limit")
            report["chunks"].append({"text": text, "tokens": tokenizer.count_tokens(text)})
        if not report["chunks"]:
            raise ValueError("Parser produced no nonempty chunks")
        report["chunk_budget"] = budget
        report["headroom"] = headroom
        report["checks"][stage] = True
        report["passed"] = True
    except Exception as exc:
        report["checks"][stage] = False
        report["failures"].append({"check": stage, "error": str(exc)})
    report["report_digest"] = parser_report_digest(report)
    return report
