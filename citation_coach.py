"""Rule-based learning explanations for recurring citation mistakes."""

from __future__ import annotations

import re
from typing import Any, Dict, List


CITATION = re.compile(r"\([A-Z][A-Za-z'’-]+(?:\s+et\s+al\.)?,?\s+(?:19|20)\d{2}[a-z]?\)|\[\d+(?:[-–,]\s*\d+)*\]", re.I)
CLAIM = re.compile(r"\b(shows?|indicates?|demonstrates?|reveals?|causes?|increases?|decreases?|significantly|majority|percent|%)\b", re.I)


def build_citation_coach(result: Dict[str, Any]) -> Dict[str, Any]:
    text = str(result.get("main_text") or result.get("full_text") or result.get("document_text") or result.get("text") or "")
    paragraphs = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n+", text) if len(p.split()) >= 15]
    lessons: List[Dict[str, Any]] = []
    for p_index, paragraph in enumerate(paragraphs):
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", paragraph) if s.strip()]
        claims = [s for s in sentences if CLAIM.search(s)]
        citations = CITATION.findall(paragraph)
        if len(claims) >= 2 and len(citations) == 1 and CITATION.search(sentences[-1] if sentences else ""):
            lessons.append({
                "priority":"important", "pattern":"one_citation_for_multiple_claims", "location":{"paragraph":p_index + 1},
                "passage":paragraph[:900],
                "explanation":f"This paragraph appears to make {len(claims)} factual or empirical claims but places only one citation at the end. It is unclear which claim the source supports.",
                "recommended_action":"Place each citation immediately after the claim it supports, add further sources where needed, or revise unsupported claims.",
            })
        if re.search(r"\b(as cited in|cited in|quoted in)\b", paragraph, re.I):
            lessons.append({
                "priority":"important", "pattern":"secondary_citation", "location":{"paragraph":p_index + 1}, "passage":paragraph[:900],
                "explanation":"The passage appears to rely on a secondary citation. Readers may assume the original source was consulted.",
                "recommended_action":"Consult and cite the original source where possible. If it cannot be accessed, format the secondary citation transparently according to the required style.",
            })
    uncited = result.get("uncited_references") or []
    if uncited:
        lessons.append({"priority":"important", "pattern":"unused_reference_entries", "location":{"section":"References"}, "passage":f"{len(uncited)} reference entries appear unused.", "explanation":"References included but not used reduce reference-list accuracy.", "recommended_action":"Cite each source where it supports the work or remove it from the reference list."})
    return {
        "method":"explainable_rule_based_coaching",
        "lessons":lessons,
        "lesson_count":len(lessons),
        "notice":"These explanations teach citation practice and require human review. They do not replace institutional guidance or supervision.",
    }

