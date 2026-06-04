from django.core.mail import send_mail
from django.urls import reverse
from django.conf import settings


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
