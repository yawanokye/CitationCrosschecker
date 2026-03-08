# verify.py

import os
import re
import time
from typing import List, Dict, Any, Optional, Tuple

import requests
from rapidfuzz import fuzz

from ai_reconstruct import reconstruct_reference


# ============================================================
# ENVIRONMENT CONFIG
# ============================================================

_ALLOWED_VERIFY_STATUSES = {"verified","likely","needs_review","not_found","offline"}

MAILTO = (
    os.getenv("CITATION_CROSSCHECKER_MAILTO")
    or os.getenv("CROSSREF_MAILTO")
    or os.getenv("OPENALEX_MAILTO")
    or ""
).strip()

UNPAYWALL_EMAIL = (os.getenv("UNPAYWALL_EMAIL") or MAILTO or "").strip()

# AI reconstruction limits
AI_REPAIR_LIMIT = 25
_ai_repair_calls = 0


# ============================================================
# TEXT HELPERS
# ============================================================

def _normalize_verify_status(s: str) -> str:
    st = (s or "").strip().lower().replace(" ","_")
    if st not in _ALLOWED_VERIFY_STATUSES:
        st = "needs_review"
    return st


def _safe_str(x: Any) -> str:
    try:
        return str(x) if x is not None else ""
    except:
        return ""


def _safe_strip(x: Any) -> str:
    return _safe_str(x).strip()


def _norm_text(s: str) -> str:
    s = _safe_strip(s).lower()
    s = re.sub(r"[^\w\s\-:/]"," ",s)
    s = re.sub(r"\s+"," ",s).strip()
    return s


def _safe_get_json(url:str,params:Optional[dict]=None,timeout:int=22):

    try:
        r = requests.get(
            url,
            params=params,
            timeout=timeout,
            headers={
                "User-Agent":"CitationCrosschecker/1.0",
                "Accept":"application/json"
            }
        )

        if r.status_code != 200:
            return None

        return r.json()

    except:
        return None


# ============================================================
# DOI / YEAR
# ============================================================

_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})(?:[a-z])?\b")

def _extract_year(text:str)->str:

    m=_YEAR_RE.search(text or "")

    return m.group(1) if m else ""


def _extract_doi(text:str)->str:

    m=re.search(r"(10\.\d{4,9}/[^\s]+)",text or "",flags=re.I)

    return _safe_strip(m.group(1)).rstrip(").,;") if m else ""


def _strip_leading_numbering(text:str)->str:

    return re.sub(r"^\s*(\[\d+\]|\d+[\.\)])\s*","",_safe_strip(text))


# ============================================================
# FIELD EXTRACTION
# ============================================================

def _extract_common_fields(ref:str):

    ref=_safe_strip(ref)

    result={
        "authors":[],
        "year":"",
        "title":"",
        "journal":"",
        "doi":_extract_doi(ref)
    }

    clean=_strip_leading_numbering(ref)

    year=_extract_year(clean)

    result["year"]=year

    if year:

        parts=re.split(year,clean,maxsplit=1)

        if len(parts)>=2:

            author_block=parts[0]
            rest=parts[1]

            for a in re.split(r",|and|&",author_block):

                a=a.strip()

                if a:

                    name=a.split()[0].lower()

                    result["authors"].append(name)

            result["authors"]=result["authors"][:3]

            title=rest.split(".")[0]

            result["title"]=title.strip()

    return result


# ============================================================
# AI REPAIR
# ============================================================

def _ai_repair_reference(ref_raw,fields):

    global _ai_repair_calls

    if _ai_repair_calls >= AI_REPAIR_LIMIT:
        return fields

    title=_safe_strip(fields.get("title"))

    if title and len(title)>12:
        return fields

    try:

        ai=reconstruct_reference(ref_raw)

        if not ai:
            return fields

        _ai_repair_calls+=1

        for k,v in ai.items():

            if v and not fields.get(k):
                fields[k]=v

        fields["ai_repaired"]=True

    except:
        pass

    return fields


# ============================================================
# QUERY BUILDERS
# ============================================================

def _build_query(fields):

    parts=[]

    if fields.get("authors"):
        parts.append(" ".join(fields["authors"]))

    if fields.get("title"):

        words=[w for w in fields["title"].split() if len(w)>3][:5]

        parts.extend(words)

    if fields.get("year"):
        parts.append(fields["year"])

    return " ".join(parts)[:280]


# ============================================================
# API QUERIES
# ============================================================

def _query_crossref(q):

    url="https://api.crossref.org/works"

    params={
        "query.bibliographic":q,
        "rows":5,
        "sort":"score",
        "order":"desc"
    }

    if MAILTO:
        params["mailto"]=MAILTO

    data=_safe_get_json(url,params)

    if not data:
        return []

    items=data.get("message",{}).get("items",[])

    return [{"source":"crossref","item":it} for it in items]


def _query_openalex(q):

    url="https://api.openalex.org/works"

    params={"search":q,"per-page":5}

    if MAILTO:
        params["mailto"]=MAILTO

    data=_safe_get_json(url,params)

    if not data:
        return []

    return [{"source":"openalex","item":it} for it in data.get("results",[])]


def _query_semantic_scholar(q):

    url="https://api.semanticscholar.org/graph/v1/paper/search"

    params={
        "query":q,
        "limit":5,
        "fields":"title,year,authors,externalIds"
    }

    data=_safe_get_json(url,params)

    if not data:
        return []

    return [{"source":"semantic_scholar","item":it} for it in data.get("data",[])]


# ============================================================
# CANDIDATE EXTRACTION
# ============================================================

def _candidate_fields(cand):

    src=cand.get("source")

    item=cand.get("item",{})

    doi=""
    title=""
    year=""
    authors=[]

    if src=="crossref":

        doi=_safe_strip(item.get("DOI"))

        titles=item.get("title",[])

        title=_norm_text(titles[0] if titles else "")

        year=_safe_str(
            (((item.get("published-print") or {}).get("date-parts") or [[None]])[0][0])
        )

        for au in item.get("author",[])[:5]:

            fam=_safe_strip(au.get("family")).lower()

            authors.append(fam)

    elif src=="openalex":

        doi=_safe_strip(item.get("doi","")).replace("https://doi.org/","")

        title=_norm_text(item.get("title"))

        year=_safe_str(item.get("publication_year"))

        for a in item.get("authorships",[])[:5]:

            name=a.get("author",{}).get("display_name","")

            if name:

                authors.append(name.split()[-1].lower())

    elif src=="semantic_scholar":

        title=_norm_text(item.get("title"))

        year=_safe_str(item.get("year"))

        ext=item.get("externalIds",{})

        doi=_safe_strip(ext.get("DOI"))

        for a in item.get("authors",[])[:5]:

            name=a.get("name","")

            if name:

                authors.append(name.split()[-1].lower())

    return doi,title,year,authors


# ============================================================
# SCORING
# ============================================================

def _score(ref_title,ref_auth,ref_year,cand_title,cand_auth,cand_year):

    title_score=fuzz.token_set_ratio(ref_title,cand_title)

    author_overlap=len(set(ref_auth).intersection(set(cand_auth)))

    year_match=1 if ref_year and cand_year and ref_year[:4]==cand_year[:4] else 0

    score=(title_score*1.35)+(author_overlap*26)+(year_match*8)

    return int(score),title_score,author_overlap,year_match


# ============================================================
# MAIN VERIFICATION
# ============================================================

def verify_references_batch(
    references:List[str],
    max_to_check:int=0,
    throttle_s:float=0.12,
    use_crossref=True,
    use_openalex=True,
    use_semantic_scholar=True
):

    refs=[r for r in references if _safe_strip(r)]

    if max_to_check:
        refs=refs[:max_to_check]

    rows=[]

    for ref in refs:

        ref_raw=_safe_strip(ref)

        fields=_extract_common_fields(ref_raw)

        fields=_ai_repair_reference(ref_raw,fields)

        query=_build_query(fields)

        ref_title=_norm_text(fields.get("title"))

        ref_auth=fields.get("authors",[])

        ref_year=fields.get("year","")

        ref_doi=fields.get("doi","")

        row={
            "reference":ref_raw,
            "query":query,
            "status":"offline",
            "score":0,
            "doi":ref_doi,
            "ai_repaired":fields.get("ai_repaired",False)
        }

        candidates=[]

        if use_crossref:
            candidates+=_query_crossref(query)
            time.sleep(throttle_s)

        if use_openalex:
            candidates+=_query_openalex(query)
            time.sleep(throttle_s)

        if use_semantic_scholar:
            candidates+=_query_semantic_scholar(query)
            time.sleep(throttle_s)

        best_score=0

        for cand in candidates:

            doi,title,year,auth=_candidate_fields(cand)

            score,title_s,auth_o,year_m=_score(
                ref_title,ref_auth,ref_year,
                title,auth,year
            )

            if score>best_score:

                best_score=score

                row.update({
                    "score":score,
                    "matched_title":title,
                    "matched_year":year,
                    "matched_authors":",".join(auth),
                    "doi":doi,
                    "source":cand["source"]
                })

        if best_score>=120:
            row["status"]="verified"
        elif best_score>=100:
            row["status"]="likely"
        elif best_score>=70:
            row["status"]="needs_review"
        else:
            row["status"]="not_found"

        rows.append(row)

    return rows
