import unittest
from pathlib import Path

import engine
from acii import compute_acii
from entitlements import apply_entitlements_to_result
from publication_integrity import (
    events_from_crossref_message,
    extract_doi,
    fetch_crossref_publication_status,
    is_publication_notice,
    summarise_publication_events,
)
from source_risk import assess_source_risks


ROOT = Path(__file__).resolve().parent


BENCHMARK_REFERENCES = """1. Özden D, Yılmaz İ, Sönmez S. Effect of moulage on nursing students' endotracheal suctioning knowledge and skills. Nurse Educ Pract. 2025;82:104262. doi:10.1016/j.nepr.2025.104262
2. Zhao L, Yang J, Liu W. Application and effect evaluation of nursing quality target management in free flap transplantation. PLoS One. 2021;16(1):e0245097. doi:10.1371/journal.pone.0245097
3. Abate TW, Enyew A, Gebrie F, Bayuh H. Nurses' knowledge and attitude towards diabetes foot care in Bahir Dar, North West Ethiopia. Heliyon. 2020;6(11):e05552. doi:10.1016/j.heliyon.2020.e05552
4. Wu JL, Zhang Q, Zhang LH, Li JY. Effect of comprehensive nursing intervention on wound pain and wound complications. Int Wound J. 2023;21(1):e14619. doi:10.1111/iwj.14619
5. de Vries DH, Buiting HM. Friendship during patients' stable and unstable phases of incurable cancer: a qualitative interview study. BMJ Open. 2022;12(5):e058801. doi:10.1136/bmjopen-2021-058801
6. Colson P, Fournier PE, Delerce J, Million M, Bedotto M, Houhamdi L. Culture and identification of a 'Deltamicron' SARS-CoV-2 in a three cases cluster in southern France. J Med Virol. 2022;94(8):3739-3749. doi:10.1002/jmv.27789
7. Duan Y, Zhang S, Wang L, Zhou X, He Q, Liu S, Yue K, Wan X. Targeted silencing of CXCR4 inhibits epithelial-mesenchymal transition in oral squamous cell carcinoma. Oncol Lett. 2016;12(3):2055-2061. doi:10.3892/ol.2016.4838
8. Tan J, Zhu R, Li Y, Wang L, Liao S, Cheng L, Mao LX, Jing D. Vitamin K2 in managing nocturnal leg cramps: a randomized clinical trial. JAMA Intern Med. 2024;185(1):31-40. doi:10.1001/jamainternmed.2024.5726
9. PLoS One Editors. Retraction: Downregulation of FBP1 promotes tumor metastasis and indicates poor prognosis in gastric cancer. PLoS One. 2026;21(1):e0340232. doi:10.1371/journal.pone.0340232
10. Abu-Mahfouz MS, AlFehaid S, Burqan HM, El Arab RA. Artificial intelligence in mental health care: a scoping review of reviews. Front Psychiatry. 2026;17:1688043. doi:10.3389/fpsyt.2026.1688043
11. Açikgöz S, Arslan M, Göl I. The relationship between artificial intelligence literacy and artificial intelligence anxiety among nurses: A correlational descriptive study. Collegian. 2026;33(2):106-14. doi:10.1016/j.colegn.2026.01.004
12. Alexander KE, Mathews N, Sutherland S, Joseph J, Holmes K, Sims M. Using artificial intelligence to create case studies addressing social determinants in graduate nursing education. Electro J Gen Med. 2026;23(1). doi:10.29333/ejgm/17634
13. Alptekin HM, Ulubay S. The effect of an artificial intelligence-assisted motivational procedure on kinesiophobia and mobility after total knee arthroplasty: a randomized controlled trial. BMC Musculoskelet Disord. 2026;27(1). doi:10.1186/s12891-026-09849-z
14. Alshanqeeti S, de Guzman A, Riley MK, Coffey KC, Goodman KE, Harris AD, et al. Generative artificial intelligence for surgical site infection surveillance. Infect Control Hosp Epidemiol. 2026;1-4. doi:10.1017/ice.2026.10429
15. Amin SU, Guizani M, Hossain MS. Advances, Evaluation, and Explainability of Large Language Models in Healthcare: A Systematic Review. ACM Trans Multimedia Comput Commun Appl. 2026;22(2). doi:10.1145/3786334
16. Ara L, Ben Miled Z, Boustani M, Mohanty S. Machine learning and artificial intelligence for delirium prediction with Electronic Health Records (EHR): a scoping review. BMC Med Informatics Decis Mak. 2026;26(1). doi:10.1186/s12911-026-03362-y
17. Aygun E, Imdat A, Dalgic N. Should we leave paediatric emergency triage to artificial intelligence? A comparison of ChatGPT 4o and Grok 3. Front Pediatr. 2026;14:1739217. doi:10.3389/fped.2026.1739217
18. Aziz Z, Mazeh AC, Ilic D, Ciardulli M, El Hadi SN, Camuccio A, et al. Leveraging AI simulations for enhancing cultural responsiveness and interprofessional collaboration in health professions education. Nurse Educ Pract. 2026;91:104711. doi:10.1016/j.nepr.2026.104711
19. Bagla P, Hanna J, Marthambadi B, Watkins S. Patterns of AI Use in Clinical Work by Hospitalists: Survey Study. J Med Internet Res. 2026;28:e85973. doi:10.2196/85973
20. Banco J, Soni A, Wong KLY, Ren LH, Xia R, Arora S, Hung L. Critical reflections from a transdisciplinary team on deploying an AI-enabled robot in long-term care. Nurse Educ Pract. 2026;91:104709. doi:10.1016/j.nepr.2026.104709"""


def event(event_type, doi, date, label=None):
    return {
        "type": event_type,
        "DOI": doi,
        "label": label or event_type.replace("_", " ").title(),
        "updated": {"date-time": date},
        "source": "retraction-watch",
    }


class IntegrityCorrectiveReleaseTests(unittest.TestCase):
    def test_submitted_benchmark_parses_as_exactly_twenty_references(self):
        rows = engine._merge_reference_lines(BENCHMARK_REFERENCES.splitlines(), "numeric")
        self.assertEqual(len(rows), 20)
        self.assertEqual(
            [engine._numeric_reference_marker_value(row) for row in rows],
            list(range(1, 21)),
        )
        self.assertIn("3739-3749. doi:10.1002/jmv.27789", rows[5])
        self.assertIn("Grok 3. Front Pediatr", rows[16])

    def test_bundled_crossref_rw_index_matches_submitted_answer_key(self):
        expected = [
            "retracted", "retracted", "retracted", "retracted", "retracted",
            "expression_of_concern", "corrected", "reinstated", "publication_notice",
            "clear", "clear", "clear", "clear", "clear", "clear", "clear", "clear",
            "clear", "clear", "clear",
        ]
        results = [
            fetch_crossref_publication_status(extract_doi(reference))
            for reference in BENCHMARK_REFERENCES.splitlines()
        ]
        self.assertTrue(all(result.get("checked") is True for result in results))
        self.assertEqual([result.get("publication_status") for result in results], expected)
        self.assertEqual(results[4]["publication_event_count"], 3)
        self.assertFalse(results[7]["is_retracted"])
        self.assertEqual(results[8]["work_role"], "publication_notice")

    def test_crossref_preserves_three_event_history_and_active_retraction(self):
        message = {
            "title": ["Friendship during patients' stable and unstable phases"],
            "update-to": [
                event("expression-of-concern", "10.1/eoc", "2024-01-01"),
                event("correction", "10.1/correction", "2024-06-01"),
                event("retraction", "10.1/retraction", "2025-01-01"),
            ],
        }
        events = events_from_crossref_message(message)
        summary = summarise_publication_events(events)
        self.assertEqual(summary["publication_event_count"], 3)
        self.assertEqual(summary["publication_status"], "retracted")
        self.assertTrue(summary["is_retracted"])
        self.assertEqual(
            summary["publication_event_types"],
            ["expression_of_concern", "correction", "retraction"],
        )

    def test_reinstatement_is_not_active_retraction(self):
        events = events_from_crossref_message({
            "title": ["Vitamin K2 trial"],
            "update-to": [
                event("retraction", "10.1/r", "2025-01-01"),
                event("reinstatement", "10.1/i", "2025-05-01"),
            ],
        })
        summary = summarise_publication_events(events)
        self.assertEqual(summary["publication_status"], "reinstated")
        self.assertFalse(summary["is_retracted"])

    def test_retraction_notice_is_legitimate_notice_not_retracted_work(self):
        message = {"title": ["Retraction: Downregulation of FBP1 promotes tumor metastasis"]}
        self.assertTrue(is_publication_notice(message))
        summary = summarise_publication_events([], is_notice=True)
        self.assertEqual(summary["publication_status"], "publication_notice")
        self.assertFalse(summary["is_retracted"])

    def test_acii_is_withheld_when_publication_coverage_is_incomplete(self):
        result = compute_acii(
            {"summary": {"reference_entries_found": 20}},
            [{"status": "verified", "publication_status_checked": True}] * 7,
        )
        self.assertIsNone(result["ACII"])
        self.assertFalse(result["score_available"])
        self.assertEqual(result["category"], "Incomplete Integrity Coverage")

    def test_acii_is_withheld_when_verification_row_count_is_inflated(self):
        rows = [
            {"status": "verified", "publication_status_checked": True, "publication_status": "clear"}
            for _ in range(25)
        ]
        result = compute_acii({"summary": {"reference_entries_found": 20}}, rows)
        self.assertIsNone(result["ACII"])
        self.assertFalse(result["coverage"]["row_count_matches_detected_references"])

    def test_acii_cannot_be_excellent_with_active_retraction(self):
        rows = [
            {
                "status": "verified",
                "publication_status_checked": True,
                "publication_status": "clear",
                "matched_year": 2026 - (index % 5),
                "matched_authors": f"Author {index}",
            }
            for index in range(20)
        ]
        rows[0]["publication_status"] = "retracted"
        rows[0]["is_retracted"] = True
        result = compute_acii({"summary": {"reference_entries_found": 20}}, rows)
        self.assertTrue(result["score_available"])
        self.assertLessEqual(result["ACII"], 49)
        self.assertEqual(result["category"], "Critical Review")

    def test_publication_safety_alerts_remain_visible_in_free_preview(self):
        base = {
            "summary": {"reference_entries_found": 1, "in_text_citations_found": 1},
            "source_risk_review": {
                "risks": [{
                    "priority": "critical",
                    "risk": "retracted_or_withdrawn",
                    "reference": "Affected work",
                }]
            },
        }
        free = apply_entitlements_to_result(base, paid=False)
        self.assertFalse(free["source_risk_review"].get("locked", False))
        self.assertEqual(free["source_risk_review"]["counts"]["critical"], 1)
        self.assertTrue(free["source_risk_review"]["safety_information_free"])

    def test_source_risk_distinguishes_reinstatement_and_notice(self):
        result = assess_source_risks({"online_verification": {"rows": [
            {"status": "verified", "publication_status_checked": True, "publication_status": "reinstated", "title": "Trial"},
            {"status": "verified", "publication_status_checked": True, "publication_status": "publication_notice", "work_role": "publication_notice", "is_corrected": True, "title": "Retraction: Notice"},
        ]}})
        names = [row["risk"] for row in result["risks"]]
        self.assertIn("reinstated_publication", names)
        self.assertIn("publication_notice", names)
        self.assertNotIn("retracted_or_withdrawn", names)

    def test_online_verification_is_enabled_by_default(self):
        upload = (ROOT / "templates" / "new_analyse.html").read_text(encoding="utf-8")
        main = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('id="onlineVerify" checked', upload)
        self.assertIn('enable_online_verification: str = Form("true")', main)


if __name__ == "__main__":
    unittest.main()
