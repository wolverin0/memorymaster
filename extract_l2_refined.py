#!/usr/bin/env python
"""L2 entity extraction from 1000 claims (refined: better name detection)."""
import json
import re
from pathlib import Path

# Load claims
with open('artifacts/_claims_temp.json', 'r') as f:
    rows = json.load(f)

output_file = Path('artifacts/haiku-l2-1000-2026-04-25.jsonl')
output_file.write_text('')

def extract_entities(text):
    """Extract L2 entities from claim text using refined heuristics."""
    entities = []
    seen_surfaces = set()

    # 1. Person names: only very high-confidence patterns
    # Famous people, or names mentioned with roles/descriptions
    # E.g., "Ada Lovelace", "Elon Musk", "Marie Curie"
    person_pattern = r'\b([A-Z][a-z]+\s+[A-Z][a-z]+)\b'
    exclude_names = {
        'Py Apps', 'Community Plugins', 'Host Authorization', 'The Personal', 'Personal Dashboard',
        'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday',
        'January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
        'September', 'October', 'November', 'December', 'Git Commit', 'Pull Request',
        'Code Review', 'Test Suite', 'Type System', 'Type Annotation'
    }
    for match in re.finditer(person_pattern, text):
        surface = match.group(1)
        if surface not in exclude_names and len(surface.split()) == 2:
            first, last = surface.split()
            # Only if both parts look like names (not all caps, not common words)
            if not (first.isupper() or last.isupper()) and first not in {'The', 'A', 'An', 'And', 'Or'}:
                if surface not in seen_surfaces:
                    entities.append({
                        'kind': 'person_name',
                        'surface_form': surface,
                        'aliases': []
                    })
                    seen_surfaces.add(surface)

    # 2. Spanish surnames (only specific ones that are definite surnames)
    spanish_surnames = {
        'Colombero', 'García', 'López', 'Martínez', 'González', 'Rodríguez',
        'Pérez', 'Hernández', 'Castillo', 'Mendoza', 'Morales', 'Sánchez',
        'Torres', 'Ramírez', 'Medina', 'Rojas', 'Delgado', 'Vargas'
    }
    for surname in spanish_surnames:
        if re.search(r'\b' + surname + r'\b', text):
            if surname not in seen_surfaces:
                entities.append({
                    'kind': 'spanish_surname',
                    'surface_form': surname,
                    'aliases': []
                })
                seen_surfaces.add(surname)

    # 3. Time expressions (more conservative)
    time_patterns = [
        r'\b(?:last|this|next|ultimo|proxima|siguiente)\s+(?:week|month|year|martes|miercoles|jueves|viernes|sabado|domingo|semana|mes|ano)\b',
        r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b',
        r'\b(?:january|february|march|april|may|june|july|august|september|october|november|december)\b',
        r'\b(?:enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre)\b',
    ]
    for pattern in time_patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            surface = match.group(0)
            if surface not in seen_surfaces:
                entities.append({
                    'kind': 'time_expression',
                    'surface_form': surface,
                    'aliases': []
                })
                seen_surfaces.add(surface)

    # 4. Model names (very specific)
    model_patterns = [
        r'\b(?:gpt-\d+(?:[a-z])?(?:-turbo)?(?:-vision)?)\b',
        r'\b(?:claude-\d+(?:\.\d+)?-(?:opus|sonnet|haiku)(?:-\d+)?)\b',
        r'\b(?:gemma\d+:[a-z0-9]+)\b',
        r'\b(?:llama\d+(?:-\d+)?(?:b)?)\b',
        r'\b(?:mistral-(?:small|medium|large|7b|8x7b))\b',
        r'\b(?:phi-\d+)\b',
        r'\b(?:falcon-\d+)\b',
        r'\b(?:mxbai-embed-\d+)\b',
    ]
    for pattern in model_patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            surface = match.group(0)
            if surface.lower() not in {s.lower() for s in seen_surfaces}:
                entities.append({
                    'kind': 'model_name',
                    'surface_form': surface,
                    'aliases': []
                })
                seen_surfaces.add(surface)

    # 5. Library/framework names
    lib_names = [
        'FastAPI', 'Flask', 'Django', 'Celery', 'SQLAlchemy', 'Pydantic', 'Pytest',
        'Qdrant', 'Weaviate', 'Pinecone', 'Milvus', 'ChromaDB', 'Cognee', 'LangChain', 'LlamaIndex', 'RAGFlow',
        'React', 'Vue', 'Angular', 'Svelte', 'Next.js', 'Nuxt', 'Remix', 'Astro',
        'Node.js', 'Express', 'Fastify', 'Nest.js', 'Koa',
        'Postgres', 'PostgreSQL', 'MySQL', 'MongoDB', 'Redis', 'SQLite', 'DuckDB',
        'Docker', 'Kubernetes', 'ArgoCD', 'Terraform', 'Ansible',
        'PyTorch', 'TensorFlow', 'Keras', 'JAX', 'Scikit-learn', 'Pandas', 'NumPy',
        'Obsidian', 'Notion', 'Confluence', 'Slack', 'Telegram',
        'GitHub', 'GitLab', 'Bitbucket', 'Gitea'
    ]
    for lib in lib_names:
        if re.search(r'\b' + re.escape(lib) + r'\b', text):
            if lib not in seen_surfaces:
                entities.append({
                    'kind': 'library_name',
                    'surface_form': lib,
                    'aliases': []
                })
                seen_surfaces.add(lib)

    # 6. Concepts (named abstract concepts)
    concept_patterns = [
        r'\bbitemporal modeling\b',
        r'\btemporary storage\b',
        r'\bevent capture\b',
        r'\bmulti-agent systems\b',
        r'\bagent coordination\b',
        r'\bvector search\b',
        r'\bsemantic retrieval\b',
        r'\bembedding database\b',
        r'\bprompt caching\b',
        r'\bcontext injection\b',
        r'\bprompt engineering\b',
        r'\bknowledge graph\b',
        r'\bmemory management\b',
        r'\bentity extraction\b',
        r'\brelationship linking\b',
        r'\bdata retrieval\b',
        r'\binformation extraction\b',
    ]
    for pattern in concept_patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            surface = match.group(0).strip()
            if surface and surface not in seen_surfaces:
                entities.append({
                    'kind': 'concept',
                    'surface_form': surface,
                    'aliases': []
                })
                seen_surfaces.add(surface)

    return entities

# Process all 1000
total_entities = 0
per_kind = {}
claims_with_entities = 0
samples = []

with open(output_file, 'a') as out:
    for idx, (claim_id, body) in enumerate(rows):
        entities = extract_entities(body)
        line = json.dumps({'claim_id': claim_id, 'entities': entities})
        out.write(line + '\n')

        total_entities += len(entities)
        if len(entities) > 0:
            claims_with_entities += 1
            if len(samples) < 5:
                for ent in entities:
                    samples.append((claim_id, ent['kind'], ent['surface_form']))
                    if len(samples) >= 5:
                        break

        for ent in entities:
            kind = ent['kind']
            per_kind[kind] = per_kind.get(kind, 0) + 1

        if (idx + 1) % 100 == 0:
            print(f"  Processed {idx + 1}/1000 claims")

print("\n=== FINAL STATS ===")
print(f"Total entities extracted: {total_entities}")
print(f"Per-kind breakdown:")
for kind in sorted(per_kind.keys()):
    count = per_kind[kind]
    print(f"  {kind}: {count}")
pct = 100 * claims_with_entities / len(rows)
print(f"Claims with >= 1 entity: {claims_with_entities} / {len(rows)} ({pct:.1f}%)")
if claims_with_entities > 0:
    avg = total_entities / claims_with_entities
    print(f"Avg entities/claim (non-empty): {avg:.2f}")
print(f"\nFirst 5 sample extractions (interesting):")
for i, (cid, kind, surface) in enumerate(samples, 1):
    print(f"  {i}. claim {cid}: {kind} = '{surface}'")
