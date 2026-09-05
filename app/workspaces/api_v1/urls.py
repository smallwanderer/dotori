from django.urls import path

from . import views

app_name = "workspaces_api"

urlpatterns = [
    path("", views.workspace_collection, name="collection"),
    path("switch/", views.switch_workspace, name="switch"),
    path("current/", views.current_workspace, name="current"),
    path("current/members/", views.member_list, name="member-list"),
    path("current/members/<int:user_id>/", views.member_detail, name="member-detail"),
    path("current/invite-code/", views.issue_invite_code, name="invite-code-issue"),
    path("invite-code/redeem/", views.redeem_invite_code, name="invite-code-redeem"),
    path("current/invites/", views.create_invite, name="invite-create"),
    path("current/retrieval-profile/", views.retrieval_profile, name="retrieval-profile"),
    path("current/retrieval-profile/draft/", views.retrieval_profile_draft, name="retrieval-profile-draft"),
    path("current/retrieval-profile/draft/discard/", views.retrieval_profile_draft_discard, name="retrieval-profile-draft-discard"),
    path("current/retrieval-profile/apply/", views.retrieval_profile_apply, name="retrieval-profile-apply"),
    path("current/retrieval-profile/evaluate/", views.retrieval_profile_evaluate, name="retrieval-profile-evaluate"),
    path("current/evaluation-datasets/", views.evaluation_datasets, name="evaluation-datasets"),
    path("current/evaluation-runs/<uuid:run_uid>/", views.evaluation_run_detail, name="evaluation-run-detail"),
    path("current/generation-profile/", views.generation_profile, name="generation-profile"),
    path("current/generation-profile/draft/", views.generation_profile_draft, name="generation-profile-draft"),
    path("current/generation-profile/draft/discard/", views.generation_profile_draft_discard, name="generation-profile-draft-discard"),
    path("current/generation-profile/apply/", views.generation_profile_apply, name="generation-profile-apply"),
    path("current/system-prompt/", views.system_prompt_profile, name="system-prompt-profile"),
    path("current/system-prompt/draft/", views.system_prompt_draft, name="system-prompt-draft"),
    path("current/system-prompt/draft/discard/", views.system_prompt_draft_discard, name="system-prompt-draft-discard"),
    path("current/system-prompt/draft/preview/", views.system_prompt_draft_preview, name="system-prompt-draft-preview"),
    path("current/system-prompt/apply/", views.system_prompt_apply, name="system-prompt-apply"),
    path("current/quality-profile/versions/", views.quality_profile_versions, name="quality-profile-versions"),
    path("invites/inbox/", views.invite_inbox, name="invite-inbox"),
    path("invites/<int:invitation_id>/accept/", views.accept_invite, name="invite-accept"),
    path("invites/<int:invitation_id>/decline/", views.decline_invite, name="invite-decline"),
]
