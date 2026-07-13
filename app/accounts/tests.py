import pytest
from django.core import mail
from django.template.loader import render_to_string
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.forms import UserRegistrationForm
from accounts.models import User
from accounts.services import send_account_activation_email

pytestmark = pytest.mark.unit


class LegalAgreementTests(TestCase):
    def test_registration_requires_legal_agreements(self):
        form = UserRegistrationForm(
            data={
                "email": "new@example.com",
                "password": "password123",
                "password2": "password123",
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("terms_agreed", form.errors)
        self.assertIn("privacy_agreed", form.errors)

    def test_legal_document_pages_render(self):
        for document in ["terms", "privacy"]:
            response = self.client.get(
                reverse("accounts:legal_document", kwargs={"document": document})
            )

            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "도토리 문서")

    def test_signup_page_exposes_korean_english_translations(self):
        response = self.client.get(reverse("accounts:signup"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-i18n="signupTitle"')
        self.assertContains(response, "계정 만들기")
        self.assertContains(response, "Create your account")
        self.assertContains(response, 'data-i18n-placeholder="signupEmailPlaceholder"')

    def test_signup_validation_errors_are_available_for_translation(self):
        response = self.client.post(
            reverse("accounts:signup"),
            data={
                "email": "invalid",
                "password": "password123",
                "password2": "different-password",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "signup-error")
        self.assertContains(response, "올바른 이메일 주소를 입력하세요.")
        self.assertContains(response, "비밀번호가 일치하지 않습니다.")

    def test_public_account_pages_expose_korean_english_translations(self):
        pages_and_keys = {
            "accounts:login": "signInTitle",
            "accounts:resend_verification": "resendTitle",
        }

        for route_name, translation_key in pages_and_keys.items():
            response = self.client.get(reverse(route_name))

            self.assertEqual(response.status_code, 200)
            self.assertContains(response, f'data-i18n="{translation_key}"')
            self.assertContains(response, "DotoriI18n")

    def test_verification_result_pages_expose_translation_keys(self):
        success_response = self.client.get(
            reverse("accounts:verify", kwargs={"uidb64": "invalid", "token": "invalid"})
        )
        self.assertContains(success_response, 'data-i18n="verificationFailTitle"')

        required_html = render_to_string("accounts/verification_required.html")
        success_html = render_to_string("accounts/verify_success.html")
        signup_done_html = render_to_string("accounts/signup_done.html")
        self.assertIn('data-i18n="verificationRequiredTitle"', required_html)
        self.assertIn('data-i18n="verificationSuccessTitle"', success_html)
        self.assertIn('data-i18n="signupDoneTitle"', signup_done_html)

    def test_activation_email_contains_korean_and_english(self):
        user = User.objects.create_user(email="mail@example.com", password="password123")

        send_account_activation_email(self.client.request().wsgi_request, user, "uid", "token")

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("이메일 인증", mail.outbox[0].subject)
        self.assertIn("Verify your email", mail.outbox[0].subject)
        self.assertIn("도토리 문서 가입", mail.outbox[0].body)
        self.assertIn("Dotori for Document account", mail.outbox[0].body)


@override_settings(DEBUG=False, ALLOWED_HOSTS=["testserver"])
class ErrorPageTests(TestCase):
    def test_unknown_page_uses_shared_error_template(self):
        response = self.client.get("/this-page-does-not-exist/")

        self.assertEqual(response.status_code, 404)
        self.assertTemplateUsed(response, "errors/error.html")
        self.assertContains(response, "페이지를 찾을 수 없음", status_code=404)
