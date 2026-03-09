# ai_reconstruct.py

import os
import requests
import json

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

API_URL = "https://api.deepseek.com/v1/chat/completions"


def reconstruct_reference(reference):

    if not DEEPSEEK_API_KEY:
        return reference

    prompt = f"""
Clean and reconstruct the following academic reference.

Return ONLY the corrected reference string.

Reference:
{reference}
"""

    try:

        r = requests.post(
            API_URL,
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": "You are an academic citation reconstruction expert."},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.1
            },
            timeout=15
        )

        if r.status_code != 200:
            return reference

        data = r.json()

        content = data["choices"][0]["message"]["content"]

        return content.strip()

    except Exception:

        return reference
