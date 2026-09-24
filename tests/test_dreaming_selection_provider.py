"""Frozen actual-model receipts: changed selection prompts require fresh evidence."""
import hashlib
import json
import time
from pathlib import Path

from memorymaster.dreaming.models import DreamCandidate
from memorymaster.dreaming.providers import consolidation_from_raw, consolidation_prompt
from memorymaster.dreaming.source_review import bind_source


FIXTURES = Path(__file__).parent / 'fixtures'


def prepared_cases(scope, cases):
    """Opaque IDs and shuffled ordering keep expected labels out of model input."""
    candidates = []
    expected = {}
    for case in cases:
        if case['scope'] != scope:
            continue
        opaque = 'dc-' + hashlib.sha256(case['case_id'].encode()).hexdigest()[:20]
        message_ids = {m['message_id']: 'm-' + hashlib.sha256(
            (opaque + m['message_id']).encode()).hexdigest()[:16] for m in case['messages']}
        payload = {**case['candidate'], 'candidate_id': opaque,
                   'evidence_message_id': message_ids[case['candidate']['evidence_message_id']]}
        row = {'scope': scope, 'messages': [{**m, 'message_id': message_ids[m['message_id']]}
                                          for m in case['messages']]}
        candidates.append(bind_source(DreamCandidate(**payload), row))
        expected[opaque] = case['expected_keep']
    references = list({r['id']: r for c in cases if c['scope'] == scope for r in c['current_claims']}.values())
    return sorted(candidates, key=lambda c: c.candidate_id), references, expected


def test_actual_provider_selection_receipts_cover_positive_and_negative_cases():
    cases = json.loads((FIXTURES / 'dreaming_selection_cases.json').read_text(encoding='utf-8'))
    receipts = json.loads((FIXTURES / 'dreaming_selection_provider_v2.json').read_text(encoding='utf-8'))
    assert {r['scope'] for r in receipts} == {c['scope'] for c in cases}
    assert len(receipts) == len({c['scope'] for c in cases})
    outcomes = {}
    for receipt in receipts:
        candidates, references, expected = prepared_cases(receipt['scope'], cases)
        prompt = consolidation_prompt(candidates, references, receipt['scope'])
        assert hashlib.sha256(prompt.encode()).hexdigest() == receipt['prompt_sha256'], 'Refresh bounded model evidence after a prompt/input change'
        result = consolidation_from_raw(receipt['response_text'], candidates, started=time.monotonic(),
                                        input_tokens=receipt['input_tokens'], output_tokens=receipt['output_tokens'],
                                        provider='antigravity', model=receipt['model'])
        actual = {d.candidate_id: d.action != 'ignore' for d in result.decisions}
        assert actual == expected
        outcomes.update(actual)
    assert len(outcomes) == len(cases)
    assert sum(outcomes.values()) == sum(c['expected_keep'] for c in cases) > 0
    assert not all(outcomes.values())
