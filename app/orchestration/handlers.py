"""Resolve implementations from existing static declarations, never eagerly."""
from importlib import import_module

from capabilities.execution import get_execution_role
from capabilities.work_registry import get_work_definition
from orchestration.interfaces import ReceiptRepository, WorkHandler, PlanDomainAdapter


def _load(path):
    if not path:
        raise ValueError("No executor implementation is registered")
    module, attribute = path.rsplit(".", 1)
    return getattr(import_module(module), attribute)


def get_handler(kind, role) -> WorkHandler:
    definition = get_work_definition(kind)
    if definition.execution_role != role:
        raise ValueError("This executor does not own the requested work")
    handler = _load(definition.handler_path)
    if handler.kind != kind:
        raise ValueError("Handler kind does not match its declaration")
    return handler


def get_receipt_repository(role) -> ReceiptRepository:
    return _load(get_execution_role(role).receipt_repository_path)


def get_plan_adapter(kind, *, preparation=False) -> PlanDomainAdapter:
    from capabilities.work_registry import get_preparation_work_definition
    definition = get_preparation_work_definition(kind) if preparation else get_work_definition(kind)
    return _load(definition.plan_adapter_path)
