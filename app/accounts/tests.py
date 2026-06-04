from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.forms import UserRegistrationForm


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
            self.assertContains(response, "Openshelf")


@override_settings(DEBUG=False, ALLOWED_HOSTS=["testserver"])
class ErrorPageTests(TestCase):
    def test_unknown_page_uses_shared_error_template(self):
        response = self.client.get("/this-page-does-not-exist/")

        self.assertEqual(response.status_code, 404)
        self.assertTemplateUsed(response, "errors/error.html")
        self.assertContains(response, "페이지를 찾을 수 없음", status_code=404)
