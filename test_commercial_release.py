import ast
import os
import re
import unittest
from pathlib import Path

from entitlements import DOCUMENT_TIERS, apply_entitlements_to_result, build_plan_selection_payload
from payment_control import default_mode
from payment_router import resolve_payment_market


ROOT = Path(__file__).resolve().parent


class CommercialReleaseTests(unittest.TestCase):
    def test_launch_price_matrix(self):
        expected = {
            "article": {"GHS": 10.0, "NGN": 1500.0, "USD": 2.99},
            "research_paper": {"GHS": 20.0, "NGN": 2500.0, "USD": 4.99},
            "thesis": {"GHS": 35.0, "NGN": 4000.0, "USD": 7.99},
            "phd": {"GHS": 50.0, "NGN": 6000.0, "USD": 12.99},
        }
        self.assertEqual({key: value["prices"] for key, value in DOCUMENT_TIERS.items()}, expected)

    def test_each_purchase_includes_recheck_within_fourteen_days(self):
        payload = build_plan_selection_payload(10, 20, "GHS", 3000)
        self.assertEqual(payload["analysis_runs_per_purchase"], 2)
        self.assertEqual(payload["validity_days"], 14)

    def test_market_gateway_routing(self):
        self.assertEqual(resolve_payment_market("GH")["provider"], "paystack")
        self.assertEqual(resolve_payment_market("GH")["currency"], "GHS")
        self.assertEqual(resolve_payment_market("NG")["provider"], "paystack")
        self.assertEqual(resolve_payment_market("NG")["currency"], "NGN")
        self.assertEqual(resolve_payment_market("GB")["provider"], "stripe")
        self.assertEqual(resolve_payment_market("GB")["currency"], "USD")

    def test_all_access_modes_are_supported(self):
        original = os.environ.get("GLOBAL_ACCESS_MODE")
        try:
            for mode in ("payment_required", "open_access", "payments_suspended", "maintenance"):
                os.environ["GLOBAL_ACCESS_MODE"] = mode
                self.assertEqual(default_mode(), mode)
        finally:
            if original is None:
                os.environ.pop("GLOBAL_ACCESS_MODE", None)
            else:
                os.environ["GLOBAL_ACCESS_MODE"] = original

    def test_landing_pricing_and_dynamic_visibility(self):
        for name in ("index.html", "new_index.html"):
            source = (ROOT / "templates" / name).read_text(encoding="utf-8")
            self.assertIn('id="pricing"', source)
            self.assertIn("config?.show_pricing !== false", source)
            self.assertIn("data-pricing-link", source)
            self.assertIn("GHS 10", source)
            self.assertIn("₦1,500", source)
            self.assertIn("US$2.99", source)

    def test_free_preview_locks_core_indicators_and_caps_other_samples(self):
        result = {
            "summary": {
                "in_text_citations_found": 80,
                "reference_entries_found": 82,
                "missing_in_references": 3,
                "uncited_references": 5,
                "match_rate": 94,
            },
            "online_verification": {
                "rows": [{"status": "verified", "reference": str(i)} for i in range(80)],
                "summary": {"verified": 79, "not_found": 1},
            },
            "missing_in_references": [{"citation": "Locked"}] * 8,
            "uncited_references": [{"reference": "Locked"}] * 12,
            "claim_support": [{"claim": str(i)} for i in range(60)],
            "citation_needed_claims": [{"claim": str(i)} for i in range(20)],
            "reconciliation_intext_to_reference": [{"status": "matched", "citation": str(i)} for i in range(80)] + [{"status": "not_found", "citation": "must stay locked"}],
            "reconciliation_reference_to_intext": [{"times_cited": 1, "reference": str(i)} for i in range(80)] + [{"times_cited": 0, "reference": "must stay locked"}],
            "recovery": {
                "missing_recovery": [{"citation": str(i)} for i in range(40)],
                "verification_recovery": [{"reference": str(i)} for i in range(60)],
            },
            "correction_plan": {
                "items": [
                    {"id": "locked-missing", "category": "missing_reference"},
                    {"id": "locked-uncited", "category": "uncited_reference"},
                    *[{"id": f"claim-{i}", "category": "claim_support"} for i in range(40)],
                ],
                "evidence_resolution_workspace": {"counts": {"pending": 42}},
            },
        }
        preview = apply_entitlements_to_result(result, tier_key="article", paid=False, currency="GHS")
        self.assertEqual(preview["summary"]["in_text_citations_found"], 80)
        self.assertEqual(preview["summary"]["reference_entries_found"], 82)
        self.assertIsNone(preview["summary"]["missing_in_references"])
        self.assertIsNone(preview["summary"]["uncited_references"])
        self.assertIsNone(preview["summary"]["match_rate"])
        self.assertTrue(preview["missing_in_references"]["locked"])
        self.assertTrue(preview["uncited_references"]["locked"])
        self.assertTrue(preview["recovery"]["missing_recovery"]["locked"])
        self.assertLessEqual(len(preview["recovery"]["verification_recovery"]), 10)
        self.assertNotIn("must stay locked", str(preview["reconciliation_intext_to_reference"]))
        self.assertNotIn("must stay locked", str(preview["reconciliation_reference_to_intext"]))
        self.assertNotIn("locked-missing", str(preview["correction_plan"]))
        self.assertNotIn("locked-uncited", str(preview["correction_plan"]))
        self.assertEqual(preview["online_verification"]["summary"]["verified"], 79)
        for key, rows in (
            ("online_verification", preview["online_verification"]["rows"]),
            ("claim_support", preview["claim_support"]),
            ("citation_needed_claims", preview["citation_needed_claims"]),
            ("reconciliation_intext_to_reference", preview["reconciliation_intext_to_reference"]),
            ("reconciliation_reference_to_intext", preview["reconciliation_reference_to_intext"]),
        ):
            self.assertLessEqual(len(rows), 10, key)
            self.assertEqual(preview["preview_coverage"][key]["shown"], len(rows))

    def test_results_ui_locks_core_indicators_and_marks_samples(self):
        source = (ROOT / "templates" / "new_results.html").read_text(encoding="utf-8")
        self.assertIn('lockRow(3, "Missing Citations"', source)
        self.assertIn('lockRow(2, "Uncited References"', source)
        self.assertIn('setCount("countMissing", paid ?', source)
        self.assertIn('previewNoticeRow(data, "online_verification"', source)
        self.assertIn('25% sample capped at 10 rows', source)
        main = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('payload.pop("reference_count", None)', main)

    def test_public_notice_is_on_landing_and_upload_pages(self):
        for name in ("index.html", "new_index.html", "new_analyse.html"):
            source = (ROOT / "templates" / name).read_text(encoding="utf-8")
            self.assertIn("developerNoticeBanner", source)
            self.assertIn("/api/public/config", source)
            self.assertIn("ciNoticeFlash", source)
            self.assertIn("#16a34a", source)
        main = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('/api/developer/notice', main)
        self.assertIn('Publish banner settings only', main)

    def test_paid_apis_have_server_side_access_checks(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        for route in (
            '/api/manual-search', '/api/manual-verify/evidence',
            '/api/corrections/{job_id}/sources/{item_id}', '/apply-autofix',
            '/api/certificate/{job_id}', '/api/report-package/{job_id}',
            '/export-fixed-document/{job_id}', '/export-references',
        ):
            self.assertIn(route, source)
        self.assertGreaterEqual(source.count("_require_commercial_access(request, job_id)"), 15)

    def test_payment_activation_verifies_ledger_values(self):
        paystack = (ROOT / "paystack_payments.py").read_text(encoding="utf-8")
        stripe = (ROOT / "stripe_payments.py").read_text(encoding="utf-8")
        for marker in ("expected_minor", "expected_currency", "expected_email"):
            self.assertIn(marker, paystack)
            self.assertIn(marker, stripe)
        self.assertIn("expected_reference", paystack)
        self.assertIn("verify_paystack_webhook_signature", paystack)
        self.assertIn("stripe.Webhook.construct_event", stripe)

    def test_projectready_central_paystack_routing_is_configured(self):
        paystack = (ROOT / "paystack_payments.py").read_text(encoding="utf-8")
        main = (ROOT / "main.py").read_text(encoding="utf-8")
        env = (ROOT / ".env.example").read_text(encoding="utf-8")
        guide = (ROOT / "RENDER_ENVIRONMENT.md").read_text(encoding="utf-8")
        for marker in (
            'PAYSTACK_REFERENCE_PREFIX", "CIT"',
            '"source_app": PAYSTACK_SOURCE_APP',
            '"product_code": "full_analysis"',
            "handle_central_payment_confirmation",
        ):
            self.assertIn(marker, paystack)
        self.assertIn('@app.get("/payment/callback")', main)
        self.assertIn('@app.post("/api/paystack/payment-confirmation")', main)
        for marker in (
            "PAYSTACK_CALLBACK_URL=https://citeintegrity.org/payment/callback",
            "PAYSTACK_REFERENCE_PREFIX=CIT",
            "PAYSTACK_SOURCE_APP=citeintegrity",
            "PAYSTACK_CONFIRMATION_SECRET=",
        ):
            self.assertIn(marker, env)
        self.assertIn("https://projectreadyai.com/api/paystack/webhook", guide)

    def test_projectready_confirmation_uses_hmac_sha256(self):
        paystack = (ROOT / "paystack_payments.py").read_text(encoding="utf-8")
        self.assertIn("PAYSTACK_CONFIRMATION_SECRET", paystack)
        self.assertIn("hashlib.sha256", paystack)
        self.assertIn("hmac.compare_digest", paystack)
        self.assertIn("verify_central_confirmation_signature", paystack)

    def test_cost_optimised_worker_roles_are_isolated(self):
        worker = (ROOT / "worker.py").read_text(encoding="utf-8")
        core = (ROOT / "core_worker.py").read_text(encoding="utf-8")
        deep = (ROOT / "deep_worker.py").read_text(encoding="utf-8")
        self.assertIn('document_processing,verification,large_document_processing', worker)
        self.assertIn('CORE_WORKER_QUEUES', core)
        self.assertIn('DEEP_WORKER_QUEUES', deep)
        self.assertIn('deep_enrichment', deep)

    def test_source_archive_contains_no_database_url_credentials(self):
        candidate = re.compile(r"postgres(?:ql)?://[^\s'\"]+:[^\s'\"]+@", re.IGNORECASE)
        for path in ROOT.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in {".py", ".md", ".txt", ".example", ".html", ".js", ".sql"}:
                continue
            self.assertIsNone(candidate.search(path.read_text(encoding="utf-8", errors="ignore")), str(path))

    def test_every_python_file_parses(self):
        for path in ROOT.glob("*.py"):
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
