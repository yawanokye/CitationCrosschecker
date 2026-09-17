import ast
import re
import unittest
from pathlib import Path

from correction_plan import _reference_audit
from evidence_resolution import build_evidence_resolution_workspace
import publication_integrity


ROOT = Path(__file__).resolve().parent


def load_completeness_gate():
    """Load the pure completeness helper without importing worker services."""
    source = (ROOT / "worker.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_reference_is_seriously_incomplete"
    )
    namespace = {
        "re": re,
        "_worker_style_family": lambda style: str(style or "apa").lower(),
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), "worker.py", "exec"), namespace)
    return namespace["_reference_is_seriously_incomplete"]


class VerificationOperationalReleaseTests(unittest.TestCase):
    def test_upload_enables_verification_and_backend_receives_option(self):
        upload = (ROOT / "templates" / "new_analyse.html").read_text(encoding="utf-8")
        main = (ROOT / "main.py").read_text(encoding="utf-8")
        worker = (ROOT / "worker.py").read_text(encoding="utf-8")
        self.assertIn('id="onlineVerify" checked', upload)
        self.assertIn("academic_voice_enabled,\n            online_verify_enabled,", main)
        self.assertIn("enable_online_verification=True", worker)
        self.assertIn('Queue("verification", connection=redis_conn)', worker)
        self.assertIn('"worker.process_verification"', worker)
        self.assertIn("Automatic verification queued", worker)

    def test_core_worker_prioritises_verification_without_extra_worker(self):
        source = (ROOT / "core_worker.py").read_text(encoding="utf-8")
        self.assertIn('"verification,document_processing,large_document_processing"', source)

    def test_fast_verification_defaults_are_bounded_and_cached(self):
        worker = (ROOT / "worker.py").read_text(encoding="utf-8")
        verifier = (ROOT / "verify.py").read_text(encoding="utf-8")
        env = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn('VERIFY_CHUNK_SIZE", "40"', worker)
        self.assertIn('VERIFY_REDIS_CACHE_ENABLED", "1"', worker)
        self.assertIn('VERIFY_REDIS_CACHE_TTL", "21600"', worker)
        self.assertIn('VERIFY_CACHE_NAMESPACE", "v4-fast-integrity"', worker)
        self.assertIn('VERIFY_SHORT_OPENALEX_MAX_QUERIES", "1"', verifier)
        for setting in (
            "VERIFY_PARALLEL_MODE=1",
            "VERIFY_PARALLEL_WORKERS=8",
            "VERIFY_CHUNK_SIZE=40",
            "VERIFY_USE_CACHE=0",
            "VERIFY_REDIS_CACHE_ENABLED=1",
            "VERIFY_REDIS_CACHE_TTL=21600",
            "VERIFY_CACHE_NAMESPACE=v4-fast-integrity",
        ):
            self.assertIn(setting, env)

    def test_bundled_retraction_watch_index_is_found_without_network_fallback(self):
        path = publication_integrity.RETRACTION_WATCH_DB_PATH
        self.assertTrue(path.is_file())
        self.assertEqual(path.parent.name, "data")
        status = publication_integrity.fetch_crossref_publication_status(
            "10.1016/j.nepr.2025.104262"
        )
        self.assertTrue(status["checked"])
        self.assertEqual(status["publication_status"], "retracted")
        self.assertEqual(status["data_version"], "2026-09-15")

    def test_complete_numeric_references_are_not_marked_incomplete(self):
        gate = load_completeness_gate()
        references = [
            "1. Özden D, Yılmaz İ, Sönmez S. Effect of moulage on nursing students' endotracheal suctioning knowledge and skills. Nurse Educ Pract. 2025;82:104262.",
            "3. Abate TW, Enyew A, Gebrie F, Bayuh H. Nurses' knowledge and attitude towards diabetes foot care in Bahir Dar, North West Ethiopia. Heliyon. 2020;6(11):e05552.",
        ]
        self.assertTrue(all(not gate(ref, "numeric_square") for ref in references))
        self.assertTrue(gate("1. Smith J. 2020.", "numeric_square"))

    def test_exact_doi_prevents_false_metadata_conflict(self):
        refs = [
            "6. Colson P, Fournier PE, Delerce J, Million M, Bedotto M, Houhamdi L. Culture and identification of a 'Deltamicron' SARS-CoV-2 in a three cases cluster in southern France. J Med Virol. 2022;94(8):3739-3749. doi:10.1002/jmv.27789",
            "8. Tan J, Zhu R, Li Y, Wang L, Liao S, Cheng L, Mao LX, Jing D. Vitamin K2 in managing nocturnal leg cramps: a randomized clinical trial. JAMA Intern Med. 2024;185(1):31-40. doi:10.1001/jamainternmed.2024.5726",
            "11. Açikgöz S, Arslan M, Göl I. The relationship between artificial intelligence literacy and artificial intelligence anxiety among nurses: A correlational descriptive study. Collegian. 2026;33(2):106-14. doi:10.1016/j.colegn.2026.01.004",
        ]
        rows = []
        for ref in refs:
            doi = re.search(r"10\.\d{4,9}/\S+", ref).group(0)
            rows.append({
                "reference": ref,
                "status": "verified",
                "matched_doi": doi,
                "matched_title": "Different punctuation or provider title",
                "matched_year": "2026",
                "matched_authors": "Provider metadata",
            })
        audit = _reference_audit({
            "selected_style": "numeric_square",
            "online_verification": {"rows": rows},
        })
        self.assertEqual(len(audit), 3)
        self.assertTrue(all(row["identity"]["accepted"] for row in audit))
        self.assertTrue(all(row["identity"]["doi_exact"] for row in audit))

    def test_workspace_count_includes_visible_formatting_corrections(self):
        workspace = build_evidence_resolution_workspace([{
            "id": "reference-metadata-1",
            "category": "reference_metadata",
            "decision": "pending",
            "available_actions": ["accept", "reject", "ignore"],
        }])
        self.assertEqual(workspace["counts"]["pending"], 1)
        self.assertEqual(workspace["groups"][0]["label"], "Formatting and other corrections")

    def test_location_is_human_readable_not_raw_json(self):
        html = (ROOT / "templates" / "new_results.html").read_text(encoding="utf-8")
        self.assertIn("function correctionLocationText(location)", html)
        self.assertNotIn("Location: ${esc(JSON.stringify(item.location))}", html)

    def test_served_results_template_exposes_publication_safety_findings(self):
        served = (ROOT / "templates" / "new_results.html").read_text(encoding="utf-8")
        mirror = (ROOT / "new_results.html").read_text(encoding="utf-8")
        self.assertEqual(served, mirror)
        for marker in (
            'id="globalPublicationSafety"',
            "function renderGlobalPublicationSafety(data)",
            "Publication integrity alert",
            'id="publicationSafetyBox"',
            "Publication Status</th>",
            "data.source_risk_review || {}",
            "These safety alerts are shown regardless of payment",
            "row.publication_status || \"unchecked\"",
            "row.publication_events || []",
            'score_available === false ? "Withheld"',
        ):
            self.assertIn(marker, served)


if __name__ == "__main__":
    unittest.main()
