from django.conf import settings


def login_mode(request):
    return {"login_required_mode": settings.LOGIN_REQUIRED}
