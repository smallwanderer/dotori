from django.apps import AppConfig

class DocumentAiConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'document_ai'

    def ready(self):
        import document_ai.signals  # noqa