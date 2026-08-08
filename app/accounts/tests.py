import pytest
from django.core import mail
from django.template.loader import render_to_string
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.forms import UserRegistrationForm
from accounts.models import User
from accounts.services import get_or_create_default_local_admin, send_account_activation_email

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

    @override_settings(LOGIN_REQUIRED=True)
    def test_public_account_pages_expose_korean_english_translations(self):
        # Requires real login enforcement: with LOGIN_REQUIRED=False the login
        # page redirects an already-auto-authenticated visitor away.
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


class LoginModeTests(TestCase):
    @override_settings(LOGIN_REQUIRED=False)
    def test_anonymous_request_auto_authenticates_as_default_admin(self):
        response = self.client.get(reverse("files:index"))

        self.assertEqual(response.status_code, 200)
        user = User.objects.get(email="local-admin@dotori.local")
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.is_staff)

    @override_settings(LOGIN_REQUIRED=True)
    def test_switch_view_is_unreachable_when_login_is_required(self):
        response = self.client.get(reverse("accounts:switch"))

        self.assertEqual(response.status_code, 404)

    @override_settings(LOGIN_REQUIRED=True)
    def test_settings_page_still_requires_real_login(self):
        response = self.client.get(reverse("accounts:settings"))

        self.assertRedirects(
            response,
            f"{reverse('accounts:login')}?next={reverse('accounts:settings')}",
        )

    @override_settings(LOGIN_REQUIRED=False)
    def test_switch_view_creates_and_activates_new_profile(self):
        self.client.get(reverse("files:index"))  # establishes the default profile via middleware

        response = self.client.post(
            reverse("accounts:switch"),
            data={"action": "create", "display_name": "둘째"},
        )

        self.assertRedirects(response, reverse("files:index"))
        new_user = User.objects.get(display_name="둘째")
        self.assertTrue(new_user.is_superuser)
        self.assertFalse(new_user.has_usable_password())
        self.assertEqual(self.client.session["active_profile_id"], new_user.id)

    @override_settings(LOGIN_REQUIRED=False)
    def test_switch_view_create_requires_display_name(self):
        response = self.client.post(
            reverse("accounts:switch"),
            data={"action": "create", "display_name": "  "},
        )

        self.assertRedirects(response, reverse("accounts:switch"))

    @override_settings(LOGIN_REQUIRED=False)
    def test_switch_view_switches_back_to_existing_profile(self):
        get_or_create_default_local_admin()
        other = User.objects.create_user(email="other@example.com", password="password123")
        other.is_active = True
        other.email_verified = True
        other.save()

        response = self.client.post(
            reverse("accounts:switch"),
            data={"action": "switch", "user_id": other.id},
        )

        self.assertRedirects(response, reverse("files:index"))
        self.assertEqual(self.client.session["active_profile_id"], other.id)

    @override_settings(LOGIN_REQUIRED=False)
    def test_password_less_profile_can_set_password_without_current_password(self):
        admin = get_or_create_default_local_admin()
        self.client.get(reverse("files:index"))  # establishes the default profile via middleware

        response = self.client.post(
            reverse("accounts:settings"),
            data={
                "action": "change_password",
                "new_password": "newpassword123",
                "new_password2": "newpassword123",
            },
        )

        # Not assertRedirects: following the redirect renders accounts:settings,
        # whose GET path separately needs a persisted LLM runtime config that's
        # unrelated to what this test checks (the POST action itself).
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("accounts:settings"))
        admin.refresh_from_db()
        self.assertTrue(admin.check_password("newpassword123"))

    @override_settings(LOGIN_REQUIRED=False)
    def test_password_less_profile_can_change_email(self):
        admin = get_or_create_default_local_admin()
        self.client.get(reverse("files:index"))

        response = self.client.post(
            reverse("accounts:settings"),
            data={"action": "change_email", "email": "real-admin@example.com"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("accounts:settings"))
        admin.refresh_from_db()
        self.assertEqual(admin.email, "real-admin@example.com")


@override_settings(DEBUG=False, ALLOWED_HOSTS=["testserver"])
class ErrorPageTests(TestCase):
    def test_unknown_page_uses_shared_error_template(self):
        response = self.client.get("/this-page-does-not-exist/")

        self.assertEqual(response.status_code, 404)
        self.assertTemplateUsed(response, "errors/error.html")
        self.assertContains(response, "페이지를 찾을 수 없음", status_code=404)
