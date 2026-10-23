"""One-time upgrade that moves an existing database onto the collapsed history.

Two things happened to the migration history in this release:

* ``document_ai`` was renamed to ``documents``, label included, so its tables,
  sequences and a handful of auto-named indexes carry the old prefix on any
  database created before the rename.
* Every app's accumulated migrations were replaced by a single
  ``0001_initial``. The regenerated files build exactly the same schema, so an
  existing database needs no DDL for this part -- only its recorded history
  has to be rewritten, because the migration names it lists no longer exist.

A model dropped in the same release needs one more step: the regenerated
initial never creates its table, so no migration is left to remove it from a
database that already has one. ``REMOVED_TABLES`` lists those.

A model moved to a different app needs a similar step, but a rename instead
of a drop: migrations are tracked by name, not content, so an existing
database that already recorded the old ``documents: 0001_initial`` as
applied never re-runs it and never creates the table under its new
``documents_*`` name -- the code looks for a table that migrate will not
create. The table has to be renamed in place, along with its sequence and
the model's ``django_content_type`` row. ``MOVED_TABLES``/``MOVED_MODELS``
list those.

None of this is something the migration framework can express, which is why this
lives in a command. The app container runs it before ``migrate`` (see
docker-compose.yml): on a pre-rename database ``migrate`` alone would create
empty ``documents_*`` tables next to the old ones and strand the data, and on
any pre-collapse database it would try to re-apply ``0001_initial`` over
tables that already exist.

It is a no-op on a fresh or already-upgraded database, so it is safe to run on
every start. It lives in ``accounts`` rather than in one of the apps it
rewrites because Django needs a command to belong to an installed app, and
``accounts`` sits lowest in the migration dependency graph -- putting it in
``documents`` would have a plugin rewriting Core's history.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

OLD_LABEL = "document_ai"
NEW_LABEL = "documents"
LAST_OLD_MIGRATION = "0031_ragjob_execution_leases"
NEW_MIGRATION = "0001_initial"

# Indexes whose names Django generated from the old `document_ai_*` table
# names. Every other index and constraint in that app is named explicitly in
# the models, so it already matches what the new 0001_initial creates.
INDEX_RENAMES = {
    "document_ai_scope_1d5cc1_idx": "docs_embgen_scope_status_idx",
    "document_ai_last_us_0334c7_idx": "docs_segemb_last_used_idx",
    "document_ai_service_0e6b10_idx": "docs_res_service_collected_idx",
    "document_ai_parser__bff5ef_idx": "docs_parse_parser_name_idx",
    "document_ai_input_d_af555b_idx": "docs_parse_input_hash_idx",
    "document_ai_owner_i_c5ed6a_idx": "docs_llmep_owner_active_idx",
    "document_ai_owner_i_20a6ea_idx": "docs_rag_own_st_created_idx",
    "document_ai_owner_i_41aa83_idx": "docs_qu_owner_mode_created_idx",
    "document_ai_intent_c6e7af_idx": "docs_qu_intent_created_idx",
    "document_ai_retriev_ed87f9_idx": "docs_qu_retrieval_created_idx",
    "document_ai_owner_i_f2d395_idx": "docs_sj_own_st_created_idx",
}

# Apps whose history was collapsed without a label change. The table listed
# for each one is created partway through the old history, so its presence
# distinguishes a database that already holds the complete schema from one
# that only ever applied the original 0001 -- the two record the same single
# migration name but are not the same database.
COLLAPSED_APPS = {
    "accounts": ("0005_remove_duplicate_token_indexes", "accounts_clitoken"),
    "files": ("0011_remove_duplicate_file_indexes", "files_fileoperationlog"),
    "workspaces": ("0008_merge_quality_profile_axes", "workspaces_workspaceinvitation"),
}

# Models dropped in the same release as the collapse. A regenerated
# 0001_initial simply does not create them, so a fresh install never has the
# table while an existing database still does -- and no migration is left to
# remove it. Dropping it here keeps the two identical.
#
# accounts_syncquota held a second, per-user upload ledger that only the sync
# API maintained. It counted uploads but never deletions, so its values drift
# upward until sync refuses to run. files_userstorage already tracks the same
# bytes per workspace, correctly in both directions, and is now the only
# quota, so the stale rows have nothing to migrate into.
REMOVED_TABLES = ("accounts_syncquota",)

# Models moved to a different app in the same release. Rows and columns are
# unchanged -- only the owning app, and therefore the table prefix, moves.
# ``WorkspaceEvaluationDataset``/``WorkspaceQualityEvaluationRun`` were
# `workspaces`-owned tables that only `documents` ever wrote to (B4's
# evaluation work data, not B2 policy). ``WorkspaceQualityProfileRevision``
# followed the same move for the same reason -- its content (retrieval/
# generation/prompt config) is B4's domain, scoped by workspace the same way
# `documents.models.rag.UserLLMPreference` already is. See
# dev-docs/block-ownership-violations-plan.md item 3. A fresh install creates
# all three under `documents_*` directly, so an existing database needs its
# tables, sequences and content-type rows renamed to match.
#
# A row with note="Server defaults" may exist from before this release --
# `_active_for_workspace` used to materialize one on first read instead of
# returning `None`. Renaming carries it over as an ordinary active row; it is
# not specially handled because it is indistinguishable from (and no less
# valid than) a real active revision.
MOVED_TABLES = {
    "workspaces_workspaceevaluationdataset": "documents_workspaceevaluationdataset",
    "workspaces_workspacequalityevaluationrun": "documents_workspacequalityevaluationrun",
    "workspaces_workspacequalityprofilerevision": "documents_workspacequalityprofilerevision",
}
MOVED_MODELS = {
    "workspaceevaluationdataset": ("workspaces", "documents"),
    "workspacequalityevaluationrun": ("workspaces", "documents"),
    "workspacequalityprofilerevision": ("workspaces", "documents"),
}


def _table_renames():
    """Old Documents tables only; relocated models keep their owning source."""
    from django.db.migrations.loader import MigrationLoader

    renames = {}
    # Only models present in the collapsed initial state existed under the old
    # label. Later feature tables are created by their own migrations.
    initial_apps = MigrationLoader(None).project_state([(NEW_LABEL, NEW_MIGRATION)]).apps
    for model in initial_apps.get_app_config(NEW_LABEL).get_models():
        new_table = model._meta.db_table
        if not new_table.startswith(f"{NEW_LABEL}_"):
            raise CommandError(f"Unexpected table name outside the app prefix: {new_table}")
        if new_table in MOVED_TABLES.values():
            continue  # These tables belonged to workspaces, not document_ai.
        old_table = f"{OLD_LABEL}_{new_table[len(NEW_LABEL) + 1:]}"
        renames[old_table] = new_table
    return renames


class Command(BaseCommand):
    help = "Move an existing database onto the collapsed migration history (safe to re-run)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would change without executing it.",
        )

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            self.stdout.write(
                f"Nothing to do: this upgrade only applies to PostgreSQL, not {connection.vendor}."
            )
            return

        if "django_migrations" not in connection.introspection.table_names():
            self.stdout.write("Nothing to do: this database has no migration history yet.")
            return

        self.dry_run = options["dry_run"]
        self.tables = set(connection.introspection.table_names())

        with transaction.atomic():
            self._relabel_documents()
            for app_label, (last_migration, marker_table) in sorted(COLLAPSED_APPS.items()):
                self._collapse(app_label, last_migration, marker_table)
            self._move_relocated_tables()
            self._drop_removed_tables()

    # -- documents (label change: tables, sequences and indexes move too) ----

    def _relabel_documents(self):
        applied = self._applied(OLD_LABEL)
        if self._applied(NEW_LABEL):
            self.stdout.write(f"{NEW_LABEL}: already upgraded.")
            return
        if not applied:
            self.stdout.write(f"{NEW_LABEL}: no {OLD_LABEL} history to move.")
            return
        if LAST_OLD_MIGRATION not in applied:
            raise CommandError(
                f"This database stops at {OLD_LABEL}.{sorted(applied)[-1]}. Upgrade with the "
                f"previous Dotori version until {LAST_OLD_MIGRATION} is applied, then run this."
            )

        renames = _table_renames()
        self._validate_table_renames(renames)
        self._validate_table_renames({old: new for old, new in MOVED_TABLES.items() if old in self.tables})
        statements = []
        for old_table, new_table in sorted(renames.items()):
            statements.append(f'ALTER TABLE "{old_table}" RENAME TO "{new_table}"')
            statements.append(
                f'ALTER SEQUENCE IF EXISTS "{old_table}_id_seq" RENAME TO "{new_table}_id_seq"'
            )
        for old_index, new_index in sorted(INDEX_RENAMES.items()):
            statements.append(f'ALTER INDEX "{old_index}" RENAME TO "{new_index}"')

        if self.dry_run:
            for statement in statements:
                self.stdout.write(statement)
            self.stdout.write(
                f"UPDATE django_content_type SET app_label = '{NEW_LABEL}' "
                f"WHERE app_label = '{OLD_LABEL}'"
            )
            self._write_history(OLD_LABEL, NEW_LABEL)
            return

        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
            # Permissions and admin log entries reference content types by id,
            # so relabelling the existing rows keeps them attached.
            cursor.execute(
                "UPDATE django_content_type SET app_label = %s WHERE app_label = %s",
                [NEW_LABEL, OLD_LABEL],
            )
        self._write_history(OLD_LABEL, NEW_LABEL)
        self.stdout.write(
            self.style.SUCCESS(
                f"{NEW_LABEL}: renamed {len(_table_renames())} tables and "
                f"{len(INDEX_RENAMES)} indexes, and collapsed the history."
            )
        )

    # -- apps that only need their recorded history rewritten ---------------

    def _collapse(self, app_label, last_migration, marker_table):
        applied = self._applied(app_label)
        if not applied:
            self.stdout.write(f"{app_label}: no history yet, migrate will create it.")
            return
        if applied == {NEW_MIGRATION}:
            if marker_table in self.tables:
                self.stdout.write(f"{app_label}: already collapsed.")
                return
            raise CommandError(
                f"{app_label} records only {NEW_MIGRATION} but {marker_table} is missing, so this "
                f"database predates the rest of its history. Upgrade with the previous Dotori "
                f"version until {last_migration} is applied, then run this."
            )
        if last_migration not in applied:
            raise CommandError(
                f"This database stops at {app_label}.{sorted(applied)[-1]}. Upgrade with the "
                f"previous Dotori version until {last_migration} is applied, then run this."
            )

        self._write_history(app_label, app_label)
        if not self.dry_run:
            self.stdout.write(
                self.style.SUCCESS(f"{app_label}: collapsed {len(applied)} migrations into one.")
            )

    # -- tables whose owning app changed -------------------------------------

    def _move_relocated_tables(self):
        present = {old: new for old, new in MOVED_TABLES.items() if old in self.tables}
        if not present:
            return
        self._validate_table_renames(present)
        statements = []
        for old_table, new_table in sorted(present.items()):
            statements.append(f'ALTER TABLE "{old_table}" RENAME TO "{new_table}"')
            statements.append(
                f'ALTER SEQUENCE IF EXISTS "{old_table}_id_seq" RENAME TO "{new_table}_id_seq"'
            )
        model_updates = [
            (model, old_app, new_app)
            for model, (old_app, new_app) in sorted(MOVED_MODELS.items())
        ]

        if self.dry_run:
            for statement in statements:
                self.stdout.write(statement)
            for model, old_app, new_app in model_updates:
                self.stdout.write(
                    f"UPDATE django_content_type SET app_label = '{new_app}' "
                    f"WHERE app_label = '{old_app}' AND model = '{model}'"
                )
            return

        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
            for model, old_app, new_app in model_updates:
                cursor.execute(
                    "UPDATE django_content_type SET app_label = %s "
                    "WHERE app_label = %s AND model = %s",
                    [new_app, old_app, model],
                )
        self.stdout.write(
            self.style.SUCCESS(f"moved {len(present)} table(s) to their new owning app.")
        )

    # -- tables the collapsed initials no longer create ---------------------

    def _drop_removed_tables(self):
        present = [table for table in REMOVED_TABLES if table in self.tables]
        if not present:
            return
        for table in present:
            statement = f'DROP TABLE IF EXISTS "{table}"'
            if self.dry_run:
                self.stdout.write(statement)
                continue
            with connection.cursor() as cursor:
                cursor.execute(statement)
        if not self.dry_run:
            self.stdout.write(
                self.style.SUCCESS(f"dropped {len(present)} table(s) no longer in the schema.")
            )

    # -- helpers ------------------------------------------------------------

    def _validate_table_renames(self, renames):
        """Dry-run and execution must reject missing sources and data collisions."""
        for old, new in renames.items():
            if old not in self.tables:
                raise CommandError(f"Expected upgrade source table is missing: {old}")
            if new in self.tables:
                raise CommandError(f"Upgrade target already exists; refusing to overwrite: {new}")


    def _applied(self, app_label):
        with connection.cursor() as cursor:
            cursor.execute("SELECT name FROM django_migrations WHERE app = %s", [app_label])
            return {row[0] for row in cursor.fetchall()}

    def _write_history(self, old_app, new_app):
        """Replace `old_app`'s recorded migrations with a single 0001_initial."""
        if self.dry_run:
            self.stdout.write(
                f"DELETE FROM django_migrations WHERE app = '{old_app}'; "
                f"INSERT INTO django_migrations (app, name, applied) "
                f"VALUES ('{new_app}', '{NEW_MIGRATION}', NOW())"
            )
            return
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM django_migrations WHERE app = %s", [old_app])
            cursor.execute(
                "INSERT INTO django_migrations (app, name, applied) VALUES (%s, %s, NOW())",
                [new_app, NEW_MIGRATION],
            )
