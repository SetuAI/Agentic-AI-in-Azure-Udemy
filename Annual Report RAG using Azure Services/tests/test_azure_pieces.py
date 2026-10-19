"""
Offline tests for the Azure-specific code: the OData filter, the index
definition, the search call, the semantic rerank, the PDF-to-markdown
cache, the content-filter handling, and the settings check.
"""

from types import SimpleNamespace               # a quick object with attributes, for fake responses

import httpx                                    # used to build a realistic fake Azure error
import pytest                                   # the test runner

import config                                   # settings
from ingestion import build_index, load_pdfs    # ingestion steps under test
from retrieval import rerank as rerank_module   # semantic rerank under test
from retrieval import search as search_module   # hybrid search under test


# ------------------------------------------------------------------------------
# Search: the OData filter
# ------------------------------------------------------------------------------

def test_filter_for_company_and_year():
    assert search_module.build_filter("Infosys", "FY2025") == "company eq 'Infosys' and fiscal_year eq 'FY2025'"


def test_filter_with_nothing_is_no_filter():
    assert search_module.build_filter() is None


def test_quotes_inside_values_are_escaped():
    assert search_module.build_filter("O'Brien") == "company eq 'O''Brien'"  # OData doubles the quote


# ------------------------------------------------------------------------------
# Search: one call does hybrid + semantic, results come back in hybrid order
# ------------------------------------------------------------------------------

def fake_hit(text, hybrid, semantic):
    """One search hit, shaped like Azure's."""
    return {"text": text, "company": "Infosys", "fiscal_year": "FY2025", "year_label": "FY 2024-25",
            "position": 0, "@search.score": hybrid, "@search.reranker_score": semantic}


def test_search_sends_hybrid_semantic_filtered_query(monkeypatch):
    calls = {}                                  # remember what search() sent to Azure

    class FakeSearch:
        def search(self, **kwargs):
            calls.update(kwargs)                # record the request
            return [fake_hit("b", 0.02, 3.5), fake_hit("a", 0.03, 1.0)]  # Azure returns semantic order

    fake_embeddings = SimpleNamespace(create=lambda **kw: SimpleNamespace(data=[SimpleNamespace(embedding=[0.1])]))
    monkeypatch.setattr(search_module, "search_client", lambda: FakeSearch())
    monkeypatch.setattr(search_module, "openai_client", lambda: SimpleNamespace(embeddings=fake_embeddings))

    results = search_module.search("revenue?", company="Infosys", fiscal_year="FY2025")

    assert calls["search_text"] == "revenue?"                  # keyword half
    assert calls["vector_queries"][0].vector == [0.1]           # vector half
    assert calls["query_type"] == "semantic"                    # semantic ranker on
    assert calls["filter"] == "company eq 'Infosys' and fiscal_year eq 'FY2025'"  # one year only
    assert [r["text"] for r in results] == ["a", "b"]           # put back into HYBRID order
    assert results[1]["reranker_score"] == 3.5                  # semantic score kept


# ------------------------------------------------------------------------------
# Rerank: sort by the semantic score, keep the top few
# ------------------------------------------------------------------------------

def test_rerank_orders_by_semantic_score():
    candidates = [{"text": t, "reranker_score": s} for t, s in [("x", 1.0), ("y", 3.9), ("z", 2.0)]]
    assert [c["text"] for c in rerank_module.rerank(candidates, top_n=2)] == ["y", "z"]


def test_rerank_falls_back_to_hybrid_order_without_semantic_scores():
    candidates = [{"text": t, "reranker_score": None} for t in "abc"]
    assert [c["text"] for c in rerank_module.rerank(candidates, top_n=2)] == ["a", "b"]


# ------------------------------------------------------------------------------
# Index definition: built with the real SDK classes
# ------------------------------------------------------------------------------

def test_index_definition_has_filters_vector_and_semantic_setup():
    pytest.importorskip("azure.search.documents")
    index = build_index.build_index_definition()
    fields = {f.name: f for f in index.fields}
    assert fields["id"].key                                     # unique key
    assert fields["fiscal_year"].filterable                     # the year filter depends on this
    assert fields["company"].filterable
    assert fields["embedding"].vector_search_dimensions == config.EMBEDDING_DIMENSIONS
    assert index.semantic_search.configurations[0].name == config.SEMANTIC_CONFIG_NAME


def test_document_ids_use_only_allowed_characters():
    chunk = {"company": "Infosys", "fiscal_year": "FY2025", "position": 7, "text": "t",
             "year_label": "FY 2024-25", "source_file": "infosys_fy2025.pdf"}
    assert build_index.to_search_document(chunk, [0.0])["id"] == "Infosys_FY2025_7"


# ------------------------------------------------------------------------------
# PyMuPDF4LLM: the cache stops us converting twice
# ------------------------------------------------------------------------------

DOC = {"filename": "infosys_fy2025.pdf", "company": "Infosys", "fiscal_year": "FY2025", "year_label": "FY 2024-25"}


def test_cached_markdown_is_used_without_calling_azure(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PROCESSED_DIR", tmp_path)
    (tmp_path / "Infosys_FY2025.md").write_text("# cached")
    monkeypatch.setattr(load_pdfs, "analyse_pdf", lambda p: pytest.fail("PDF was converted again"))
    result = load_pdfs.load_single_pdf(DOC)
    assert result["text"] == "# cached" and result["loaded_from"] == "cache"


def test_first_load_analyses_and_writes_the_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PROCESSED_DIR", tmp_path)
    monkeypatch.setattr(load_pdfs, "download_pdf", lambda d: tmp_path / "x.pdf")
    monkeypatch.setattr(load_pdfs, "analyse_pdf", lambda p: "# fresh")
    load_pdfs.load_single_pdf(DOC)
    assert (tmp_path / "Infosys_FY2025.md").read_text() == "# fresh"


# ------------------------------------------------------------------------------
# Azure OpenAI content filter: handled, not a crash
# ------------------------------------------------------------------------------

def test_content_filter_block_becomes_a_reviewable_draft(monkeypatch):
    from openai import BadRequestError
    from graph import nodes

    request = httpx.Request("POST", "https://example.openai.azure.com")
    error = BadRequestError("content_filter triggered", response=httpx.Response(400, request=request),
                            body={"code": "content_filter"})

    def blocked(**kwargs):
        raise error

    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=blocked)))
    monkeypatch.setattr(nodes, "openai_client", lambda: fake)
    state = {"company": "Infosys", "question": "q", "reranked_chunks": {}}
    assert "content filter" in nodes.generate_node(state)["draft_answer"]


# ------------------------------------------------------------------------------
# Settings check
# ------------------------------------------------------------------------------

def test_missing_settings_are_named(monkeypatch):
    monkeypatch.setattr(config, "AZURE_SEARCH_API_KEY", None)
    with pytest.raises(RuntimeError, match="AZURE_SEARCH_API_KEY"):
        config.require("AZURE_SEARCH_API_KEY")
