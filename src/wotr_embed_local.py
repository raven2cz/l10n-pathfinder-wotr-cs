#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wotr_embed_local.py
-------------------
Lokální embeddings přes HuggingFace SentenceTransformers.
První běh model stáhne do cache (~%USERPROFILE%/.cache/huggingface), pak offline.

Funkce:
- embed_texts_with_progress(texts, model="paraphrase-multilingual-MiniLM-L12-v2", batch_size=64, device auto)
  => vrací list vektorů (list[float]), normalizovaných (L2=1), takže dot product = cosine.
"""

from __future__ import annotations
from typing import List, Optional
import numpy as np

DEFAULT_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"

def embed_texts_with_progress(
    texts: List[str],
    model: str = DEFAULT_MODEL,
    batch_size: int = 64,
    device: Optional[str] = None,   # "cpu" | "cuda" | None(auto)
    show_progress: bool = True,
) -> List[List[float]]:
    from sentence_transformers import SentenceTransformer
    import torch

    if device is None:
        device = "cuda" if torch.cuda.is_available() else "cpu"

    # Pozn.: normalize_embeddings=True => už jsou normalizované (cosine = dot product)
    st = SentenceTransformer(model, device=device)
    arr = st.encode(
        texts,
        batch_size=batch_size,
        show_progress_bar=show_progress,
        convert_to_numpy=True,
        normalize_embeddings=True,
    )
    # převod na čisté Python floaty kvůli JSON/serializaci případně
    return [row.astype(float).tolist() for row in np.asarray(arr)]
