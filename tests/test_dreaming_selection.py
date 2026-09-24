"""Outcome gates: supported text is not enough to authorize useful-memory writes."""

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.dreaming.ledger import DreamLedger
from memorymaster.dreaming.models import DreamCandidate, DreamDecision
from memorymaster.dreaming.source_review import CHECKS, bind_source, parse_review, record_review, review_allows_confirmation
from memorymaster.dreaming.worker import DreamWorker
from memorymaster.govern.jobs import validator


def useful_candidate():
    return DreamCandidate('dc-selection', 'The importer must compare external IDs before retrying a payment write.',
                          'constraint', 'payment importer', 'retry guard', 'compare external IDs',
                          'project', 'm1', 'compare external IDs before retrying a payment write', .9)


def source(scope='project:test'):
    return {'id': 1, 'scope': scope, 'provider': 'codex', 'session_hash': 'fixture', 'run_id': 'fixture',
            'messages': [{'message_id': 'm1', 'role': 'user',
                          'text': 'We reproduced double posting: compare external IDs before retrying a payment write. '
                                  'Keep this guard because retries run after every timeout.',
                          'timestamp': '2026-09-08T00:00:00Z'}]}


def accepted(candidate=None, row=None):
    bound = bind_source(candidate or useful_candidate(), row or source())
    return {'version': 2, 'verdict': 'accept', 'source_hash': bound.source_context['source_hash'],
            'checks': dict.fromkeys(CHECKS, True),
            'selection': {'destination': 'memory', 'kind': 'lesson', 'novelty': 'new',
                          'scope': (row or source())['scope'],
                          'memory_key': 'payment_importer.retry.external_id_guard',
                          'future_use': 'Prevent duplicate payment posting when a timeout triggers a retry.'}}


@pytest.fixture
def service(tmp_path):
    result = MemoryService(tmp_path / 'claims.db', workspace_root=tmp_path)
    result.init_db()
    return result


@pytest.mark.parametrize('verdict', ['reject', 'needs_evidence'])
@pytest.mark.parametrize('action', ['add', 'reinforce', 'propose_supersede', 'propose_conflict'])
def test_rejected_decision_cannot_create_claim_or_proposal(service, tmp_path, verdict, action):
    worker = DreamWorker(DreamLedger(tmp_path / 'capture.db'), service, None, None)
    review = {**accepted(), 'verdict': verdict}
    decision = DreamDecision('dc-selection', action, 'fixture', .9, target_claim_id=123, source_review=review)
    summary = {'candidate_writes': 0, 'proposals': 0}
    try:
        result = worker._apply_decision(source(), useful_candidate(), decision, summary)
    except ValueError:
        result = None  # A bad target must not hide an already-written rejected candidate.
    assert result is None
    assert service.list_claims(include_archived=True, limit=20) == []
    assert summary['candidate_writes'] == summary['proposals'] == 0
    assert service.list_events(event_type='audit', limit=20) == []


@pytest.mark.parametrize('missing', ['version', 'selection'])
def test_source_support_alone_cannot_authorize_new_write(service, tmp_path, missing):
    worker = DreamWorker(DreamLedger(tmp_path / 'capture.db'), service, None, None)
    review = {key: value for key, value in accepted().items() if key != missing}
    decision = DreamDecision('dc-selection', 'add', 'fixture', .9, source_review=review)
    with pytest.raises(ValueError, match='review|selection'):
        worker._apply_decision(source(), useful_candidate(), decision, {'candidate_writes': 0, 'proposals': 0})
    assert service.list_claims(limit=20) == []


@pytest.mark.parametrize('field,value', [('destination', 'project_docs'), ('kind', 'inventory'),
                                        ('novelty', 'duplicate'), ('future_use', ''),
                                        ('scope', 'project:other'), ('memory_key', '')])
def test_accept_must_demonstrate_retention_and_exact_scope(field, value):
    bound = bind_source(useful_candidate(), source())
    payload = accepted()
    payload['selection'][field] = value
    with pytest.raises(ValueError, match='review|selection'):
        parse_review(payload, bound)


def test_global_transport_cannot_become_project_memory():
    row = source('global')
    with pytest.raises(ValueError, match='review|scope'):
        parse_review(accepted(row=row), bind_source(useful_candidate(), row))


def test_generated_summary_cannot_launder_session_authority():
    row = source()
    row['messages'][0]['role'] = 'assistant'
    row['messages'][0]['text'] = 'This session is being continued from a previous conversation. Summary: ' + row['messages'][0]['text']
    with pytest.raises(ValueError, match='review|source'):
        parse_review(accepted(row=row), bind_source(useful_candidate(), row))


def _ingest(service):
    c = useful_candidate()
    return service.ingest(c.text, [CitationInput('dream-worker', 'dream:codex:fixture:m1', c.evidence_quote)],
                          scope='project:test', source_agent='dream-worker', claim_type=c.claim_type,
                          subject=c.subject, predicate=c.predicate, object_value=c.object_value, confidence=.9)


def test_legacy_source_receipt_cannot_confirm_new_memory(service):
    from memorymaster.dreaming.source_review import _claim_manifest, _hash
    claim = _ingest(service)
    with service.store.connect() as conn:
        claim_hash = _hash(_claim_manifest(conn, claim.id))
    old_checks = ('evidence', 'chronology', 'modality', 'scope', 'specificity', 'privacy')
    service.store.record_event(claim_id=claim.id, event_type='audit', details='dream_source_review_v1',
                               payload={'verdict': 'accept', 'checks': dict.fromkeys(old_checks, True), 'claim_hash': claim_hash})
    assert not review_allows_confirmation(service.store, claim.id)
    validator.run(service.store, min_score=0)
    assert service.store.get_claim(claim.id).status == 'candidate'
    with pytest.raises(ValueError, match='source.review'):
        service.store.apply_status_transition(claim, to_status='confirmed', reason='fixture', event_type='validator')


def test_useful_memory_is_candidate_then_cited_recalled_and_retired(service, tmp_path):
    worker = DreamWorker(DreamLedger(tmp_path / 'capture.db'), service, None, None)
    summary = {'candidate_writes': 0, 'proposals': 0}
    claim_id = worker._apply_decision(source(), useful_candidate(),
                                    DreamDecision('dc-selection', 'add', 'fixture', .9, source_review=accepted()), summary)
    assert service.store.get_claim(claim_id).status == 'candidate'
    assert service.query('payment importer', scope_allowlist=['project:test']) == []
    validator.run(service.store, min_score=0)
    found = service.query('payment importer', scope_allowlist=['project:test'])
    assert [c.id for c in found] == [claim_id]
    assert any(c.excerpt == useful_candidate().evidence_quote for c in found[0].citations)
    assert service.query('payment importer', scope_allowlist=['project:other']) == []
    service.store.apply_status_transition(service.store.get_claim(claim_id), to_status='archived',
                                          reason='retired fixture', event_type='transition')
    assert service.query('payment importer', scope_allowlist=['project:test']) == []


def test_accepted_review_can_not_be_reused_after_candidate_edit(service):
    claim = _ingest(service)
    bound = bind_source(useful_candidate(), source())
    record_review(service.store, claim.id, bound, parse_review(accepted(), bound))
    with service.store.connect() as conn:
        conn.execute('UPDATE claims SET text=? WHERE id=?', ('Different rule entirely', claim.id))
        conn.commit()
    assert not review_allows_confirmation(service.store, claim.id)


def test_retry_after_ingest_restores_receipt_without_losing_application(service, tmp_path):
    worker = DreamWorker(DreamLedger(tmp_path / 'capture.db'), service, None, None)
    claim = worker._ingest_candidate(source(), useful_candidate(), 'project:test')
    assert not review_allows_confirmation(service.store, claim.id)
    result = worker._apply_decision(source(), useful_candidate(),
                                   DreamDecision('dc-selection', 'add', 'retry', .9, source_review=accepted()),
                                   {'candidate_writes': 0, 'proposals': 0})
    assert result == claim.id
    assert len(service.list_claims(limit=20)) == 1
    assert review_allows_confirmation(service.store, claim.id)


def test_invalid_proposal_target_leaves_no_candidate(service, tmp_path):
    worker = DreamWorker(DreamLedger(tmp_path / 'capture.db'), service, None, None)
    with pytest.raises(ValueError, match='target'):
        worker._apply_decision(source(), useful_candidate(),
                              DreamDecision('dc-selection', 'propose_supersede', 'fixture', .9,
                                            target_claim_id=987, source_review=accepted()),
                              {'candidate_writes': 0, 'proposals': 0})
    assert service.list_claims(limit=20) == []


def test_ignored_malformed_receipt_cannot_block_reviewed_positive():
    import json
    import time
    from dataclasses import replace
    from memorymaster.dreaming.providers import consolidation_from_raw

    good = bind_source(useful_candidate(), source())
    ignored = bind_source(replace(useful_candidate(), candidate_id='ignored'), source())
    raw = {'decisions': [
        {'candidate_id': good.candidate_id, 'action': 'add', 'rationale': 'useful', 'confidence': .9,
         'source_review': accepted()},
        {'candidate_id': 'ignored', 'action': 'ignore', 'rationale': 'discard', 'confidence': .9,
         'source_review': {'checks': {'novelty': 'new'}}},
    ]}
    result = consolidation_from_raw(json.dumps(raw), [good, ignored], started=time.monotonic(),
                                    input_tokens=1, output_tokens=1, provider='fixture', model='fixture')
    assert [d.action for d in result.decisions] == ['add', 'ignore']


@pytest.mark.parametrize('boundary', ['tenant', 'agent', 'sensitive', 'scope'])
def test_proposal_cannot_target_inaccessible_claim(service, tmp_path, boundary):
    target_service = MemoryService(service.store.db_path, workspace_root=tmp_path, tenant_id='other') if boundary == 'tenant' else service
    kwargs = {'visibility': 'private'} if boundary == 'agent' else {}
    if boundary == 'sensitive':
        kwargs['visibility'] = 'sensitive'
    target = target_service.ingest('The old payment retry rule is obsolete.', [CitationInput('fixture')],
                                   scope='project:other' if boundary == 'scope' else 'project:test',
                                   source_agent='unrelated-agent', **kwargs)
    worker = DreamWorker(DreamLedger(tmp_path / 'capture.db'), service, None, None)
    with pytest.raises(ValueError, match='target'):
        worker._apply_decision(source(), useful_candidate(),
                              DreamDecision('dc-selection', 'propose_conflict', 'untrusted target ID', .9,
                                            target_claim_id=target.id, source_review=accepted()),
                              {'candidate_writes': 0, 'proposals': 0})
    assert service.list_events(claim_id=target.id, event_type='policy_decision', limit=20) == []
    assert not any(c.source_agent == 'dream-worker' for c in service.list_claims(limit=20))


def test_novelty_search_finds_older_rule_without_access_reinforcement_or_scope_leak(service, tmp_path):
    candidate = useful_candidate()
    old = service.ingest(candidate.text, [CitationInput('fixture')], scope='project:test', source_agent='fixture')
    service.store.apply_status_transition(old, to_status='confirmed', reason='fixture', event_type='validator')
    for number in range(201):
        claim = service.ingest(f'Unrelated orchard catalogue entry {number}.', [CitationInput('fixture')],
                               scope='project:test', source_agent='fixture')
        service.store.apply_status_transition(claim, to_status='confirmed', reason='fixture', event_type='validator')
    foreign = MemoryService(service.store.db_path, workspace_root=tmp_path, tenant_id='foreign')
    hidden = [
        foreign.ingest(candidate.text, [CitationInput('fixture')], scope='project:test', source_agent='fixture'),
        service.ingest(candidate.text, [CitationInput('fixture')], scope='project:other', source_agent='fixture'),
        service.ingest(candidate.text, [CitationInput('fixture')], scope='project:test', source_agent='private-owner', visibility='private'),
        service.ingest(candidate.text, [CitationInput('fixture')], scope='project:test', source_agent='sensitive-owner', visibility='sensitive'),
    ]
    assert old.id not in {c.id for c in service.list_claims(status='confirmed', limit=200)}
    before = service.store.get_claim(old.id)
    worker = DreamWorker(DreamLedger(tmp_path / 'capture.db'), service, None, None)
    references = worker._current_claims('project:test', [candidate])
    assert references[0]['id'] == old.id
    assert not {c.id for c in hidden} & {c['id'] for c in references}
    after = service.store.get_claim(old.id)
    assert (after.access_count, after.confidence, after.version) == (before.access_count, before.confidence, before.version)


def _captured_worker(service, tmp_path, keep):
    from datetime import datetime, timezone
    from memorymaster.dreaming.models import (
        CaptureEnvelope, ConsolidationResult, DreamMessage, ExtractionResult, ProviderUsage,
    )
    from memorymaster.dreaming.worker import DreamConfig

    usage = ProviderUsage('fixture', 'fixture', 200, 1, 10, 10, True)
    calls = []

    class Extractor:
        provider = model = 'fixture'

        def extract(self, messages, **kwargs):
            calls.append('extract')
            assert messages[0]['text'] == source()['messages'][0]['text']
            return ExtractionResult((useful_candidate(),), usage)

    class Consolidator:
        provider = model = 'fixture'

        def consolidate(self, candidates, current_claims, *, scope):
            calls.append('consolidate')
            review = accepted()
            review['source_hash'] = candidates[0].source_context['source_hash']
            if not keep:
                review['verdict'] = 'needs_evidence'
            return ConsolidationResult((DreamDecision('dc-selection', 'add', 'synthetic fixture', .9,
                                                       source_review=review),), usage)

    ledger = DreamLedger(tmp_path / 'capture.db')
    now = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
    ledger.enqueue(CaptureEnvelope('codex', 'fixture', 'project:test', source()['messages'][0]['timestamp'],
                                   source()['messages'][0]['timestamp'],
                                   (DreamMessage(**source()['messages'][0]),
                                    DreamMessage('m2', 'assistant', 'Recorded the reproduced failure.',
                                                 '2026-09-08T00:01:00Z')), 0, 100, 'fixture-hash'))
    worker = DreamWorker(ledger, service, Extractor(), Consolidator(),
                         config=DreamConfig(idle_minutes=1), now=lambda: now)
    return worker, calls


@pytest.mark.parametrize('keep', [True, False])
def test_capture_worker_to_steward_recall_retirement(service, tmp_path, keep):
    worker, calls = _captured_worker(service, tmp_path, keep)
    result = worker.run(apply_candidates=True)
    assert result['errors'] == 0
    assert result['candidate_writes'] == int(keep)
    assert calls == ['extract', 'consolidate']
    assert service.query('payment importer', scope_allowlist=['project:test']) == []
    validator.run(service.store, min_score=0)
    recalled = service.query('payment importer', scope_allowlist=['project:test'])
    assert len(recalled) == int(keep)
    assert worker.run(apply_candidates=True)['candidate_writes'] == 0
    assert calls == ['extract', 'consolidate']
    if keep:
        assert recalled[0].citations[0].excerpt == useful_candidate().evidence_quote
        assert service.query('payment importer', scope_allowlist=['project:other']) == []
        service.store.apply_status_transition(recalled[0], to_status='archived',
                                              reason='source withdrawn in fixture', event_type='transition')
        assert service.query('payment importer', scope_allowlist=['project:test']) == []
    else:
        assert service.list_claims(include_archived=True, limit=20) == []
