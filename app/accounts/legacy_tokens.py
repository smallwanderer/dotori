"""Authentication for the legacy ``APIToken`` (the local-folder sync connector).

Scope-free and account-wide, unlike ``CLIToken`` in ``cli_tokens.py``, so it
gets its own module rather than being folded into one that models scopes and
a required-scope decorator this token type doesn't have.
"""

from django.utils import timezone

from .models import APIToken


class LegacyAPITokenError(Exception):
    def __init__(self, code, message, *, status=401):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def authenticate_legacy_api_token(raw_token):
    try:
        token = APIToken.objects.select_related("user").get(key=raw_token, is_active=True)
    except APIToken.DoesNotExist as exc:
        raise LegacyAPITokenError(
            "INVALID_API_TOKEN",
            "Invalid or inactive API token.",
        ) from exc

    if not token.user.is_active:
        raise LegacyAPITokenError(
            "ACCOUNT_INACTIVE",
            "User account is inactive.",
            status=403,
        )

    APIToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())
    return token
