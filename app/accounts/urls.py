from django.urls import path
from . import views

app_name = "accounts"

urlpatterns = [
    path("signup/", views.signup_view, name="signup"),
    path("legal/<str:document>/", views.legal_document_view, name="legal_document"),
    path("verify/<uidb64>/<token>/", views.verify_email, name="verify"),
    path("resend-verification/", views.resend_verification_email, name="resend_verification"),
    path("verification-required/", views.verification_required_view, name="verification_required"),
    path("login/", views.SigninView.as_view(), name="login"),
    path("logout/", views.SignoutView.as_view(), name="logout"),
    path("settings/", views.settings_view, name="settings"),
]
