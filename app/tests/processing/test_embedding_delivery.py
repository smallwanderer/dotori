from uuid import uuid4

import pytest

from documents.processing.embedding_contract import (
    INTENT_SCHEMA, EmbeddingContractError, EmbeddingDelivery,
    parse_embedding_command, parse_embedding_intent, parse_embedding_delivery,
)
from tests.processing.test_embedding_contract import payload


def intent_payload():
    value = payload()
    value.pop("input")
    value["schema"] = INTENT_SCHEMA
    return value


def test_intent_canonical_and_binds_only_concrete_input():
    intent = parse_embedding_intent(intent_payload())
    command = parse_embedding_command(payload())
    assert intent.bind(command.input) == command
    assert "input" not in intent.to_payload()
    assert parse_embedding_intent(intent.to_payload()) == intent


def test_delivery_detached_roundtrip():
    delivery = EmbeddingDelivery(parse_embedding_command(payload()), str(uuid4()), "a" * 64)
    value = delivery.to_payload()
    assert parse_embedding_delivery(value) == delivery
    value["command"]["selector"]["ids"].append(999)
    assert delivery.command.selector.ids == (41, 42)


@pytest.mark.parametrize("parser,value", [
    (parse_embedding_delivery, intent_payload()),
    (parse_embedding_delivery, payload()),
    (parse_embedding_command, intent_payload()),
    (parse_embedding_intent, payload()),
])
def test_phase_payloads_are_not_interchangeable(parser, value):
    with pytest.raises(EmbeddingContractError):
        parser(value)


@pytest.mark.parametrize("mutation", ["id", "hash", "extra", "input"])
def test_delivery_rejects_invalid_reference_or_command(mutation):
    value = EmbeddingDelivery(parse_embedding_command(payload()), str(uuid4()), "a" * 64).to_payload()
    if mutation == "id":
        value["manifest"]["id"] = "not-a-uuid"
    elif mutation == "hash":
        value["manifest"]["fingerprint"] = "A" * 64
    elif mutation == "extra":
        value["manifest"]["targets"] = [41]
    else:
        value["command"]["input"] = {}
    with pytest.raises(EmbeddingContractError):
        parse_embedding_delivery(value)
