# acii.py
import numpy as np
import pandas as pd
from math import log


# ---------- helpers ----------

def _safe_div(a, b):
    return a / b if b else 0


def _herfindahl(values):
    total = sum(values)
    if total == 0:
        return 0
    shares = [(v / total) ** 2 for v in values]
    return sum(shares)


def _normalized_entropy(values):
    total = sum(values)
    if total == 0:
        return 0

    probs = [v / total for v in values if v > 0]

    entropy = -sum(p * log(p) for p in probs)

    max_entropy = log(len(values)) if len(values) > 1 else 1

    return 1 - (entropy / max_entropy)


# ---------- main computation ----------

def compute_acii(data):

    """
    data must contain columns:
    citations_in_text
    author
    journal
    year
    peer_reviewed
    self_citation
    """

    df = pd.DataFrame(data)

    if df.empty:
        return {"ACII": 0, "rating": "No data"}

    total_citations = df["citations_in_text"].sum()

    # ------------------------------
    # C1 Reference concentration
    # ------------------------------

    ref_counts = df["citations_in_text"].values
    C1 = _normalized_entropy(ref_counts)

    # ------------------------------
    # C2 Author concentration
    # ------------------------------

    author_counts = (
        df.groupby("author")["citations_in_text"]
        .sum()
        .values
    )

    C2 = _herfindahl(author_counts)

    # ------------------------------
    # C3 Journal concentration
    # ------------------------------

    journal_counts = (
        df.groupby("journal")["citations_in_text"]
        .sum()
        .values
    )

    C3 = _herfindahl(journal_counts)

    # ------------------------------
    # C4 Self citation risk
    # ------------------------------

    self_cites = df[df["self_citation"] == True]["citations_in_text"].sum()

    SC = _safe_div(self_cites, total_citations)

    SC_MAX = 0.15

    C4 = min(SC / SC_MAX, 1)

    # ------------------------------
    # C5 Temporal risk
    # ------------------------------

    CURRENT_YEAR = 2025
    THRESHOLD = 10

    old_cites = df[
        df["year"] < (CURRENT_YEAR - THRESHOLD)
    ]["citations_in_text"].sum()

    C5 = _safe_div(old_cites, total_citations)

    # ------------------------------
    # C6 Source credibility
    # ------------------------------

    non_peer = df[df["peer_reviewed"] == False]["citations_in_text"].sum()

    C6 = _safe_div(non_peer, total_citations)

    components = np.array([C1, C2, C3, C4, C5, C6])

    acii_score = 1 - components.mean()

    rating = interpret_acii(acii_score)

    return {
        "ACII": round(acii_score, 3),
        "rating": rating,
        "Reference Concentration": round(C1, 3),
        "Author Concentration": round(C2, 3),
        "Journal Concentration": round(C3, 3),
        "Self Citation Risk": round(C4, 3),
        "Temporal Risk": round(C5, 3),
        "Source Credibility Risk": round(C6, 3),
    }


# ---------- interpretation ----------

def interpret_acii(score):

    if score >= 0.80:
        return "Excellent citation integrity"

    elif score >= 0.65:
        return "Strong citation integrity"

    elif score >= 0.50:
        return "Moderate citation integrity"

    elif score >= 0.35:
        return "Weak citation integrity"

    else:
        return "Poor citation integrity"
