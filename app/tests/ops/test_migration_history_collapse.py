import re

import pytest
from django.apps import apps
from django.db.migrations.loader import MigrationLoader

from accounts.management.commands.collapse_migration_history import (
    COLLAPSED_APPS,
    INDEX_RENAMES,
    MOVED_MODELS,
    MOVED_TABLES,
    NEW_MIGRATION,
    REMOVED_TABLES,
    _table_renames,
)

pytestmark = pytest.mark.unit

# Django derives a name like "document_ai_owner_i_20a6ea_idx" from the table
# name when Meta.indexes omits one, so such a name differs between a fresh
# database and one upgraded from the old label.
AUTO_GENERATED_INDEX_NAME = re.compile(r"^\w+_[0-9a-f]{6}_idx$")

COLLAPSED_LABELS = set(COLLAPSED_APPS) | {"documents"}


def _app_models(label="documents"):
    return list(apps.get_app_config(label).get_models())


def test_every_documents_table_uses_the_new_label_prefix():
    for model in _app_models():
        assert model._meta.db_table.startswith("documents_"), model.__name__


def test_upgrade_renames_every_documents_table():
    renames = _table_renames()
    initial_apps = MigrationLoader(None).project_state([("documents", NEW_MIGRATION)]).apps
    initial_tables = {model._meta.db_table for model in initial_apps.get_app_config("documents").get_models()}
    assert len(renames) == len(initial_tables) - len(MOVED_TABLES)
    assert all(old.startswith("document_ai_") for old in renames)
    assert set(renames.values()) | set(MOVED_TABLES.values()) == initial_tables
    assert set(renames.values()).isdisjoint(MOVED_TABLES.values())


def test_post_initial_plan_table_is_created_by_feature_migration_not_renamed():
    from orchestration_state.models import PlanSubmission
    assert PlanSubmission._meta.db_table not in _table_renames().values()


def test_no_documents_index_keeps_an_auto_generated_name():
    """Auto-generated names would not survive the table rename identically."""
    for model in _app_models():
        for index in model._meta.indexes:
            assert not AUTO_GENERATED_INDEX_NAME.match(index.name), (model.__name__, index.name)


def test_index_renames_target_names_the_models_declare():
    declared = {index.name for model in _app_models() for index in model._meta.indexes}
    assert set(INDEX_RENAMES.values()) <= declared
    assert len(set(INDEX_RENAMES.values())) == len(INDEX_RENAMES)


def test_collapsed_apps_root_at_the_migration_the_command_records():
    """The command marks NEW_MIGRATION as applied and lets migrate do the rest,
    so it only requires that name to be each app's root -- later feature
    migrations are applied normally on top and need no change here."""
    loader = MigrationLoader(None, ignore_no_migrations=True)
    for label in sorted(COLLAPSED_LABELS):
        names = sorted(name for app, name in loader.graph.nodes if app == label)
        assert names, f"{label} has no migrations"
        assert names[0] == NEW_MIGRATION, (label, names[0])


def test_marker_tables_exist_in_the_collapsed_apps():
    """The command probes these to tell a complete database from a pre-history one."""
    for label, (_, marker_table) in COLLAPSED_APPS.items():
        tables = {model._meta.db_table for model in _app_models(label)}
        assert marker_table in tables, (label, marker_table)


def test_removed_tables_are_not_backing_a_live_model():
    """The command drops these outright, so a name still in use would take
    the live table with it."""
    live = {model._meta.db_table for model in apps.get_models()}
    assert set(REMOVED_TABLES).isdisjoint(live), sorted(set(REMOVED_TABLES) & live)


def test_moved_tables_old_names_are_not_backing_a_live_model():
    """The old (pre-move) table name must be free -- it's renamed onto, not
    dropped, so a name still in use elsewhere would collide."""
    live = {model._meta.db_table for model in apps.get_models()}
    assert set(MOVED_TABLES).isdisjoint(live), sorted(set(MOVED_TABLES) & live)


def test_moved_tables_new_names_match_a_live_model():
    """The new name is where the model actually lives now -- if a future
    rename changes the table name again without updating this map, the
    command would rename an upgrading database onto a table nothing reads."""
    live = {model._meta.db_table for model in apps.get_models()}
    assert set(MOVED_TABLES.values()) <= live, sorted(set(MOVED_TABLES.values()) - live)


def test_moved_models_resolve_under_their_new_app_label():
    """MOVED_MODELS records where each model used to live; confirm it
    actually lives under the new label now, not still the old one."""
    for model_name, (old_app, new_app) in MOVED_MODELS.items():
        model = apps.get_model(new_app, model_name)
        assert model._meta.app_label == new_app
        assert old_app != new_app


@pytest.mark.parametrize("tables, expected", [
    ({"old", "new"}, "target already exists"),
    (set(), "source table is missing"),
])
def test_upgrade_rejects_missing_sources_and_colliding_targets(tables, expected):
    from django.core.management.base import CommandError
    from accounts.management.commands.collapse_migration_history import Command
    command = Command()
    command.tables = tables
    with pytest.raises(CommandError, match=expected):
        command._validate_table_renames({"old": "new"})


def test_upgrade_accepts_existing_source_and_new_target():
    from accounts.management.commands.collapse_migration_history import Command
    command = Command()
    command.tables = {"old"}
    command._validate_table_renames({"old": "new"})
