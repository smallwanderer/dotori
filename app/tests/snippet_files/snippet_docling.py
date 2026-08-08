from pathlib import Path

from docling.document_converter import DocumentConverter
from docling.chunking import HybridChunker
from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer
from transformers import AutoTokenizer


EMBED_MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
MAX_TOKENS = 30

test_file = Path(__file__).parent.joinpath("test_files").joinpath("docling_contextualize_stress_fixture.docx")

doc = DocumentConverter().convert(source=test_file).document

tokenizer = HuggingFaceTokenizer(
    tokenizer=AutoTokenizer.from_pretrained(EMBED_MODEL_ID),
    max_tokens=MAX_TOKENS,
)

chunker = HybridChunker(
    tokenizer=tokenizer,
    merge_peers=True
)


# HybridChunker.contextualize acts differently than in codex / gemini says so: 
# Relationship between HybridChunker.contextualize and MAX_TOKENS:
#
# 1. HybridChunker tries to keep the final metadata-enriched serialization
#    returned by chunker.contextualize(chunk) within MAX_TOKENS.
#    However, this should be treated as a best-effort behavior rather than
#    an unconditional guarantee; validating token count before embedding is safer.
#
# 2. During chunker.chunk(), token length is calculated on the contextualized
#    representation, not only on chunk.text.
#    The chunker computes:
#
#        text_len = tokens(chunk.text)
#        total_len = tokens(chunker.contextualize(chunk))
#        other_len = total_len - text_len
#        available_length = max_tokens - other_len
#
#    Therefore, the body text budget is reduced by the token space occupied
#    by metadata such as headings and captions.
#ㅇ
# 3. If headings/captions metadata consumes the available token budget
#    (available_length <= 0), HybridChunker emits a warning, removes
#    headings and captions from the chunk metadata, and retries splitting
#    with body text only.
#    For repeated table headers, overflow behavior can also be controlled
#    with omit_header_on_overflow.
#
# 4. Calling chunker.contextualize(chunk) after chunker.chunk() does not
#    perform additional splitting or token-limit enforcement.
#    It serializes the already produced chunk into an embedding-oriented,
#    metadata-enriched string.

for i, chunk in enumerate(chunker.chunk(dl_doc=doc)):
    raw_text = chunk.text
    contextualized_text = chunker.contextualize(chunk=chunk)

    headings = chunk.meta.headings if hasattr(chunk.meta, "headings") and chunk.meta.headings else []
    heading_path = " > ".join(headings)
    print(f"chunk.meta: {chunk.meta}")
    print(f"heading path: {heading_path}")

    raw_tokens = tokenizer.count_tokens(raw_text)
    contextualized_tokens = tokenizer.count_tokens(contextualized_text)
    token_added = contextualized_tokens - raw_tokens

    if contextualized_tokens > MAX_TOKENS:
        print(f"[chunk {i}] overflow: {contextualized_tokens}")

    if token_added > 0:
        print(f"[chunk {i}]")
        print(f"chunk.text tokens: {raw_tokens}")
        print(f"contextualized tokens: {contextualized_tokens}")
        print(f"token added: {contextualized_tokens - raw_tokens}")

