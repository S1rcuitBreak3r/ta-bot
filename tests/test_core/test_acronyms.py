"""
Tests for acronym expansion and TOSP keyword lookup.
Pure functions — no DB or network needed.
"""
import os
import sys
import unittest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ADMIN_TELEGRAM_ID", "1234567")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from core.acronyms import expand_query, tosp_keywords


class TestExpandQueryTosp(unittest.TestCase):
    """TOSP mode replaces acronyms with their expansion."""

    def test_lscs_replaced(self):
        self.assertEqual(expand_query("LSCS", tosp=True), "lower segment caesarean section")

    def test_circ_replaced(self):
        self.assertEqual(expand_query("circ", tosp=True), "circumcision")

    def test_single_letter_g(self):
        self.assertEqual(expand_query("G", tosp=True), "gastroscopy")

    def test_single_letter_c(self):
        self.assertEqual(expand_query("C", tosp=True), "colonoscopy")

    def test_gc_combined(self):
        result = expand_query("GC", tosp=True)
        self.assertIn("gastroscopy", result)
        self.assertIn("colonoscopy", result)

    def test_t_and_a_ampersand(self):
        result = expand_query("T&A", tosp=True)
        self.assertIn("tonsillectomy", result)
        self.assertIn("adenoidectomy", result)

    def test_t_and_a_spelled_out(self):
        result = expand_query("T and A", tosp=True)
        self.assertIn("tonsillectomy", result)
        self.assertIn("adenoidectomy", result)

    def test_acronym_within_sentence(self):
        result = expand_query("fee for LSCS please", tosp=True)
        self.assertIn("lower segment caesarean section", result)
        self.assertNotIn("LSCS", result)

    def test_no_partial_word_match(self):
        # "gcs" must not trigger the "gc" or "g"/"c" expansions
        result = expand_query("gcs score", tosp=True)
        self.assertEqual(result, "gcs score")


class TestExpandQueryGeneral(unittest.TestCase):
    """General mode appends, preserving original wording."""

    def test_lscs_appended(self):
        result = expand_query("what is LSCS")
        self.assertIn("LSCS", result)
        self.assertIn("lower segment caesarean section", result)

    def test_single_letters_not_expanded(self):
        # "a" is a normal English word outside TOSP context
        result = expand_query("what is a spinal")
        self.assertEqual(result, "what is a spinal")

    def test_unknown_text_unchanged(self):
        self.assertEqual(expand_query("what is jiak"), "what is jiak")


class TestTospKeywords(unittest.TestCase):
    def test_circ_keyword(self):
        self.assertIn(("Circumcision",), tosp_keywords("circ"))

    def test_gc_requires_both_terms(self):
        self.assertIn(("Upper GI Endoscopy", "Colonoscopy"), tosp_keywords("GC"))

    def test_no_keywords_for_plain_text(self):
        self.assertEqual(tosp_keywords("colonoscopy fee"), [])

    def test_case_insensitive(self):
        self.assertEqual(tosp_keywords("CIRC"), tosp_keywords("circ"))


if __name__ == "__main__":
    unittest.main()
