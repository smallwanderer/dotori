from accounts.models import APIToken, CLIToken


def serialize_client_identity(token):
    user = token.user
    if isinstance(token, CLIToken):
        token_type = "cli"
        scopes = list(token.scopes or [])
    elif isinstance(token, APIToken):
        token_type = "sync"
        scopes = ["sync"]
    else:
        raise TypeError("Unsupported access token type.")

    return {
        "ok": True,
        "account": {
            "id": str(user.pk),
            "email": user.email,
            "display_name": user.display_name,
        },
        "token": {
            "type": token_type,
            "scopes": scopes,
        },
        # The selected workspace remains request-specific through
        # X-Dotori-Workspace; identity does not silently choose a team.
        "workspace": None,
    }
