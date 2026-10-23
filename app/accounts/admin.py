from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import APIToken, CLIToken, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ("email", "display_name", "is_active", "is_staff", "email_verified", "created_at")
    list_filter = ("is_active", "is_staff", "email_verified")
    search_fields = ("email", "display_name")
    ordering = ("-created_at",)
    fieldsets = (
        (None, {"fields": ("email", "display_name", "password")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "email_verified")}),
        ("Dates", {"fields": ("created_at", "email_verification_sent_at")}),
    )
    add_fieldsets = (
        (None, {"classes": ("wide",), "fields": ("email", "password1", "password2")}),
    )
    readonly_fields = ("created_at",)


@admin.register(APIToken)
class APITokenAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "key_preview", "is_active", "created_at", "last_used_at")
    list_filter = ("is_active",)
    search_fields = ("name", "user__email")
    readonly_fields = ("key", "created_at", "last_used_at")

    def key_preview(self, obj):
        return f"{obj.key[:8]}...{obj.key[-4:]}"
    key_preview.short_description = "Key"


@admin.register(CLIToken)
class CLITokenAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "prefix", "scope_list", "is_active", "created_at", "last_used_at")
    list_filter = ("is_active",)
    search_fields = ("name", "user__email", "prefix")
    readonly_fields = ("uid", "key_hash", "prefix", "created_at", "last_used_at")

    def scope_list(self, obj):
        return ", ".join(obj.scopes or [])
    scope_list.short_description = "Scopes"
