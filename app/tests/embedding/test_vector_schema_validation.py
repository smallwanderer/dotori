import pytest

from documents.embedding.stores.pgvector_chunk import DIMENSION_FIELD_MAP
from documents.embedding.validation import get_pgvector_column_dimension
from documents.models import ChunkEmbedding


@pytest.mark.django_db
@pytest.mark.parametrize("dimension", sorted(DIMENSION_FIELD_MAP))
def test_vector_schema_reports_actual_model_dimension(dimension):
    field = ChunkEmbedding._meta.get_field(DIMENSION_FIELD_MAP[dimension])
    assert get_pgvector_column_dimension(ChunkEmbedding._meta.db_table, field.column) == dimension
