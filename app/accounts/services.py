import secrets

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.urls import reverse
from django.conf import settings


DEFAULT_LOCAL_ADMIN_EMAIL = "local-admin@dotori.local"


def get_or_create_default_local_admin():
    """Return the auto-provisioned profile used when LOGIN_REQUIRED is off, creating it on first use."""
    User = get_user_model()
    user, created = User.objects.get_or_create(
        email=DEFAULT_LOCAL_ADMIN_EMAIL,
        defaults={
            "display_name": "로컬 관리자",
            "is_active": True,
            "is_staff": True,
            "is_superuser": True,
            "email_verified": True,
        },
    )
    if created:
        user.set_unusable_password()
        user.save(update_fields=["password"])
    return user


def _generate_local_profile_email():
    """A synthetic, never-shown identifier. Offline/no-login profiles have no
    real email to verify or log in with, so nothing user-facing depends on it
    being a real address — it only satisfies the unique USERNAME_FIELD."""
    User = get_user_model()
    for _ in range(5):
        candidate = f"local-{secrets.token_hex(6)}@dotori.local"
        if not User.objects.filter(email__iexact=candidate).exists():
            return candidate
    raise ValidationError("계정을 생성하지 못했습니다. 다시 시도해주세요.")


def create_local_profile(display_name):
    """Create a new password-less local profile (admin rights by default, since this
    is only reachable while LOGIN_REQUIRED is off) and return it. Only asks for a
    display name — no email, since this flow assumes offline/personal use."""
    display_name = (display_name or "").strip()[:50]
    if not display_name:
        raise ValidationError("별칭을 입력해주세요.")

    User = get_user_model()
    user = User(
        email=_generate_local_profile_email(),
        display_name=display_name,
        is_active=True,
        is_staff=True,
        is_superuser=True,
        email_verified=True,
    )
    user.set_unusable_password()
    user.save()
    return user


def update_account_email(user, new_email):
    """Change a user's login email, validating format and uniqueness. Used to
    turn a password-less local profile into one that can sign in for real,
    once LOGIN_REQUIRED is switched on."""
    User = get_user_model()
    new_email = User.objects.normalize_email((new_email or "").strip())
    if not new_email:
        raise ValidationError("이메일을 입력해주세요.")
    validate_email(new_email)
    if User.objects.filter(email__iexact=new_email).exclude(pk=user.pk).exists():
        raise ValidationError("이미 사용 중인 이메일입니다.")
    user.email = new_email
    user.save(update_fields=["email"])
    return user


def send_account_activation_email(request, user, uid, token):
    activation_url = reverse("accounts:verify", kwargs={"uidb64": uid, "token": token})
    subject = "[도토리 문서] 이메일 인증 / Verify your email"
    message = f"""
도토리 문서 가입을 완료하려면 아래 링크에서 이메일 주소를 인증해 주세요.

{activation_url}

To finish creating your Dotori for Document account, verify your email address using the link above.
""" 

    send_mail(
        subject,
        message,
        settings.DEFAULT_FROM_EMAIL,
        [user.email],
        fail_silently=False,
    )
