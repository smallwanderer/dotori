from dataclasses import replace
from uuid import uuid4

import pytest

from documents.processing.embedding_batches import bind_embedding_batch, validate_embedding_batch
from documents.processing.embedding_contract import EmbeddingContractError


def args():
    return dict(parent_job_id="parent", manifest_id=str(uuid4()),
        manifest_fingerprint="a" * 64, target_ids=(42, 41), approved_target_ids=(41, 42, 43))


def test_identity_is_stable_for_same_subset_and_partition_is_executor_owned():
    values = args()
    batch = bind_embedding_batch(**values)
    assert bind_embedding_batch(**{**values, "target_ids": (41, 42)}) == batch
    assert bind_embedding_batch(**{**values, "target_ids": (41,)}).batch_id != batch.batch_id
    evidence = {key: value for key, value in values.items() if key != "target_ids"}
    assert validate_embedding_batch(batch, **evidence) == (41, 42)


@pytest.mark.parametrize("field,value", [
    ("target_ids", (99,)), ("target_ids", (41, 41)), ("target_ids", (True,)),
    ("target_ids", ()), ("approved_target_ids", ()), ("manifest_id", "path/to/file"),
    ("manifest_fingerprint", "A" * 64), ("parent_job_id", " parent"),
])
def test_invalid_or_expanded_subset_rejected(field, value):
    with pytest.raises(EmbeddingContractError):
        bind_embedding_batch(**{**args(), field: value})


@pytest.mark.parametrize("mutation", ["job", "manifest", "fingerprint", "subset", "batch_id"])
def test_forged_binding_or_changed_parent_rejected(mutation):
    values = args()
    batch = bind_embedding_batch(**values)
    if mutation == "job":
        batch = replace(batch, parent_job_id="other")
    elif mutation == "manifest":
        batch = replace(batch, manifest_id=str(uuid4()))
    elif mutation == "fingerprint":
        batch = replace(batch, manifest_fingerprint="b" * 64)
    elif mutation == "subset":
        batch = replace(batch, target_ids=(41,))
    else:
        batch = replace(batch, batch_id="fake")
    with pytest.raises(EmbeddingContractError):
        validate_embedding_batch(batch, **{key: value for key, value in values.items() if key != "target_ids"})
