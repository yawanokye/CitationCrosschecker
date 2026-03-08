import os
import json
import requests


DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

API_URL = "https://api.deepseek.com/v1/chat/completions"


def reconstruct_reference(reference: str):

    """
    Use AI to reconstruct missing citation metadata
    """

    if not DEEPSEEK_API_KEY:
        return None

    prompt = f"""
Extract bibliographic metadata from the following reference.

Return ONLY JSON with this format:

{{
"title":"",
"authors":[],
"year":"",
"journal":"",
"doi":""
}}

Reference:
{reference}
"""

    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "temperature": 0
    }

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }

    try:

        r = requests.post(API_URL, json=payload, headers=headers, timeout=20)

        if r.status_code != 200:
            return None

        txt = r.json()["choices"][0]["message"]["content"]

        txt = txt.replace("```json", "").replace("```", "").strip()

        return json.loads(txt)

    except Exception:
        return None
