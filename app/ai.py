# Copyright (c) 2026 John Harris. All rights reserved. Proprietary.
"""Documentation briefing for the reviewer. This code does not approve or deny.

The default checker runs in process so a clinical note never leaves the network
by accident. A BAA-covered chat endpoint can be turned on with PRIOR_AUTH_AI_BASE_URL.
Whatever comes back is scanned. If it tries to decide the case, we throw that
text away and keep the in-process checklist instead. We do not store the rejected text.
"""

import hashlib
import json
import re
from dataclasses import dataclass

import httpx

# Decision language from a model is discarded. "decide" and "determination" are allowed
# because the safe copy uses them to say a person still has to make the call.
_FORBIDDEN = re.compile(
    r"\b(approv(?:e|ed|al)|den(?:y|ied|ial)|reject(?:ed|ion)?)\b|not medically necessary",
    re.IGNORECASE,
)

_SYSTEM = (
    "You check whether a prior-authorization note documents the diagnosis and procedure codes. "
    "You do not make a coverage determination and you must not recommend one. "
    "Return a JSON object with keys missing_information (array of strings), "
    "extracted_facts (array of strings), and narrative (string). "
    "The narrative only lists missing documentation."
)


class AiOutputRejected(Exception):
    """The model used decision language. The message is empty on purpose so logs stay clean."""


@dataclass(frozen=True)
class Briefing:
    missing_information: tuple[str, ...]
    extracted_facts: tuple[str, ...]
    narrative: str


def sanitize(briefing: Briefing) -> Briefing:
    _walk(briefing.narrative)
    _walk(list(briefing.missing_information))
    _walk(list(briefing.extracted_facts))
    return briefing


def _walk(value: object) -> None:
    if isinstance(value, str):
        if _FORBIDDEN.search(value):
            raise AiOutputRejected()
        return
    if isinstance(value, list):
        for item in value:
            _walk(item)


STATIC_FALLBACK = Briefing(
    missing_information=("The documentation assistant could not be used.",),
    extracted_facts=(),
    narrative="Read the clinical note yourself. A person still has to make the coverage determination.",
)


class DocumentationChecker:
    """Looks for the diagnosis and each procedure code in the note. Nothing more."""

    name = "documentation-checker"
    model = "rules-v1"

    def brief(self, packet: dict) -> Briefing:
        note = str(packet.get("clinical_note") or "")
        diagnosis = str(packet["diagnosis_code"])
        procedures = list(packet["procedures"])
        missing: list[str] = []
        if not note.strip():
            missing.append("A clinical note was not attached.")
        else:
            folded = note.upper().replace(".", "")
            if diagnosis.upper().replace(".", "") not in folded:
                missing.append(f"The note does not mention diagnosis {diagnosis}.")
            for procedure in procedures:
                code = str(procedure["code"])
                if code.upper() not in note.upper():
                    missing.append(f"The note does not mention procedure {code}.")
        facts = [
            f"Date of service {packet['date_of_service']}.",
            f"Diagnosis {diagnosis}.",
        ]
        for procedure in procedures:
            facts.append(f"Procedure {procedure['code']} for {procedure['units']} units.")
        if packet.get("units_confirmed"):
            facts.append("The office explicitly confirmed a unit count above 30.")
        if missing:
            narrative = (
                "Documentation still needed: "
                + " ".join(missing)
                + " A person must make the coverage determination."
            )
        else:
            narrative = (
                "The note mentions the diagnosis and each procedure code. "
                "A person must make the coverage determination."
            )
        return sanitize(
            Briefing(
                missing_information=tuple(missing),
                extracted_facts=tuple(facts),
                narrative=narrative,
            )
        )


class HttpBriefingProvider:
    name = "openai-compatible"
    model = "remote"

    def __init__(self, base_url: str, api_key: str | None, model: str, client: httpx.Client | None = None) -> None:
        self.name = "openai-compatible"
        self.model = model
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._client = client or httpx.Client()
        self._owns_client = client is None

    def brief(self, packet: dict) -> Briefing:
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        response = self._client.post(
            self._url,
            headers=headers,
            json={
                "model": self.model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": json.dumps(packet, sort_keys=True)},
                ],
            },
            timeout=5.0,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        parsed = _parse_model_json(content)
        briefing = Briefing(
            missing_information=tuple(str(item) for item in parsed.get("missing_information", [])),
            extracted_facts=tuple(str(item) for item in parsed.get("extracted_facts", [])),
            narrative=str(parsed.get("narrative", "")),
        )
        return sanitize(briefing)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


def _parse_model_json(content: str) -> dict:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("model JSON must be an object")
    return data


def packet_hash(packet: dict) -> str:
    raw = json.dumps(packet, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def build_provider(base_url: str | None, api_key: str | None, model: str) -> DocumentationChecker | HttpBriefingProvider:
    if base_url:
        return HttpBriefingProvider(base_url, api_key, model)
    return DocumentationChecker()
