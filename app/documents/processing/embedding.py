"""Compatibility imports for the embedding executor domain.

New code should import from :mod:`documents.embedding.executor`. This module
remains temporarily so existing extensions do not break during the service-name
migration.
"""

from documents.embedding.executor import *  # noqa: F401,F403
from documents.embedding.executor import _group_chunks_into_batches
