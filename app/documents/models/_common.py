def _ensure_workspace_from_owner(instance):
    if instance.workspace_id or not instance.owner_id:
        return
    from workspaces.models import WorkspaceMembership

    instance.workspace_id = WorkspaceMembership.objects.filter(
        user_id=instance.owner_id,
        status=WorkspaceMembership.STATUS_ACTIVE,
        workspace__kind="personal",
    ).values_list("workspace_id", flat=True).first()
    if not instance.workspace_id:
        raise ValueError("A workspace is required.")
