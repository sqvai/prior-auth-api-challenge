# Copyright (c) 2026 John Harris. All rights reserved. Proprietary.

import json

import httpx
import pytest

from app.ai import (
    AiOutputRejected,
    Briefing,
    DocumentationChecker,
    HttpBriefingProvider,
    build_provider,
    packet_hash,
    sanitize,
)


def test_remote_model_that_tries_to_approve_is_rejected():
    def handler(_request: httpx.Request) -> httpx.Response:
        payload = {
            "missing_information": [],
            "extracted_facts": ["looks fine"],
            "narrative": "I recommend we APPROVE the knee replacement.",
        }
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)}}]})

    provider = HttpBriefingProvider(
        "http://ai.example/v1",
        "secret",
        "test-model",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(AiOutputRejected):
        provider.brief(
            {
                "clinical_note": "M17.11 27447",
                "date_of_service": "2026-12-24",
                "diagnosis_code": "M17.11",
                "procedures": [{"code": "27447", "units": 1}],
                "units_confirmed": False,
            }
        )


def test_remote_model_json_in_a_fence_is_accepted_when_it_does_not_decide():
    def handler(_request: httpx.Request) -> httpx.Response:
        text = '```json\n{"missing_information": ["laterality"], "extracted_facts": ["Diagnosis M17.11."], "narrative": "Documentation still needed: laterality."}\n```'
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})

    provider = HttpBriefingProvider(
        "http://ai.example/v1",
        None,
        "test-model",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    briefing = provider.brief(
        {
            "clinical_note": "note",
            "date_of_service": "2026-12-24",
            "diagnosis_code": "M17.11",
            "procedures": [{"code": "27447", "units": 1}],
            "units_confirmed": False,
        }
    )
    assert briefing.missing_information == ("laterality",)
    provider.close()


def test_a_note_that_omits_the_diagnosis_or_a_code_is_flagged():
    briefing = DocumentationChecker().brief(
        {
            "clinical_note": "Knee pain. Planned 27447.",
            "date_of_service": "2026-12-24",
            "diagnosis_code": "M17.11",
            "procedures": [{"code": "27447", "units": 1}, {"code": "99213", "units": 1}],
            "units_confirmed": False,
        }
    )
    text = " ".join(briefing.missing_information)
    assert "M17.11" in text
    assert "99213" in text
    assert "27447" not in text
    sanitize(Briefing(("fine",), (1,), "A person must make the coverage determination."))


def test_remote_failures_do_not_become_a_decision():
    def down(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "down"})

    def not_object(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "[]"}}]})

    packet = {
        "clinical_note": "note",
        "date_of_service": "2026-12-24",
        "diagnosis_code": "M17.11",
        "procedures": [{"code": "27447", "units": 1}],
        "units_confirmed": False,
    }
    down_provider = HttpBriefingProvider(
        "http://ai.example/v1", None, "m", client=httpx.Client(transport=httpx.MockTransport(down))
    )
    with pytest.raises(httpx.HTTPStatusError):
        down_provider.brief(packet)
    bad_provider = HttpBriefingProvider(
        "http://ai.example/v1", None, "m", client=httpx.Client(transport=httpx.MockTransport(not_object))
    )
    with pytest.raises(ValueError):
        bad_provider.brief(packet)


def test_decision_language_in_a_fact_is_rejected_and_the_client_closes():
    with pytest.raises(AiOutputRejected):
        sanitize(Briefing(("please deny this",), ("fine",), "A person still determines this."))
    remote = build_provider("http://ai.example/v1", "k", "remote-model")
    assert remote.model == "remote-model"
    remote.close()
    assert isinstance(build_provider(None, None, "m"), DocumentationChecker)
    assert packet_hash({"b": 1, "a": 2}) == packet_hash({"a": 2, "b": 1})
