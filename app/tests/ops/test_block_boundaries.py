"""Import- and write-level guards for the ownership rules documented in
dotori-docs/architecture/crosscutting/boundaries.md and
dev-docs/block-ownership-violations-plan.md item 7.

`ast.walk` is used instead of a module-top-level scan because the violations
this project has actually hit hid behind function-local imports: `SyncQuota`
(item 1) and the former cross-import between `documents.services
.rag_runtime_config` and `llm_installation` (item 6) both passed a
top-of-file-only import lint. A scanner that only checks module-level
imports would have missed both.

This deliberately does not try to forbid every cross-app import -- most of
them are normal layered reads (B4 reading B3's `Node`, B3 reading B2's
`Workspace`, and so on) and blanket-forbidding them would just produce an
allowlist nobody maintains. It encodes the specific invariants that
crosscutting/boundaries.md and this plan actually rely on.
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

APP_ROOT = Path(__file__).resolve().parents[2]


def _is_top_level(node: ast.AST, tree: ast.Module) -> bool:
    return node in tree.body


def _imports_of(path: Path, target: str) -> tuple[int, int]:
    """(module-level, local) count of imports naming `target` as the top
    package, anywhere in the file -- including inside function bodies."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    top_level = 0
    local = 0
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == target:
                    name = target
                    break
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module and node.module.split(".")[0] == target:
                name = target
        if name is None:
            continue
        if _is_top_level(node, tree):
            top_level += 1
        else:
            local += 1
    return top_level, local


def _any_import_of(root: Path, target: str) -> dict[str, tuple[int, int]]:
    hits = {}
    for path in root.rglob("*.py"):
        if "migrations" in path.parts or "__pycache__" in path.parts:
            continue
        top_level, local = _imports_of(path, target)
        if top_level or local:
            hits[str(path.relative_to(APP_ROOT))] = (top_level, local)
    return hits


def test_llm_installation_never_imports_documents():
    """AGENTS.md/CLAUDE.md: `llm_installation` must never import `documents`
    -- it has to work at install time, before Django's app registry (and so
    before any model) exists. `rag_runtime_config.py` broke this while living
    in `documents/services/`; moving it into `llm_installation` is what makes
    this invariant true today (block-ownership-violations-plan.md item 6).
    A regression would most likely come back as a function-local import, the
    same way the original violation hid, hence `ast.walk` and not a
    top-of-file-only check."""
    hits = _any_import_of(APP_ROOT / "llm_installation", "documents")
    assert not hits, sorted(hits)


def test_workspaces_api_v1_views_imports_documents_at_module_level():
    """This file delegates quality-profile/evaluation/LLM-endpoint requests to
    `documents` (B4) -- a normal B2-view-calls-B4-service direction once those
    tables moved to `documents` (block-ownership-violations-plan.md item 3).
    The imports used to be function-local, which hid the dependency from
    static analysis; they were promoted to module level once it was confirmed
    `documents.search`/`documents.services.llm_endpoint_service` don't pull in
    torch/docling at import time, so the lightweight `dotori-app-web` image
    still imports this file cleanly. A local import reappearing here would be
    a regression back to that hidden state, hence asserting `local == 0`."""
    path = APP_ROOT / "workspaces" / "api_v1" / "views.py"
    top_level, local = _imports_of(path, "documents")
    assert local == 0, "a function-local import here hides the B2->B4 dependency again"
    assert top_level > 0, "this file is expected to depend on documents"


FOUNDATION_PACKAGES = ("capabilities", "orchestration", "orchestration_state")  # B6, B7

# Packages whose Django models define a block's state. B5 and B6 own no tables,
# so they only appear on the writer side.
STATE_PACKAGES = {
    "accounts": "B1",
    "workspaces": "B2",
    "files": "B3",
    "documents": "B4",
    "orchestration_state": "B7",
}
WRITER_PACKAGES = {
    **STATE_PACKAGES,
    "llm_installation": "B5",
    "capabilities": "B6",
    "orchestration": "B7",
}
MANAGER_WRITES = {
    "create", "update", "delete", "bulk_create", "bulk_update",
    "update_or_create", "get_or_create",
}


def test_foundation_blocks_never_import_documents_or_llm_installation():
    """B6 (capabilities) and B7 (orchestration) are read by the installer, the
    web process and the executors alike, so they must stay free of the feature
    module and of the installation tooling: a foundation block that knows a
    plugin has to change whenever a plugin is added. Both were measured at zero
    imports before this guard existed. `ast.walk` keeps a function-local import
    from hiding a regression, as in the tests above."""
    hits = {}
    for package in FOUNDATION_PACKAGES:
        for target in ("documents", "llm_installation"):
            for path, counts in _any_import_of(APP_ROOT / package, target).items():
                hits[f"{path} -> {target}"] = counts
    assert not hits, sorted(hits)


def _is_model_class(node):
    for base in node.bases:
        name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
        if name == "Model" or name.endswith(("Model", "Base")):
            return True
    return False


def _source_trees(package):
    for path in (APP_ROOT / package).rglob("*.py"):
        parts = path.parts
        if "migrations" in parts or "tests" in parts or "__pycache__" in parts:
            continue
        if path.name == "tests.py" or path.name.startswith("test_"):
            continue
        yield path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _model_owners():
    owners = {}
    ambiguous = set()
    for package, block in STATE_PACKAGES.items():
        for _path, tree in _source_trees(package):
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and _is_model_class(node):
                    if owners.get(node.name, block) != block:
                        ambiguous.add(node.name)
                    owners[node.name] = block
    return {name: block for name, block in owners.items() if name not in ambiguous}


def _manager_write_target(call):
    """`Model` for a `Model.objects....<write>(...)` call, otherwise None."""
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr in MANAGER_WRITES):
        return None
    node, via_manager = func.value, False
    while isinstance(node, (ast.Attribute, ast.Call)):
        if isinstance(node, ast.Attribute):
            via_manager = via_manager or node.attr == "objects"
            node = node.value
        else:
            node = node.func
    return node.id if via_manager and isinstance(node, ast.Name) else None


def test_blocks_never_write_another_blocks_models():
    """The rule that a block writes only its own state (crosscutting/boundaries.md) is the
    criterion the block boundaries rest on, so it needs a check that can fail.
    This is a lower bound: it sees `Model.objects...create/update/delete`-style
    manager writes by model name, not `instance.save()`, related managers or raw
    SQL, and it skips model names that two blocks share. Reads cross blocks on
    purpose (B4 reads Files and Workspaces) and are not restricted here."""
    owners = _model_owners()
    assert set(owners.values()) == set(STATE_PACKAGES.values()), owners
    own_writes = 0
    hits = set()
    for package, block in WRITER_PACKAGES.items():
        for path, tree in _source_trees(package):
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                model = _manager_write_target(node)
                if model not in owners:
                    continue
                if owners[model] == block:
                    own_writes += 1
                else:
                    hits.add(f"{path.relative_to(APP_ROOT)}: {block} writes {model} ({owners[model]})")
    # Positive control: a detector that sees no write at all would pass vacuously.
    assert own_writes > 0, "detector no longer sees any manager write"
    assert not hits, sorted(hits)
