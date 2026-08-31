import ast
import os
import re
import unittest
from pathlib import Path

from entitlements import DOCUMENT_TIERS, build_plan_selection_payload
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

    def test_public_notice_is_on_landing_and_upload_pages(self):
        for name in ("index.html", "new_index.html", "new_analyse.html"):
            source = (ROOT / "templates" / name).read_text(encoding="utf-8")
            self.assertIn("developerNoticeBanner", source)
            self.assertIn("/api/public/config", source)
            self.assertIn("ciNoticeFlash", source)
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
        self.assertIn("verify_paystack_webhook_signature", paystack)
        self.assertIn("stripe.Webhook.construct_event", stripe)

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
