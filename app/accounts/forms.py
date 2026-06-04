from django import forms
from django.contrib.auth.forms import AuthenticationForm

from .models import User


class UserRegistrationForm(forms.ModelForm):
    terms_agreed = forms.BooleanField(
        required=True,
        widget=forms.CheckboxInput(attrs={"class": "auth-checkbox"}),
        error_messages={"required": "You must agree to the Terms of Use."},
    )
    privacy_agreed = forms.BooleanField(
        required=True,
        widget=forms.CheckboxInput(attrs={"class": "auth-checkbox"}),
        error_messages={"required": "You must agree to the Privacy Policy."},
        help_text="We use your personal information to provide and improve our services.",
    )
    password = forms.CharField(
        widget=forms.PasswordInput(
            attrs={
                "class": "auth-input",
                "placeholder": "Enter a password",
                "data-i18n-placeholder": "signupPasswordPlaceholder",
            }
        )
    )
    password2 = forms.CharField(
        widget=forms.PasswordInput(
            attrs={
                "class": "auth-input",
                "placeholder": "Confirm your password",
                "data-i18n-placeholder": "signupPasswordConfirmPlaceholder",
            }
        )
    )

    class Meta:
        model = User
        fields = ["email"]
        widgets = {
            "email": forms.EmailInput(
                attrs={
                    "class": "auth-input",
                    "placeholder": "Enter your email address",
                    "data-i18n-placeholder": "signupEmailPlaceholder",
                }
            )
        }

    def clean_email(self):
        email = self.cleaned_data.get("email")
        if User.objects.filter(email=email).exists():
            raise forms.ValidationError("This email address is already in use.")
        return email

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        password2 = cleaned_data.get("password2")
        if password != password2:
            raise forms.ValidationError("Passwords do not match.")
        return cleaned_data


class ResendVerificationEmailForm(forms.Form):
    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(
            attrs={
                "class": "auth-input",
                "placeholder": "Enter your email address",
                "data-i18n-placeholder": "accountEmailPlaceholder",
            }
        ),
    )

    def clean_email(self):
        email = self.cleaned_data.get("email")
        if not User.objects.filter(email=email).exists():
            raise forms.ValidationError("No account exists for this email address.")
        return email


class EmailAuthenticationForm(AuthenticationForm):
    username = forms.EmailField(
        label="Email",
        required=True,
        widget=forms.EmailInput(
            attrs={
                "class": "auth-input",
                "placeholder": "Enter your email address",
                "data-i18n-placeholder": "accountEmailPlaceholder",
            }
        ),
    )
    password = forms.CharField(
        widget=forms.PasswordInput(
            attrs={
                "class": "auth-input",
                "placeholder": "Enter your password",
                "data-i18n-placeholder": "accountPasswordPlaceholder",
            }
        )
    )
