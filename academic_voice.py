"""Explainable academic-writing diagnostics with optional passage rewriting.

The diagnostics are deliberately rule based. They identify writing patterns,
not authorship, and must never be presented as proof that AI wrote a passage.
Only a passage explicitly selected by the user is sent to the OpenAI API.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections import Counter
from typing import Any, Dict, Iterable, List


FORMULAIC_PHRASES = {
    "it is important to note": "Remove the stock introduction and state the point directly.",
    "it is worth noting": "State the evidence or qualification directly.",
    "in today's rapidly evolving": "Replace generic framing with the specific setting and period.",
    "in the contemporary": "Name the actual context instead of using broad framing.",
    "plays a crucial role": "Describe the mechanism or measured effect.",
    "a multifaceted approach": "Name the individual actions or dimensions.",
    "underscores the importance": "Explain the practical or theoretical implication precisely.",
    "this highlights the need": "State who should do what and why.",
    "in conclusion": "Use a conclusion only where the document structure requires it.",
    "furthermore": "Check whether the sentence adds a distinct idea or only extends a list.",
    "moreover": "Check whether the transition expresses a real logical relationship.",
}

CLAIM_MARKERS = re.compile(
    r"\b(shows?|demonstrates?|indicates?|reveals?|proves?|causes?|leads? to|"
    r"significantly|associated with|increases?|decreases?|majority|percent|%)\b",
    re.I,
)
CITATION_MARKER = re.compile(
    r"\((?=[^)]{1,180}\b(?:19|20)\d{2}[a-z]?\b)[^)]{1,180}\)|"
    r"\b[A-Z][A-Za-z&.'’ -]{1,80}\s+\((?:19|20)\d{2}[a-z]?\)|"
    r"\[\d+(?:[-–,]\s*\d+)*\]|\bdoi:\s*10\.",
    re.I,
)
PRESENT_STUDY_MARKER = re.compile(
    r"\b(this study|present study|our (?:results|findings|analysis)|the (?:results|findings)|"
    r"respondents?|sample|table\s+\d|coefficient|p[- ]?value|standard deviation|mean score|"
    r"cronbach(?:'s)? alpha|research question|hypothesis)\b",
    re.I,
)
TRANSITIONS = re.compile(
    r"^(however|furthermore|moreover|therefore|consequently|additionally|"
    r"in addition|on the other hand|in conclusion|overall)\b",
    re.I,
)

NON_PROSE_MARKERS = re.compile(
    r"\b(university of cape coast|dissertation submitted|candidate[’']s declaration|"
    r"list of tables|list of figures|acronyms?|abbreviation meaning|table\s+1\s+distribution|"
    r"partial fulfilment|award of master|statistical package for the social sciences|"
    r"participants were required to meet the following criteria)\b",
    re.I,
)


def _is_non_prose(sentence: str) -> bool:
    """Exclude front matter, inventories, headings and other extraction artefacts."""
    clean = _normalise(sentence)
    if NON_PROSE_MARKERS.search(clean):
        return True
    letters = [char for char in clean if char.isalpha()]
    upper_ratio = sum(char.isupper() for char in letters) / max(len(letters), 1)
    if len(clean.split()) >= 12 and upper_ratio > 0.72:
        return True
    # Objectives, recommendations and similar colon-led semicolon lists are
    # intentionally long enumerations, not evidence of artificial voice.
    if ":" in clean and clean.count(";") >= 2:
        return True
    # A short author-year entry containing title-like full stops is normally a
    # reference-list record, not a prose sentence requiring voice revision.
    if len(clean.split()) < 45 and re.search(r"\((?:19|20)\d{2}[a-z]?\)", clean) and clean.count(".") >= 2:
        return True
    return False


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _sentences(text: str) -> List[str]:
    clean = _normalise(text)
    if not clean:
        return []
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", clean) if len(s.split()) >= 4]


def _paragraphs(text: str) -> List[str]:
    return [_normalise(p) for p in re.split(r"\n\s*\n+", str(text or "")) if len(_normalise(p).split()) >= 12]


def _coefficient_of_variation(values: Iterable[int]) -> float:
    values = list(values)
    if len(values) < 2 or not sum(values):
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((x - mean) ** 2 for x in values) / len(values)
    return math.sqrt(variance) / mean if mean else 0.0


def _location(index: int, paragraph: int = 0) -> Dict[str, int]:
    return {"paragraph": paragraph + 1, "sentence": index + 1}


def analyse_academic_voice(text: str, max_passages: int = 30) -> Dict[str, Any]:
    """Return explainable style signals without inferring human or AI authorship."""
    paragraphs = _paragraphs(text)
    all_sentences = _sentences(text)
    sentence_lengths = [len(s.split()) for s in all_sentences]
    paragraph_lengths = [len(p.split()) for p in paragraphs]
    signals: List[Dict[str, Any]] = []
    formulaic_counts = Counter(
        phrase for sentence in all_sentences for phrase in FORMULAIC_PHRASES
        if phrase in sentence.lower() and not _is_non_prose(sentence)
    )

    for p_idx, paragraph in enumerate(paragraphs):
        sentences = _sentences(paragraph)
        paragraph_has_citation = bool(CITATION_MARKER.search(paragraph))
        openings: Counter[str] = Counter()
        for s_idx, sentence in enumerate(sentences):
            if _is_non_prose(sentence):
                continue
            lower = sentence.lower()
            opening = " ".join(re.findall(r"[a-z']+", lower)[:3])
            if opening:
                openings[opening] += 1
            found = [phrase for phrase in FORMULAIC_PHRASES if phrase in lower]
            if found and (found[0] not in {"furthermore", "moreover"} or formulaic_counts[found[0]] >= 4):
                phrase = found[0]
                signals.append({
                    "priority": "optional",
                    "signal": "formulaic_language",
                    "passage": sentence,
                    "location": _location(s_idx, p_idx),
                    "why_flagged": f"Contains the formulaic phrase ‘{phrase}’.",
                    "recommended_action": FORMULAIC_PHRASES[phrase],
                    "confidence": "medium",
                })
            word_count = len(sentence.split())
            if word_count > 55:
                signals.append({
                    "priority": "important" if word_count > 80 else "optional",
                    "signal": "very_long_sentence",
                    "passage": sentence,
                    "location": _location(s_idx, p_idx),
                    "why_flagged": f"The sentence contains {word_count} words and may combine several claims.",
                    "recommended_action": "Separate the claims and keep each citation close to the statement it supports.",
                    "confidence": "high" if word_count > 80 else "medium",
                })
        repeated = {opening for opening, count in openings.items() if count >= 2 and opening}
        if repeated and not _is_non_prose(paragraph):
            signals.append({
                "priority": "optional",
                "signal": "repeated_sentence_opening",
                "passage": paragraph[:700],
                "location": {"paragraph": p_idx + 1},
                "why_flagged": "Several sentences begin in the same way: " + ", ".join(sorted(repeated)),
                "recommended_action": "Vary the sentence structure only where it improves the logical flow.",
                "confidence": "medium",
            })

    transition_starts = sum(bool(TRANSITIONS.search(s)) for s in all_sentences)
    sentence_cv = _coefficient_of_variation(sentence_lengths)
    paragraph_cv = _coefficient_of_variation(paragraph_lengths)
    if len(all_sentences) >= 8 and sentence_cv < 0.23:
        signals.insert(0, {
            "priority": "important",
            "signal": "uniform_sentence_rhythm",
            "passage": "Document-level pattern",
            "location": {"scope": "document"},
            "why_flagged": "Sentence lengths show unusually little variation across the analysed text.",
            "recommended_action": "Review paragraph rhythm and combine or divide sentences only where the argument benefits.",
            "confidence": "medium",
        })

    # A supervisor normally gives representative comments instead of repeating
    # the same observation dozens of times. Cap each signal type before applying
    # the overall limit.
    type_limits = {
        "very_long_sentence": 10,
        "formulaic_language": 3,
        "repeated_sentence_opening": 4,
        "uniform_sentence_rhythm": 1,
    }
    limited_signals: List[Dict[str, Any]] = []
    type_counts: Counter[str] = Counter()
    for signal in signals:
        signal_type = signal.get("signal", "other")
        if type_counts[signal_type] >= type_limits.get(signal_type, 6):
            continue
        type_counts[signal_type] += 1
        limited_signals.append(signal)
    signals = limited_signals

    priority_rank = {"critical": 0, "important": 1, "optional": 2}
    signals.sort(key=lambda item: priority_rank.get(item.get("priority"), 9))
    signals = signals[: max(1, int(max_passages))]
    counts = Counter(item["priority"] for item in signals)
    signal_rate = len(signals) / max(len(all_sentences), 1)
    score = max(0, round(100 - counts["critical"] * 5 - min(35, signal_rate * 220)))
    return {
        "feature": "Authentic Academic Voice Review",
        "method": "explainable_rule_based_diagnostics",
        "authorship_inference": False,
        "disclaimer": (
            "These signals identify formulaic, unclear, inconsistent, or weakly supported writing patterns. "
            "They do not prove that a human or an AI system wrote any passage."
        ),
        "summary": {
            "sentences_reviewed": len(all_sentences),
            "paragraphs_reviewed": len(paragraphs),
            "critical": counts["critical"],
            "important": counts["important"],
            "optional": counts["optional"],
            "voice_readiness_score": score,
            "sentence_length_variation": round(sentence_cv, 3),
            "paragraph_length_variation": round(paragraph_cv, 3),
            "transition_opening_rate": round(transition_starts / max(len(all_sentences), 1), 3),
        },
        "signals": signals,
    }


def rewrite_selected_passage(
    passage: str,
    context: str = "",
    discipline: str = "",
    spelling: str = "British English",
    model: str | None = None,
) -> Dict[str, Any]:
    """Rewrite one user-selected passage. Full documents must not be passed here."""
    passage = _normalise(passage)
    context = _normalise(context)[:2000]
    if not passage:
        raise ValueError("A passage is required.")
    if len(passage.split()) > int(os.getenv("AI_REWRITE_MAX_INPUT_WORDS", "500")):
        raise ValueError("The selected passage exceeds the configured word limit.")
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("AI-assisted rewriting is not configured.")

    from openai import OpenAI

    primary = model or os.getenv("AI_REWRITE_MODEL", "gpt-5.6-luna")
    fallback = os.getenv("AI_REWRITE_FALLBACK_MODEL", "gpt-5.6-terra")
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "revised_passage": {"type": "string"},
            "changes_made": {"type": "array", "items": {"type": "string"}},
            "citations_preserved": {"type": "boolean"},
            "meaning_changed": {"type": "boolean"},
            "claims_requiring_verification": {"type": "array", "items": {"type": "string"}},
            "student_review_warning": {"type": "string"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        },
        "required": ["revised_passage", "changes_made", "citations_preserved", "meaning_changed", "claims_requiring_verification", "student_review_warning", "confidence"],
    }
    instructions = (
        "You are an academic writing assistant. Improve clarity and natural scholarly voice while preserving the author's meaning. "
        "Never invent, remove, replace, or alter citations, evidence, numerical findings, experiences, methods, or conclusions. "
        "Do not promise detector evasion. Flag any statement that cannot be revised safely. "
        f"Use {spelling}. Discipline: {discipline or 'not specified'}."
    )
    user_text = f"CONTEXT:\n{context or 'Not supplied'}\n\nSELECTED PASSAGE:\n{passage}"
    client = OpenAI()

    def call(chosen_model: str) -> Dict[str, Any]:
        response = client.responses.create(
            model=chosen_model,
            reasoning={"effort": os.getenv("AI_REWRITE_REASONING", "low")},
            instructions=instructions,
            input=user_text,
            text={"format": {"type": "json_schema", "name": "academic_revision", "strict": True, "schema": schema}},
            max_output_tokens=int(os.getenv("AI_REWRITE_MAX_OUTPUT_TOKENS", "1000")),
        )
        parsed = json.loads(response.output_text)
        parsed["model"] = chosen_model
        return parsed

    try:
        result = call(primary)
        if result.get("confidence") == "low" and fallback and fallback != primary:
            return call(fallback)
        return result
    except Exception:
        if fallback and fallback != primary:
            return call(fallback)
        raise
