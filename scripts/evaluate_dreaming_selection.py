"""Run a bounded, blinded synthetic selection check with the configured Gemini CLI."""
from __future__ import annotations

import argparse
import ast
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import runpy
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from memorymaster.core.antigravity_client import AntigravityClient  # noqa: E402
from memorymaster.dreaming.providers import (  # noqa: E402
    _without_markdown_fence, consolidation_from_raw, consolidation_prompt,
)
from memorymaster.dreaming.source_review import REVIEW_INSTRUCTIONS  # noqa: E402


def _baseline_instructions(ref: str | None) -> str | None:
    if not ref:
        return None
    source = subprocess.check_output(['git', 'show', f'{ref}:memorymaster/dreaming/source_review.py'],
                                     cwd=ROOT, text=True, encoding='utf-8')
    return next(ast.literal_eval(node.value) for node in ast.parse(source).body
                if isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == 'REVIEW_INSTRUCTIONS'
                    for target in node.targets))


def _response_record(client, prompt, candidates, scope, arm, cached):
    fingerprint = hashlib.sha256(prompt.encode()).hexdigest()
    previous = next((row for row in cached if row['scope'] == scope and row['arm'] == arm
                     and row.get('prompt_sha256') == fingerprint
                     and row.get('response', {}).get('model') == client.model), None)
    record = {'scope': scope, 'arm': arm, 'prompt_sha256': fingerprint, 'blinded': True}
    started = time.monotonic()
    try:
        record['response'] = previous['response'] if previous else asdict(client.complete(prompt))
        record['reused_response'] = previous is not None
        response = record['response']
        decisions = json.loads(_without_markdown_fence(response['text']))['decisions']
        if arm == 'selection_v2':
            result = consolidation_from_raw(response['text'], candidates, started=started,
                input_tokens=response['input_tokens'], output_tokens=response['output_tokens'],
                provider='antigravity', model=client.model)
            decisions = [decision.to_dict() for decision in result.decisions]
        ids = [decision['candidate_id'] for decision in decisions]
        if len(ids) != len(candidates) or set(ids) != {c.candidate_id for c in candidates}:
            raise ValueError('missing, duplicate or unknown candidate decisions')
        record['decisions'] = decisions
    except Exception as exc:
        record['error'] = type(exc).__name__ + ': ' + str(exc)[:250]
    return record


def _metrics(records, expected):
    output = {}
    for arm in sorted({record['arm'] for record in records}):
        rows = [record for record in records if record['arm'] == arm]
        actual = {d['candidate_id']: d['action'] != 'ignore' for r in rows for d in r.get('decisions', [])}
        tp = sum(value and expected[key] for key, value in actual.items())
        fp = sum(value and not expected[key] for key, value in actual.items())
        fn = sum(not value and expected[key] for key, value in actual.items())
        output[arm] = {'cases': len(actual), 'tp': tp, 'fp': fp, 'fn': fn,
            'tn': len(actual) - tp - fp - fn, 'precision': tp / (tp + fp) if tp + fp else None,
            'recall': tp / (tp + fn) if tp + fn else None,
            'input_tokens': sum(r.get('response', {}).get('input_tokens', 0) for r in rows),
            'output_tokens': sum(r.get('response', {}).get('output_tokens', 0) for r in rows),
            'recorded_calls': len(rows), 'new_calls': sum(not r.get('reused_response', False) for r in rows),
            'passed': actual == expected and not any('error' in r for r in rows)}
    return output


def _write_receipts(path, records):
    receipts = [{'scope': row['scope'], 'prompt_sha256': row['prompt_sha256'],
                 'response_text': row['response']['text'], 'model': row['response']['model'],
                 'input_tokens': row['response']['input_tokens'], 'output_tokens': row['response']['output_tokens']}
                for row in records if row['arm'] == 'selection_v2']
    path.write_text(json.dumps(receipts, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--baseline-ref', help='Optional Git revision for paired prior selection instructions')
    parser.add_argument('--reuse', type=Path, help='Reuse only responses with identical prompt hash and model')
    parser.add_argument('--write-receipts', type=Path, help='Write regression receipts only when every expected v2 outcome matches')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    case_bytes = (ROOT / 'tests/fixtures/dreaming_selection_cases.json').read_bytes()
    cases = json.loads(case_bytes)
    prepare = runpy.run_path(str(ROOT / 'tests/test_dreaming_selection_provider.py'))['prepared_cases']
    baseline = _baseline_instructions(args.baseline_ref)
    cached = json.loads(args.reuse.read_text(encoding='utf-8')) if args.reuse else []
    client = AntigravityClient(work_dir=args.output / 'agy-isolated')
    records, expected = [], {}
    for scope in sorted({case['scope'] for case in cases}):
        candidates, references, scope_expected = prepare(scope, cases)
        expected.update(scope_expected)
        for arm in (('baseline', 'selection_v2') if baseline else ('selection_v2',)):
            prompt = consolidation_prompt(candidates, references, scope)
            if arm == 'baseline':
                prompt = prompt.replace(REVIEW_INSTRUCTIONS, baseline)
            record = _response_record(client, prompt, candidates, scope, arm, cached)
            records.append(record)
            (args.output / 'provider-results.json').write_text(json.dumps(records, ensure_ascii=True, indent=2), encoding='utf-8')
            print(json.dumps({'scope': scope, 'arm': arm, 'error': record.get('error'),
                              'reused': record.get('reused_response', False)}), flush=True)
            if 'response' not in record:
                return 3
    report = {'fixture_sha256': hashlib.sha256(case_bytes).hexdigest(), 'baseline_ref': args.baseline_ref,
              'label_origin': 'AI-authored development fixtures, not human or production acceptance',
              'metrics': _metrics(records, expected)}
    (args.output / 'provider-metrics.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    (args.output / 'frozen-cases.json').write_bytes(case_bytes)
    passed = report['metrics']['selection_v2']['passed']
    if args.write_receipts and passed:
        _write_receipts(args.write_receipts, records)
    print(json.dumps(report, indent=2))
    return 0 if passed else 3


if __name__ == '__main__':
    raise SystemExit(main())
