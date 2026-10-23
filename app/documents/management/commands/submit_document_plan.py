"""Operator-only initial plan producer/replay; no idle scheduler."""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Submit an authorized initial document plan, or replay its durable publication."

    def add_arguments(self, parser):
        parser.add_argument("--source-uid")
        parser.add_argument("--workspace-uid")
        parser.add_argument("--actor-id", type=int)
        parser.add_argument("--request-id")
        parser.add_argument("--replay-plan-id")
        parser.add_argument("--embedding", action="store_true")
        parser.add_argument("--mode", choices=("missing", "refresh"))
        parser.add_argument("--chunk-ids", nargs="+", type=int)

    def handle(self, *args, **options):
        from documents.services.plan_delivery import submit_document_plan
        from orchestration.submissions import publish_submission
        from workspaces.models import Workspace, WorkspaceMembership
        try:
            if options["replay_plan_id"]:
                if options["embedding"] or any(options[key] is not None for key in (
                        "source_uid", "workspace_uid", "actor_id", "request_id", "mode", "chunk_ids")):
                    raise CommandError("Replay cannot be combined with submission arguments")
                row = publish_submission(options["replay_plan_id"], replay=True)
            else:
                if not all(options[key] for key in ("source_uid", "workspace_uid", "actor_id", "request_id")):
                    raise CommandError("source/workspace/actor/request are required")
                actor = get_user_model().objects.get(pk=options["actor_id"])
                workspace = Workspace.objects.get(uid=options["workspace_uid"])
                membership = WorkspaceMembership.objects.filter(workspace=workspace, user=actor).first()
                request = dict(actor=actor, workspace=workspace, membership=membership,
                    source_uid=options["source_uid"], request_id=options["request_id"])
                if options["embedding"]:
                    from documents.orchestration import submit_embedding_plan
                    selector = ({"type": "chunk_ids", "ids": options["chunk_ids"]}
                                if options["chunk_ids"] is not None else {"type": "all"})
                    row = submit_embedding_plan(**request, mode=options["mode"] or "missing", selector=selector)
                else:
                    if options["mode"] is not None or options["chunk_ids"] is not None:
                        raise CommandError("Embedding options require --embedding")
                    row = submit_document_plan(**request)
                row.refresh_from_db()
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        if row.publish_error:
            raise CommandError(f"Plan {row.plan_id} persisted; publication failed: {row.publish_error}")
        self.stdout.write(f"plan_id={row.plan_id} job_id={row.job_id} published={row.published_at is not None}")
