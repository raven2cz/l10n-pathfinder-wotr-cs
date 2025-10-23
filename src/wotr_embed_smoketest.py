#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wotr_embed_smoketest.py
Ověří volání embeddings a spočítá kosinovou podobnost.
Používá model 'text-embedding-3-small' (levnější).
"""

import os, math
import numpy as np

# umožní .env, ale když tam není, nevadí
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

API_KEY = os.getenv("OPENAI_API_KEY")
BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")  # ponech default

if not API_KEY:
    raise SystemExit("Chybí OPENAI_API_KEY (env nebo .env).")

def cosine(a, b):
    a = np.array(a, dtype=np.float32)
    b = np.array(b, dtype=np.float32)
    denom = (np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / denom) if denom else 0.0

def main():
    try:
        # preferuj oficiální SDK
        from openai import OpenAI
        client = OpenAI(api_key=API_KEY, base_url=BASE_URL, timeout=180)

        texts = ["Hello from Kenabres.", "Ahoj z Kenabres."]
        res = client.embeddings.create(model="text-embedding-3-small", input=texts)
        embs = [res.data[i].embedding for i in range(len(texts))]

        print(f"OK (SDK). dim={len(embs[0])}, cos(H/A)={cosine(embs[0], embs[1]):.4f}")

    except Exception as e_sdk:
        # fallback na čistý REST přes requests
        import requests, json
        headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
        payload = {"model": "text-embedding-3-small", "input": ["Hello from Kenabres.", "Ahoj z Kenabres."]}
        r = requests.post(BASE_URL.rstrip("/") + "/embeddings", headers=headers, data=json.dumps(payload), timeout=180)
        r.raise_for_status()
        data = r.json()
        embs = [data["data"][i]["embedding"] for i in range(len(payload["input"]))]
        print(f"OK (requests). dim={len(embs[0])}, cos(H/A)={cosine(embs[0], embs[1]):.4f}")

if __name__ == "__main__":
    main()
