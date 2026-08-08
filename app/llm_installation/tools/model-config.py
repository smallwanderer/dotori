"""
모델 카탈로그 메타데이터 리졸버

3단계 fallback으로 모델 스펙(architecture, hidden_size, num_hidden_layers,
num_attention_heads, num_key_value_heads, max_context_length, 파라미터 수,
다운로드 크기)을 조회한다.

1단계: 대상 리포(GGUF 리포 포함)에 AutoConfig 시도
       -> unsloth류 리포처럼 config.json이 같이 올라와 있으면 여기서 끝남
2단계: 1단계 실패 시, 지정된 원본(safetensors) 리포로 AutoConfig 재시도
       -> google 공식 GGUF 전용 리포처럼 config.json이 없거나 불완전한 경우
3단계: 그래도 실패하면 GGUF 파일 헤더를 직접 파싱 (gguf 라이브러리)
       -> config.json 자체가 아예 없는 최악의 경우 대비

파라미터 수(총 params)와 다운로드 크기는 별도로 HF API를 통해 항상 조회한다.
"""

from __future__ import annotations

import argparse
import os
import sys
import dataclasses
import json
import math
from pathlib import Path
from typing import Optional

HF_API_BASE = "https://huggingface.co/api/models"
SPEC_FIELDS = (
    "architecture",
    "text_architecture",
    "hidden_size",
    "num_hidden_layers",
    "num_attention_heads",
    "num_key_value_heads",
    "head_dim",
    "max_context_length",
)
CORE_SPEC_FIELDS = (
    "architecture",
    "hidden_size",
    "num_hidden_layers",
    "num_attention_heads",
    "max_context_length",
)

# Architectures whose head_dim is NOT hidden_size // num_attention_heads.
# Maps architecture name substring (lowercased) -> canonical head_dim.
# Used as the first fallback when head_dim cannot be read from the config.
_KNOWN_HEAD_DIM: dict[str, int] = {
    "gemma3": 256,
    "gemma4": 256,
    "qwen3": 128,
    "qwen3_5": 128,
    "qwen3_moe": 128,
}


@dataclasses.dataclass
class ModelSpec:
    architecture: Optional[str] = None
    text_architecture: Optional[str] = None
    hidden_size: Optional[int] = None
    num_hidden_layers: Optional[int] = None
    num_attention_heads: Optional[int] = None
    num_key_value_heads: Optional[int] = None
    head_dim: Optional[int] = None
    max_context_length: Optional[int] = None
    source: Optional[str] = None  # "autoconfig:<repo>" | "gguf_header:<repo>/<file>"


class MetadataResolutionError(Exception):
    pass


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _strip_env_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _read_hf_token_from_dotenv(dotenv_path: Optional[Path] = None) -> Optional[str]:
    path = dotenv_path or (_repo_root() / ".env")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip() == "HF_TOKEN":
            token = _strip_env_value(value)
            return token or None
    return None


def resolve_hf_token(
    token: Optional[str] = None,
    *,
    dotenv_path: Optional[Path] = None,
) -> Optional[str]:
    """Use explicit token, environment HF_TOKEN, then repo .env HF_TOKEN."""
    return token or os.environ.get("HF_TOKEN") or _read_hf_token_from_dotenv(dotenv_path)


def _headers(token: Optional[str]) -> dict:
    token = resolve_hf_token(token)
    return {"Authorization": f"Bearer {token}"} if token else {}


def _extract_from_config(config, source_tag: str) -> ModelSpec:
    """AutoConfig 결과에서 text_config 유무에 따라 값을 안전하게 뽑아낸다."""
    tc = getattr(config, "text_config", None) or config  # 멀티모달이면 text_config, 아니면 flat
    max_context_length = next(
        (
            getattr(tc, attr, None)
            for attr in (
                "max_position_embeddings",
                "seq_length",
                "n_positions",
                "model_max_length",
            )
            if getattr(tc, attr, None)
        ),
        None,
    )

    # head_dim: read explicitly from text_config first.
    # Some architectures (Gemma 3/4, Qwen3/3.5) declare it separately from
    # hidden_size / num_attention_heads, so we must not synthesize it here.
    head_dim: Optional[int] = getattr(tc, "head_dim", None)

    return ModelSpec(
        architecture=getattr(config, "model_type", None),
        text_architecture=getattr(tc, "model_type", None),
        hidden_size=getattr(tc, "hidden_size", None),
        num_hidden_layers=getattr(tc, "num_hidden_layers", None),
        num_attention_heads=getattr(tc, "num_attention_heads", None),
        num_key_value_heads=getattr(tc, "num_key_value_heads", None),
        head_dim=head_dim,
        max_context_length=max_context_length,
        source=source_tag,
    )


def _has_any_spec_value(spec: ModelSpec) -> bool:
    return any(getattr(spec, field) is not None for field in SPEC_FIELDS)


def _has_core_spec_values(spec: ModelSpec) -> bool:
    return all(getattr(spec, field) is not None for field in CORE_SPEC_FIELDS)


def _merge_specs(specs: list[ModelSpec]) -> Optional[ModelSpec]:
    merged = ModelSpec()
    sources = []
    for spec in specs:
        if not spec:
            continue
        for field in SPEC_FIELDS:
            if getattr(merged, field) is None:
                value = getattr(spec, field)
                if value is not None:
                    setattr(merged, field, value)
        if spec.source:
            sources.append(spec.source)
    if not _has_any_spec_value(merged):
        return None
    merged.source = " + ".join(sources) if sources else None
    return merged


def try_autoconfig(repo_id: str, token: Optional[str]) -> Optional[ModelSpec]:
    """1단계 / 2단계 공용: 주어진 리포에 AutoConfig 시도. 실패하면 None."""
    try:
        from transformers import AutoConfig

        token = resolve_hf_token(token)
        config = AutoConfig.from_pretrained(repo_id, token=token)
        spec = _extract_from_config(config, source_tag=f"autoconfig:{repo_id}")

        if _has_any_spec_value(spec):
            print(f"  [OK] {repo_id}: AutoConfig 메타데이터 일부/전체 조회 성공", file=sys.stderr)
        else:
            print(f"  [실패] {repo_id}: AutoConfig에서 사용 가능한 메타데이터 없음", file=sys.stderr)
            return None
        return spec

    except Exception as e:
        print(f"  [실패] {repo_id}: AutoConfig 불가 ({type(e).__name__}: {e})", file=sys.stderr)
        return None


def try_gguf_header(repo_id: str, filename: str, token: Optional[str]) -> Optional[ModelSpec]:
    """3단계: GGUF 파일을 받아서 헤더 메타데이터를 직접 파싱."""
    try:
        from gguf import GGUFReader
    except ImportError:
        raise MetadataResolutionError(
            "gguf 패키지가 없습니다. `pip install gguf` 후 재시도하세요."
        )

    try:
        from huggingface_hub import hf_hub_download

        token = resolve_hf_token(token)
        path = hf_hub_download(repo_id=repo_id, filename=filename, token=token)
        reader = GGUFReader(path)

        def get_field(key: str):
            field = reader.fields.get(key)
            if field is None or not field.data:
                return None
            return field.parts[field.data[0]]

        arch = get_field("general.architecture")
        arch_str = str(arch) if arch is not None else None

        def arch_field(suffix: str):
            if arch_str is None:
                return None
            return get_field(f"{arch_str}.{suffix}")

        # GGUF stores head_dim as attention.key_length (bytes per key head).
        raw_key_length = arch_field("attention.key_length")
        head_dim_gguf: Optional[int] = int(raw_key_length) if raw_key_length is not None else None

        spec = ModelSpec(
            architecture=arch_str,
            text_architecture=arch_str,
            hidden_size=arch_field("embedding_length"),
            num_hidden_layers=arch_field("block_count"),
            num_attention_heads=arch_field("attention.head_count"),
            num_key_value_heads=arch_field("attention.head_count_kv"),
            head_dim=head_dim_gguf,
            max_context_length=arch_field("context_length"),
            source=f"gguf_header:{repo_id}/{filename}",
        )
        print(f"  [OK] {repo_id}/{filename}: GGUF 헤더 파싱 성공", file=sys.stderr)
        return spec

    except Exception as e:
        print(f"  [실패] {repo_id}/{filename}: GGUF 헤더 파싱 불가 ({type(e).__name__}: {e})", file=sys.stderr)
        return None


def resolve_model_spec(
    repo_id: str,
    *,
    fallback_repo_id: Optional[str] = None,
    gguf_filename: Optional[str] = None,
    token: Optional[str] = None,
) -> ModelSpec:
    """
    3단계 fallback으로 스펙을 조회한다.

    repo_id:          대상 리포 (GGUF 리포일 수도, safetensors 리포일 수도 있음)
    fallback_repo_id:  1단계 실패 시 시도할 원본 safetensors 리포 (예: "google/gemma-3-12b-it")
    gguf_filename:     3단계에서 헤더를 읽을 특정 GGUF 파일명 (없으면 리포 내 대표 파일 자동 탐색 필요)
    """
    specs = []

    print(f"[1단계] {repo_id} 에 AutoConfig 시도", file=sys.stderr)
    spec = try_autoconfig(repo_id, token)
    if spec:
        specs.append(spec)
    merged = _merge_specs(specs)
    if merged and _has_core_spec_values(merged):
        return merged

    if fallback_repo_id:
        print(f"[2단계] fallback 리포 {fallback_repo_id} 에 AutoConfig 시도", file=sys.stderr)
        spec = try_autoconfig(fallback_repo_id, token)
        if spec:
            specs.append(spec)
    merged = _merge_specs(specs)
    if merged and _has_core_spec_values(merged):
        return merged

    if gguf_filename:
        print(f"[3단계] GGUF 헤더 직접 파싱 시도", file=sys.stderr)
        spec = try_gguf_header(repo_id, gguf_filename, token)
        if spec:
            specs.append(spec)

    merged = _merge_specs(specs)
    if merged:
        return merged

    raise MetadataResolutionError(
        f"모든 단계 실패: repo_id={repo_id}, fallback_repo_id={fallback_repo_id}, "
        f"gguf_filename={gguf_filename}"
    )


def fetch_download_size_mb(repo_id: str, filename: str, token: Optional[str]) -> Optional[int]:
    """HF API로 특정 파일의 다운로드 크기(MiB)를 조회."""
    import requests

    url = f"{HF_API_BASE}/{repo_id}"
    resp = requests.get(
        url,
        headers=_headers(token),
        params={"blobs": True},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    sibling = next(
        (s for s in data.get("siblings", []) if s.get("rfilename") == filename),
        None,
    )
    if sibling is None:
        return None

    size_bytes = sibling.get("lfs", {}).get("size") or sibling.get("size")
    if not size_bytes:
        return None
    return math.ceil(size_bytes / (1024 * 1024))


def fetch_total_params(safetensors_repo_id: str, token: Optional[str]) -> Optional[int]:
    """원본 safetensors 리포에서 실측 파라미터 수 조회 (양자화와 무관하게 동일)."""
    import requests

    url = f"{HF_API_BASE}/{safetensors_repo_id}"
    resp = requests.get(url, headers=_headers(token), timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data.get("safetensors", {}).get("total")


def _without_none(payload: dict) -> dict:
    clean = {}
    for key, value in payload.items():
        if value is None:
            continue
        if isinstance(value, dict):
            value = _without_none(value)
            if not value:
                continue
        clean[key] = value
    return clean


def _blank_unknown(value):
    if value is None:
        return ""
    if isinstance(value, dict):
        return {key: _blank_unknown(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_blank_unknown(item) for item in value]
    return value


def resolve_head_dim(
    spec: ModelSpec,
) -> tuple[Optional[int], str]:
    """Resolve head_dim with a two-level fallback and return (value, method_tag).

    Priority:
      1. Explicit value from config/GGUF header  -> most accurate.
      2. Architecture known-defaults table        -> empirically verified.
      3. hidden_size // num_attention_heads       -> last resort; may be wrong
                                                    for modern non-standard
                                                    architectures. Emits a
                                                    warning in the extra field.
    """
    # Level 1: already resolved from source.
    if spec.head_dim is not None:
        return spec.head_dim, "explicit"

    # Level 2: architecture known-defaults.
    arch = (spec.text_architecture or spec.architecture or "").lower()
    for key, default_dim in _KNOWN_HEAD_DIM.items():
        if key in arch:
            return default_dim, f"known_default:{key}"

    # Level 3: standard formula — only reliable for classic architectures
    # where hidden_size == num_attention_heads * head_dim strictly holds.
    if spec.hidden_size and spec.num_attention_heads:
        derived = spec.hidden_size // spec.num_attention_heads
        return derived, "derived:hidden_size//num_attention_heads"

    return None, "unknown"


def build_model_metadata_payload(
    spec: ModelSpec,
    *,
    total_params: Optional[int] = None,
) -> dict:
    """Return only fetched metadata fields; do not synthesize missing values."""
    head_dim_value, head_dim_method = resolve_head_dim(spec)

    extra: dict = {
        "metadata_source": spec.source,
        "root_architecture": spec.architecture,
    }
    if head_dim_method not in {"explicit"}:
        # Record how head_dim was derived so catalog reviewers can spot-check.
        extra["head_dim_resolution"] = head_dim_method
    if head_dim_method.startswith("derived:"):
        extra["head_dim_warning"] = (
            "head_dim was calculated as hidden_size // num_attention_heads. "
            "Verify against the official config.json for this architecture."
        )

    payload = {
        "architecture": spec.text_architecture or spec.architecture,
        "parameter_count_b": round(total_params / 1_000_000_000, 3)
        if total_params
        else None,
        "max_context_length": spec.max_context_length,
        "hidden_size": spec.hidden_size,
        "num_hidden_layers": spec.num_hidden_layers,
        "num_attention_heads": spec.num_attention_heads,
        "num_key_value_heads": spec.num_key_value_heads,
        "head_dim": head_dim_value,
        "extra": extra,
    }
    return _without_none(payload)


def build_artifact_payload(
    *,
    format: str,
    quant: str,
    filename: Optional[str] = None,
    dtype: Optional[str] = None,
    download_size_mb: Optional[int] = None,
) -> dict:
    """Return only fetched/provided artifact fields; missing values stay absent."""
    return _without_none(
        {
            "format": format,
            "quant": quant,
            "filename": filename,
            "dtype": dtype,
            "download_size_mb": download_size_mb,
        }
    )


def build_catalog_draft(
    *,
    repo_id: str,
    fallback_repo_id: Optional[str],
    gguf_filename: Optional[str],
    model_id: str = "",
    artifact_id: str = "",
    display_name: str = "",
    artifact_format: str = "",
    quant: str = "",
    dtype: str = "",
    token: Optional[str] = None,
) -> dict:
    spec = resolve_model_spec(
        repo_id=repo_id,
        fallback_repo_id=fallback_repo_id,
        gguf_filename=gguf_filename,
        token=token,
    )
    total_params = fetch_total_params(fallback_repo_id, token) if fallback_repo_id else None
    size_mb = (
        fetch_download_size_mb(repo_id, gguf_filename, token)
        if gguf_filename
        else None
    )
    model_metadata = build_model_metadata_payload(spec, total_params=total_params)
    artifact = build_artifact_payload(
        format=artifact_format,
        quant=quant,
        filename=gguf_filename,
        dtype=dtype,
        download_size_mb=size_mb,
    )

    return _blank_unknown(
        {
            "model": {
                "id": model_id,
                "display_name": display_name,
                "description": "",
                "license": "",
                "model_metadata": {
                    "architecture": model_metadata.get("architecture"),
                    "parameter_count_b": model_metadata.get("parameter_count_b"),
                    "max_context_length": model_metadata.get("max_context_length"),
                    "hidden_size": model_metadata.get("hidden_size"),
                    "num_hidden_layers": model_metadata.get("num_hidden_layers"),
                    "num_attention_heads": model_metadata.get("num_attention_heads"),
                    "num_key_value_heads": model_metadata.get("num_key_value_heads"),
                    "head_dim": model_metadata.get("head_dim"),
                    "extra": model_metadata.get("extra", {}),
                },
                "capabilities": {
                    "modalities": [],
                    "tasks": [],
                    "languages": [],
                },
            },
            "artifact": {
                "id": artifact_id,
                "model_id": model_id,
                "enabled": "",
                "priority": "",
                "display_name": display_name,
                "description": "",
                "hf": {
                    "repo_id": repo_id,
                    "revision": "",
                    "gated": "",
                    "trust_remote_code": "",
                },
                "artifact": {
                    "format": artifact.get("format"),
                    "filename": artifact.get("filename"),
                    "quant": artifact.get("quant"),
                    "dtype": artifact.get("dtype"),
                    "download_size_mb": artifact.get("download_size_mb"),
                    "disk_required_mb": "",
                },
                "backend_profiles": [],
                "policy": {
                    "visible": "",
                    "allow_auto_select": "",
                    "allow_manual_select": "",
                    "safety": "",
                    "tags": [],
                },
            },
        }
    )


def write_catalog_draft(draft: dict, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(draft, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output_path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a model catalog draft from Hugging Face metadata."
    )
    parser.add_argument("--repo-id", help="Artifact repository id on Hugging Face.")
    parser.add_argument(
        "--fallback-repo-id",
        help="Source safetensors repository id used for config/parameter metadata.",
    )
    parser.add_argument("--gguf-filename", help="GGUF filename to inspect or draft.")
    parser.add_argument("--model-id", default="", help="Catalog model id draft value.")
    parser.add_argument("--artifact-id", default="", help="Catalog artifact id draft value.")
    parser.add_argument("--display-name", default="", help="Display name draft value.")
    parser.add_argument("--format", default="", help="Artifact format draft value.")
    parser.add_argument("--quant", default="", help="Artifact quantization draft value.")
    parser.add_argument("--dtype", default="", help="Artifact dtype draft value.")
    parser.add_argument(
        "--output",
        default="model-catalog-draft.json",
        help="Draft JSON output path.",
    )
    parser.add_argument("--stdout", action="store_true", help="Print draft JSON only.")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    if not args.repo_id:
        raise SystemExit("--repo-id is required.")

    token = resolve_hf_token()
    draft = build_catalog_draft(
        repo_id=args.repo_id,
        fallback_repo_id=args.fallback_repo_id,
        gguf_filename=args.gguf_filename,
        model_id=args.model_id,
        artifact_id=args.artifact_id,
        display_name=args.display_name,
        artifact_format=args.format,
        quant=args.quant,
        dtype=args.dtype,
        token=token,
    )

    if args.stdout:
        print(json.dumps(draft, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        output_path = write_catalog_draft(draft, Path(args.output))
        print(f"Draft written: {output_path}", file=sys.stderr)
