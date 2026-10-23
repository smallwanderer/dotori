"""Explicit offline probes: do not change runtime, plan, or stored vectors."""
import json
from pathlib import Path
import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from documents.services.embedding_runtime_config import load_embedding_runtime
from llm_installation.embedding_catalog.loader import get_embedding_catalog_entry
from llm_installation.embedding_function_evaluator import evaluate_text_embed_contract
from llm_installation.function_catalog import FunctionEvidenceRecord
from llm_installation.function_evaluator import _normalize_base_url
from documents.management.commands.evaluate_text_generate import Command as GenerateCommand

class Command(BaseCommand):
    help = "Evaluate the selected embedding contract without changing deployment or vectors."
    def add_arguments(self, parser):
        for name in ("base-url", "runtime-config", "backend-version", "evaluation-id", "result-output", "evidence-output"):
            parser.add_argument("--" + name, required=True)
        parser.add_argument("--timeout", type=float, default=120)

    def handle(self, *args, **opts):
        paths = [Path(opts[k]) for k in ("result_output", "evidence_output")]
        if paths[0].resolve() == paths[1].resolve() or any(p.exists() for p in paths):
            raise CommandError("Use distinct new output paths")
        runtime_path = Path(opts["runtime_config"])
        if not runtime_path.is_file():
            raise CommandError("Explicit runtime config required; legacy fallback is not an evaluation target")
        runtime = load_embedding_runtime(path=runtime_path)
        entry = get_embedding_catalog_entry(runtime.catalog_id)
        fields = {"model_id": "repo_id", "model_revision": "revision", "provider": "provider",
            "dimension": "dimension", "store": "store", "normalize_embeddings": "normalize_embeddings",
            "distance_strategy": "distance_strategy", "supports_sparse": "supports_sparse",
            "query_prefix": "query_prefix", "document_prefix": "document_prefix",
            "tokenizer_id": "tokenizer_id", "tokenizer_revision": "tokenizer_revision"}
        if entry is None or any(getattr(runtime,k) != getattr(entry,v) for k,v in fields.items()):
            raise CommandError("Runtime and catalog contracts differ")
        if getattr(runtime, "provider_options", {}) != getattr(entry, "provider_options", {}):
            raise CommandError("Runtime and catalog provider options differ")
        if getattr(runtime, "tokenizer_options", {}) != getattr(entry, "tokenizer_options", {}):
            raise CommandError("Runtime and catalog tokenizer options differ")
        base_url = _normalize_base_url(opts["base_url"])
        try:
            response = requests.get(base_url + "/readyz", timeout=opts["timeout"])
            ready = response.json() if response.status_code == 200 else {}
        except (requests.RequestException, ValueError):
            ready = {}
        expected = dict(status="ready", runtime_fingerprint=runtime.runtime_fingerprint,
            model_name=runtime.model_id, backend=runtime.provider, dimension=runtime.dimension,
            model_revision=runtime.model_revision)
        if not isinstance(ready,dict) or any(ready.get(k) != v for k,v in expected.items()) or runtime.model_revision in ("main","legacy",""):
            GenerateCommand._write_new_json(paths[0], dict(evaluation_id=opts["evaluation_id"],
                function_id="text.embed", passed=False, checks={"loaded_artifact_identity":False}, evidence=None))
            raise CommandError("Executor identity mismatch; failure report saved, no evidence issued")
        from transformers import AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained(runtime.tokenizer_id, revision=runtime.tokenizer_revision, local_files_only=True)
        cap = min(32,runtime.max_tokens)
        text = "Dotori documents are stored locally. " * 100
        prefix = runtime.document_prefix
        ids = tokenizer(prefix + text, truncation=True, max_length=cap)["input_ids"]
        short = tokenizer.decode(ids,skip_special_tokens=True)
        if len(tokenizer(prefix + text,truncation=False)["input_ids"]) <= cap or not short.startswith(prefix):
            raise CommandError("Token fixture cannot verify this adapter")
        short = short[len(prefix):]
        report = evaluate_text_embed_contract(base_url=base_url,runtime=runtime,model_id=entry.model_id,
            backend_version=opts["backend_version"],evaluation_id=opts["evaluation_id"],
            token=settings.EMBEDDING_INTERNAL_TOKEN,timeout=opts["timeout"],token_fixture=(text,short,cap))
        report["checks"].update(loaded_artifact_identity=True,index_store_compatibility=self._store_matches(runtime))
        report["remaining_checks"] = []
        report["passed"] = all(report["checks"].values())
        report["failures"] = [k for k,v in report["checks"].items() if not v]
        if report["passed"]:
            report["evidence"] = self._evidence(report,runtime,entry,opts).model_dump(mode="json")
        GenerateCommand._write_new_json(paths[0],report)
        if not report["passed"]:
            raise CommandError("text.embed evaluation failed; no evidence issued")
        GenerateCommand._write_new_json(paths[1],report["evidence"])
        self.stdout.write("text.embed contract passed; report and evidence saved")

    @staticmethod
    def _store_matches(runtime):
        from documents.embedding.stores.pgvector_chunk import DIMENSION_FIELD_MAP
        from documents.embedding.indexes import ACTIVE_HNSW_INDEX
        from documents.models import ChunkEmbedding
        field = DIMENSION_FIELD_MAP.get(runtime.dimension)
        if not field or runtime.store != f"pgvector_chunk_{runtime.dimension}":
            return False
        table = ChunkEmbedding._meta.db_table
        column = ChunkEmbedding._meta.get_field(field).column
        with connection.cursor() as cursor:
            cursor.execute("SELECT format_type(a.atttypid,a.atttypmod) FROM pg_attribute a WHERE a.attrelid=to_regclass(%s) AND a.attname=%s AND NOT a.attisdropped",[table,column])
            typ = cursor.fetchone()
            cursor.execute("SELECT indexdef FROM pg_indexes WHERE schemaname=current_schema() AND tablename=%s AND indexname=%s",[table,ACTIVE_HNSW_INDEX])
            idx = cursor.fetchone()
        return bool(typ and typ[0] == f"vector({runtime.dimension})" and idx and "USING hnsw" in idx[0]
            and f"{column} vector_ip_ops" in idx[0] and runtime.distance_strategy == "inner_product" and runtime.normalize_embeddings)

    @staticmethod
    def _evidence(report,runtime,entry,opts):
        return FunctionEvidenceRecord.model_validate(dict(evidence_id=opts["evaluation_id"],function_id="text.embed",
            verification="contract_verified",model_id=entry.model_id,model_revision=runtime.model_revision,
            contract_version="text-embed-v1",targets=[dict(target_id=entry.id,target_revision=runtime.model_revision,
            artifact_digest=runtime.model_revision,backend_id=runtime.provider,backend_version=opts["backend_version"],
            adapter_contract="dotori-internal-embed-v1")],input_preprocessing_version="dotori-prefix-token-limit-v1",
            output_parsing_version="dotori-dense-sparse-v1",inference_options=dict(runtime_fingerprint=runtime.runtime_fingerprint,
            max_tokens=runtime.max_tokens,tested_token_cap=min(32,runtime.max_tokens),
            query_prefix=runtime.query_prefix,document_prefix=runtime.document_prefix,
            normalize_embeddings=runtime.normalize_embeddings,dimension=runtime.dimension,
            store=runtime.store,distance_strategy=runtime.distance_strategy),fixture_set_id="dotori-text-embed-http-smoke",
            fixture_set_revision="v2",evaluator_version="dotori-text-embed-contract-v1",executed_at=report["executed_at"],
            sample_count=report["sample_count"],contract_checks=report["checks"],passed=True))
