"""`app.urlkeywords.onpage.extract_on_page_terms` — frequency extraction
over the real `seo_audit.policy` tokenizer (task-64 §3.2)."""

from __future__ import annotations

from app.urlkeywords.onpage import extract_on_page_terms


class TestExtractOnPageTerms:
    def test_empty_text_returns_nothing(self):
        assert extract_on_page_terms("", title=None, h1=None, description=None) == []

    def test_repeated_term_is_counted_and_ranked_first(self):
        text = (
            "Hearing loss screening. Our hearing specialists screen hearing "
            "every single visit, because hearing matters."
        )
        terms = extract_on_page_terms(text, title=None, h1=None, description=None)
        assert terms, "expected at least one extracted term"
        assert terms[0].term == "hearing"
        assert terms[0].count == 4
        assert terms[0].is_phrase is False

    def test_repeated_adjacent_pair_surfaces_as_a_phrase(self):
        text = "hearing aid cleaning. hearing aid fitting. hearing aid repair."
        terms = extract_on_page_terms(text, title=None, h1=None, description=None)
        phrases = [t for t in terms if t.is_phrase]
        assert any(p.term == "hearing aid" for p in phrases)

    def test_single_occurrence_pair_is_not_a_phrase(self):
        text = "one two three four five six seven"
        terms = extract_on_page_terms(text, title=None, h1=None, description=None)
        assert not any(t.is_phrase for t in terms)

    def test_in_title_h1_description_flags(self):
        text = "hearing aid cleaning services in NYC"
        terms = extract_on_page_terms(
            text,
            title="Hearing Aid Cleaning | NYC Hearing",
            h1="Cleaning services",
            description=None,
        )
        by_term = {t.term: t for t in terms}
        assert by_term["cleaning"].in_title is True
        assert by_term["cleaning"].in_h1 is True
        assert by_term["cleaning"].in_description is False

    def test_top_n_caps_the_result(self):
        text = " ".join(f"word{i}" for i in range(50))
        terms = extract_on_page_terms(text, title=None, h1=None, description=None, top_n=5)
        assert len([t for t in terms if not t.is_phrase]) <= 5
