#!/usr/bin/env python3
"""Entity extraction over a JSON batch of claims.

Inputs and outputs are CLI args so the script is portable across machines.
Codex P2 fix (mm-e431 follow-up): no hardcoded G:/_OneDrive/... paths.
"""
import argparse
import json
import re
from pathlib import Path

# Entity patterns
PATTERNS = {
    "person_name": [
        r"\b[A-Z][a-z]+\s+[A-Z][a-z]+\b",  # First Last
        r"\b[A-Z](?:ndrej|Karpathy|Lovelace)\b",  # Known names
    ],
    "spanish_surname": [
        r"\b(?:García|Messi|López|Rodríguez|Martínez|Hernández|González|Pérez|Fernández|Díaz)\b",
    ],
    "time_expression": [
        r"(?:next|last|this)\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)",
        r"(?:el|la)\s+(?:lunes|martes|miércoles|jueves|viernes|sábado|domingo)(?:\s+(?:pasado|próximo|que viene))?",
        r"Q[1-4]\s+20\d{2}",
        r"in\s+\d+\s+(?:days|weeks|months|hours|minutes)",
        r"(?:next|last)\s+(?:week|month|quarter|year)",
    ],
    "model_name": [
        r"(?:gpt-4o(?:-mini)?|claude-(?:opus|sonnet|haiku)(?:-\d+)?(?:-\d+)?|gemini-\d+\.?\d*-(?:flash|pro)|llama-\d+|mistral)",
        r"\b(?:GPT-4|GPT-3\.5|Claude|Gemini|Llama|Mistral)\b",
    ],
    "library_name": [
        r"\b(?:FastAPI|React|Django|Flask|SQLAlchemy|PyTorch|TensorFlow|pandas|numpy|pytest|Webpack|Vite|Next\.js|Vue|Angular|Express|Node\.js|pyafipws|Qdrant|PostgreSQL|SQLite|Redis|Celery|Gunicorn|pytest|Docker|Kubernetes|Terraform|Ansible|Prometheus|Grafana|ELK|Spring|Hibernate|Maven|Gradle|Kotlin|Rust|Go|Ruby|PHP|Java|C\#|C\+\+|JavaScript|TypeScript)\b",
    ],
    "concept": [
        r"(?:writer-lock|RRF\s+fusion|AFIP\s+electronic\s+invoicing|byzantine\s+consensus|consensus\s+algorithm|state\s+machine|replication|vector\s+search|embedding|retrieval|ranking|BM25|semantic\s+similarity|recall|precision|F1|MAP|NDCG|cross-encoder|knowledge\s+graph|ontology|schema|validation|schema\s+evolution|backward\s+compatibility)",
    ],
}

def extract_entities(text):
    """Extract entities from claim text."""
    entities = []
    seen = set()

    # person_name: look for capital letters in pairs
    person_matches = re.finditer(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b', text)
    for m in person_matches:
        surface = m.group()
        if surface not in seen and len(entities) < 8:
            entities.append({"kind": "person_name", "surface_form": surface})
            seen.add(surface)

    # time_expression: structured date/time patterns
    time_patterns = [
        (r'\b(?:next|last|this)\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|week|month|quarter|year)\b', 'time_expression'),
        (r'\b(?:el|la)\s+(?:lunes|martes|miércoles|jueves|viernes|sábado|domingo)(?:\s+(?:pasado|próximo|que\s+viene))?\b', 'time_expression'),
        (r'\bQ[1-4]\s+20\d{2}\b', 'time_expression'),
        (r'\bin\s+\d+\s+(?:days?|weeks?|months?|hours?|minutes?)\b', 'time_expression'),
    ]
    for pattern, kind in time_patterns:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            surface = m.group()
            if surface not in seen and len(entities) < 8:
                entities.append({"kind": kind, "surface_form": surface})
                seen.add(surface)

    # model_name: LLM model identifiers
    model_patterns = [
        r'\bgpt-4o(?:-mini)?\b',
        r'\bGPT-[34](?:\.[50])?\b',
        r'\bclaude-(?:opus|sonnet|haiku)(?:-\d+)*\b',
        r'\bgemini-\d+(?:\.\d)?-(?:flash|pro)\b',
        r'\bClaude\b',
        r'\bGPT\b',
        r'\bGemini\b',
        r'\b(?:Llama|Mistral|Falcon)\b',
    ]
    for pattern in model_patterns:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            surface = m.group()
            if surface not in seen and len(entities) < 8:
                entities.append({"kind": "model_name", "surface_form": surface})
                seen.add(surface)

    # library_name: software frameworks/libraries
    lib_patterns = [
        r'\b(?:FastAPI|SQLAlchemy|PyTorch|TensorFlow|pytest|Webpack|Next\.js|Express|SQLite|PostgreSQL|Docker|Kubernetes|Terraform|Redis|Celery|Qdrant|pyafipws|FastMCP)\b',
        r'\b(?:React|Django|Flask|pandas|numpy|Vue|Angular|Spring|Hibernate)\b',
    ]
    for pattern in lib_patterns:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            surface = m.group()
            if surface not in seen and len(entities) < 8:
                entities.append({"kind": "library_name", "surface_form": surface})
                seen.add(surface)

    # concept: domain-specific concepts
    concept_patterns = [
        r'\b(?:writer-lock|RRF\s+fusion|AFIP\s+electronic\s+invoicing|byzantine\s+consensus)\b',
        r'\b(?:BM25|semantic\s+similarity|cross-encoder|knowledge\s+graph|state\s+machine)\b',
        r'\b(?:vector\s+search|embedding|retrieval|ranking|consensus)\b',
    ]
    for pattern in concept_patterns:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            surface = m.group()
            if surface not in seen and len(entities) < 8:
                entities.append({"kind": "concept", "surface_form": surface})
                seen.add(surface)

    # spanish_surname: solo apellido patterns
    spanish_surnames = ['García', 'Messi', 'López', 'Rodríguez', 'Martínez', 'Hernández', 'González', 'Pérez', 'Fernández', 'Díaz']
    for surname in spanish_surnames:
        for m in re.finditer(r'\b' + surname + r'\b', text):
            surface = m.group()
            if surface not in seen and len(entities) < 8:
                entities.append({"kind": "spanish_surname", "surface_form": surface})
                seen.add(surface)
                break  # Only once per surname

    return entities

def main():
    repo_root = Path(__file__).resolve().parent
    default_input = repo_root / "artifacts" / "l2-haiku-batches" / "in-batch01.json"
    default_output = repo_root / "artifacts" / "l2-haiku-batches" / "out-batch01.json"

    parser = argparse.ArgumentParser(description="Extract L2 entities from a JSON batch of claims.")
    parser.add_argument("--input", "-i", type=Path, default=default_input,
                        help=f"Input JSON file with claims [default: {default_input}]")
    parser.add_argument("--output", "-o", type=Path, default=default_output,
                        help=f"Output JSON file [default: {default_output}]")
    args = parser.parse_args()
    input_path = args.input
    output_path = args.output

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(input_path, 'r', encoding='utf-8') as f:
        claims = json.load(f)

    results = []
    total_entities = 0

    for claim in claims:
        entities = extract_entities(claim['text'])
        total_entities += len(entities)
        results.append({
            "id": claim['id'],
            "entities": entities
        })

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"done: {len(results)} claims processed, {total_entities} entities extracted")

if __name__ == '__main__':
    main()
