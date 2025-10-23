#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wotr_feminize_service.py
------------------------
Servis pro přepis řádku do ženského rodu mluvčí (adresát = muž).
- Čte JSON s { "system_rules": "...", "user_prefix": "..." } – doporučuju tvůj opravený prompt.
- Volá /v1/responses (OpenAI-compatible), bez temperature.
- Robustní extrakce textu z odpovědi (podporuje několik struktur).
- Pokud je výstup identický a detekujeme jasné maskulinní 1.os. tvary, provede 2. dotaz v "PATCH mode"
  se seznamem přesných náhrad (deterministicky).
- Volitelné debug dumpy žádostí/odpovědí.

Použití:
    svc = FeminizeService(prompts_path="prompts-feminine.json", model="gpt-5-mini")
    out = svc.feminize(line, speaker="Seelah")
"""

from __future__ import annotations
import os, json, time, re
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
import requests

DEFAULT_MODEL = "gpt-5-mini"
DEFAULT_BASE_URL = "https://api.openai.com/v1"

# --- Bezpečné maskulinní indikátory (1. os.) -> jednoznačné přepisy ---
PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\babych byl\b", re.IGNORECASE), "abych byla"),
    (re.compile(r"\bbyl jsem\b", re.IGNORECASE), "byla jsem"),
    (re.compile(r"\bjsem rád\b", re.IGNORECASE), "jsem ráda"),
    (re.compile(r"\brád bych\b", re.IGNORECASE), "ráda bych"),
    (re.compile(r"\břekl jsem\b", re.IGNORECASE), "řekla jsem"),
    (re.compile(r"\bzapomněl jsem\b", re.IGNORECASE), "zapomněla jsem"),
    (re.compile(r"\budělal jsem\b", re.IGNORECASE), "udělala jsem"),
    (re.compile(r"\bmyslel jsem\b", re.IGNORECASE), "myslela jsem"),
    (re.compile(r"\bprosil jsem\b", re.IGNORECASE), "prosila jsem"),
    (re.compile(r"\babych byl upřímný\b", re.IGNORECASE), "abych byla upřímná"),
]

def find_masc_indicators(text: str) -> List[Tuple[str, str]]:
    """Najde jasné maskulinní 1.os. tvary a vrátí seznam (nalezený_tvar, ženský_tvar)."""
    found: List[Tuple[str, str]] = []
    seen = set()
    for rx, fem in PATTERNS:
        for m in rx.finditer(text):
            src = m.group(0)
            key = (src.lower(), fem.lower())
            if key not in seen:
                seen.add(key)
                found.append((src, fem))
    return found

def _strip_code_fence(s: str) -> str:
    """Když se model „uplete“ a vrátí ```...```, vyndáme vnitřek. Triple quotes v obsahu NEcháváme."""
    t = s.strip()
    if t.startswith("```") and t.endswith("```"):
        inner = t.strip("`")
        # Po "```" může být jazyk – pokusně odřízneme první řádek
        parts = inner.splitlines()
        if parts and not parts[0].strip():
            parts = parts[1:]
        elif parts:
            # řádek s jazykem pryč
            parts = parts[1:]
        return "\n".join(parts).rstrip("\r\n")
    return s

class FeminizeService:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        timeout_s: int = 180,
        retries: int = 6,
        prompts_path: Optional[str | Path] = None,
        debug_dir: Optional[Path] = None,
    ):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY") or ""
        if not self.api_key:
            raise RuntimeError("Missing API key. Set OPENAI_API_KEY or pass api_key=...")

        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.model = model
        self.timeout_s = int(timeout_s)
        self.retries = int(retries)
        self.debug_dir = Path(debug_dir) if debug_dir else None

        if prompts_path:
            p = Path(prompts_path)
            j = json.loads(p.read_text(encoding="utf-8"))
            self.system_rules = j.get("system_rules", "").strip()
            self.user_prefix = j.get("user_prefix", "").strip()
        else:
            # Fallback: stručný bezpečný prompt (doporučuju ale dodat vlastní JSON)
            self.system_rules = (
                "You are a precise Czech localization rewriter for game dialogue. "
                "Convert the SPEAKER to FEMALE, keep ADDRESSEE (player) MALE. "
                "Change only the speaker's gendered morphology. Preserve markup {g|…}…{/g}, {name}, {n}…{/n}, punctuation, quotes, and spacing exactly. "
                "Do not rephrase. Return only the line."
            )
            self.user_prefix = (
                "SPEAKER (female): {speaker}\nADDRESSEE: male\n"
                "Rewrite the line so ONLY the SPEAKER's forms are feminine; keep 2nd-person masculine intact. "
                "Preserve markup exactly. Return only the line.\n\nLINE: "
            )

        self._url = f"{self.base_url}/responses"
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        })

    def _dump(self, stem: str, kind: str, payload: Any):
        if not self.debug_dir:
            return
        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            (self.debug_dir / f"{stem}.{kind}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    @staticmethod
    def _extract_text(resp_json: Dict[str, Any]) -> str:
        # 1) OpenAI responses style
        if isinstance(resp_json.get("output_text"), str):
            return resp_json["output_text"]
        # 2) Tool-style output list
        if isinstance(resp_json.get("output"), list):
            out = []
            for item in resp_json["output"]:
                cont = item.get("content")
                if isinstance(cont, list):
                    for c in cont:
                        t = c.get("text")
                        if isinstance(t, str):
                            out.append(t)
            if out:
                return "".join(out)
        # 3) Chat choices
        if isinstance(resp_json.get("choices"), list) and resp_json["choices"]:
            msg = resp_json["choices"][0].get("message", {})
            cont = msg.get("content")
            if isinstance(cont, str):
                return cont
            if isinstance(cont, list):
                chunks = [c.get("text") for c in cont if isinstance(c, dict) and "text" in c]
                if any(chunks):
                    return "".join([c for c in chunks if isinstance(c, str)])
        # 4) Nested response
        if isinstance(resp_json.get("response"), dict):
            r = resp_json["response"]
            if isinstance(r.get("output_text"), str):
                return r["output_text"]
        raise ValueError("Unable to extract text from response JSON.")

    def _build_messages(self, line: str, speaker: str, hints: List[Tuple[str,str]] | None = None) -> List[Dict[str, str]]:
        up = self.user_prefix.replace("{speaker}", speaker or "")
        user_content = f"{up}{line}"
        if hints:
            bullets = "\n".join([f"- '{src}' → '{dst}'" for src, dst in hints])
            user_content += (
                "\n\nDETECTED_MASC_TOKENS:\n"
                + bullets
                + "\nApply these exact changes if present and keep everything else identical."
            )
        return [
            {"role": "system", "content": self.system_rules},
            {"role": "user",   "content": user_content},
        ]

    def _call(self, messages: List[Dict[str,str]], stem: str) -> str:
        payload = {"model": self.model, "input": messages}
        last_err = None
        for attempt in range(1, self.retries + 1):
            try:
                if self.debug_dir and attempt == 1:
                    self._dump(stem, "request", payload)
                r = self._session.post(self._url, json=payload, timeout=self.timeout_s)
                if r.status_code >= 400:
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text[:800]}")
                j = r.json()
                if self.debug_dir and attempt == 1:
                    self._dump(stem, "response", j)
                out = self._extract_text(j)
                out = _strip_code_fence(out).rstrip("\r\n")
                return out
            except Exception as e:
                last_err = e
                time.sleep(min(6.0 * attempt, 18.0))
        raise RuntimeError(f"API failed after {self.retries} attempts: {last_err}")

    def feminize(self, line: str, speaker: str = "") -> str:
        stem = "last_call"
        hints = find_masc_indicators(line)
        out = self._call(self._build_messages(line, speaker, hints=hints), stem=stem)
        if out != line:
            return out
        if hints:  # PATCH mód – deterministické náhrady
            rules = "\n".join([f"- Replace EXACT '{src}' with EXACT '{dst}'" for src, dst in hints])
            strict_user = (
                "PATCH MODE — perform ONLY these replacements on the line and keep EVERYTHING else identical:\n"
                f"{rules}\n\nLINE:\n{line}"
            )
            messages = [
                {"role": "system", "content": (
                    "You are a deterministic patcher. Replace exactly the listed substrings if present; "
                    "do not alter any other characters, quotes, markup, or spacing. Return ONLY the final line."
                )},
                {"role": "user", "content": strict_user},
            ]
            out2 = self._call(messages, stem=stem + "_patch")
            return out2
        return line

    def close(self):
        try:
            self._session.close()
        except Exception:
            pass
