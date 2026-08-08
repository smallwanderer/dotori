from django.conf import settings
from django.contrib.auth import get_user_model, login

from .services import get_or_create_default_local_admin


class LocalProfileMiddleware:
    """When LOGIN_REQUIRED is off, auto-authenticate anonymous requests as a local
    profile (no password) instead of asking the user to sign in. Must run after
    AuthenticationMiddleware, which resolves request.user first."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not settings.LOGIN_REQUIRED and not request.user.is_authenticated:
            user = self._resolve_active_profile(request)
            user.backend = "django.contrib.auth.backends.ModelBackend"
            login(request, user)
            request.session["active_profile_id"] = user.id

        return self.get_response(request)

    def _resolve_active_profile(self, request):
        User = get_user_model()
        profile_id = request.session.get("active_profile_id")
        if profile_id:
            try:
                return User.objects.get(pk=profile_id, is_active=True)
            except User.DoesNotExist:
                pass
        return get_or_create_default_local_admin()
