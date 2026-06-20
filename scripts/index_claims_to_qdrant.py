"""Bulk-index memorymaster claims into a Qdrant collection for vector search.

Embeds ``subject + text`` for every non-archived claim using sentence-transformers
(all-MiniLM-L6-v2, 384-dim by default) and upserts into Qdrant. Idempotent:
uses a deterministic UUID-v5 derived from the claim id as the Qdrant point id,
so re-running the script updates existing points rather than creating dupes.

Usage::

    python scripts/index_claims_to_qdrant.py \
        --db memorymaster.db \
        --qdrant-url http://localhost:6333 \
        --collection memorymaster-claims

Environment variables (respected as defaults when flags are omitted):

    MEMORYMASTER_QDRANT_URL           (default: http://localhost:6333)
    MEMORYMASTER_QDRANT_COLLECTION    (default: memorymaster-claims)
    MEMORYMASTER_EMBED_MODEL          (default: all-MiniLM-L6-v2)

READ-ONLY against the SQLite DB — never writes to it.
"""
from __future__ import annotations

import argparse
import io
import logging
import os
import sys
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# Ensure UTF-8 stdout on Windows so we can print claim text safely.
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("index_claims_to_qdrant")

DEFAULT_COLLECTION = "memorymaster-claims"
DEFAULT_EMBED_MODEL = "all-MiniLM-L6-v2"
DEFAULT_QDRANT_URL = "http://localhost:6333"
POINT_NAMESPACE = uuid.UUID("6e9a0f8a-0000-5000-8000-000000000001")
# Non-archived statuses we want searchable. Deliberately excludes
# ``archived`` and ``superseded`` — those shouldn't surface in recall.
SEARCHABLE_STATUSES = ("confirmed", "candidate", "stale", "conflicted")


def _point_id(claim_id: int) -> str:
    return str(uuid.uuid5(POINT_NAMESPACE, f"mm-claim-{claim_id}"))


def _claim_text(subject: str | None, text: str) -> str:
    subject = (subject or "").strip()
    text = (text or "").strip()
    if subject and subject not in text:
        return f"{subject}: {text}"
    return text or subject


def _iter_claims(db_path: Path):
    """Yield (id, scope, subject, text, status, confidence) tuples for every
    non-archived, non-superseded claim. Read-only, streaming."""
    import sqlite3

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        placeholders = ",".join("?" for _ in SEARCHABLE_STATUSES)
        cur = conn.execute(
            f"""
            SELECT id, scope, subject, text, status, confidence
              FROM claims
             WHERE status IN ({placeholders})
             ORDER BY id
            """,
            SEARCHABLE_STATUSES,
        )
        for row in cur:
            yield row
    finally:
        conn.close()


def _count_claims(db_path: Path) -> int:
    import sqlite3

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        placeholders = ",".join("?" for _ in SEARCHABLE_STATUSES)
        return int(
            conn.execute(
                f"SELECT COUNT(*) FROM claims WHERE status IN ({placeholders})",
                SEARCHABLE_STATUSES,
            ).fetchone()[0]
        )
    finally:
        conn.close()


def _render_progress(done: int, total: int, started_at: float) -> str:
    pct = (100.0 * done / total) if total else 0.0
    elapsed = time.monotonic() - started_at
    rate = done / elapsed if elapsed > 0 else 0.0
    eta_sec = (total - done) / rate if rate > 0 else 0.0
    bar_width = 30
    filled = int(bar_width * done / total) if total else 0
    bar = "#" * filled + "-" * (bar_width - filled)
    return (
        f"[{bar}] {done}/{total} ({pct:5.1f}%)  "
        f"rate={rate:5.1f}/s  eta={eta_sec / 60:4.1f}m  "
        f"elapsed={elapsed / 60:4.1f}m"
    )


def _load_embedder(model_name: str):
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise SystemExit(
            "sentence-transformers is required. Install with:\n"
            "    pip install memorymaster[vector]\n"
            "or\n"
            "    pip install sentence-transformers qdrant-client"
        ) from exc
    logger.info("Loading sentence-transformers model: %s", model_name)
    return SentenceTransformer(model_name)


def _load_qdrant(url: str):
    try:
        from qdrant_client import QdrantClient
    except ImportError as exc:
        raise SystemExit(
            "qdrant-client is required. Install with:\n"
            "    pip install memorymaster[vector]\n"
            "or\n"
            "    pip install qdrant-client"
        ) from exc
    logger.info("Connecting to Qdrant at %s", url)
    return QdrantClient(url=url, timeout=30.0)


def _ensure_collection(client, collection: str, dims: int) -> None:
    from qdrant_client.models import Distance, VectorParams

    existing = {c.name for c in client.get_collections().collections}
    if collection in existing:
        info = client.get_collection(collection)
        vec_params = info.config.params.vectors
        existing_dims = getattr(vec_params, "size", None)
        if existing_dims is not None and existing_dims != dims:
            raise SystemExit(
                f"Collection '{collection}' already exists with size={existing_dims}, "
                f"but embedder produces size={dims}. Refuse to overwrite."
            )
        logger.info("Using existing collection '%s' (size=%d)", collection, dims)
        return
    logger.info("Creating collection '%s' (size=%d, Cosine)", collection, dims)
    client.create_collection(
        collection_name=collection,
        vectors_config=VectorParams(size=dims, distance=Distance.COSINE),
    )


def _upsert_batch(client, collection: str, points: list) -> None:
    # qdrant-client's upsert is idempotent on point id — same id overwrites.
    client.upsert(collection_name=collection, points=points, wait=False)


def index_claims(
    db_path: Path,
    qdrant_url: str,
    collection: str,
    embed_model: str,
    batch_size: int = 64,
    limit: int | None = None,
) -> dict[str, int]:
    from qdrant_client.models import PointStruct

    embedder = _load_embedder(embed_model)
    dims = int(embedder.get_sentence_embedding_dimension())
    client = _load_qdrant(qdrant_url)
    _ensure_collection(client, collection, dims)

    total = _count_claims(db_path)
    if limit is not None:
        total = min(total, limit)
    logger.info("Indexing up to %d claims from %s", total, db_path)

    started_at = time.monotonic()
    stats = {"total": total, "indexed": 0, "skipped": 0, "errors": 0}

    texts: list[str] = []
    metas: list[dict] = []
    done = 0
    last_progress = started_at

    def _flush() -> None:
        nonlocal texts, metas, done
        if not texts:
            return
        try:
            vectors = embedder.encode(
                texts,
                batch_size=min(32, len(texts)),
                show_progress_bar=False,
                normalize_embeddings=True,
            )
        except Exception as exc:
            logger.warning("encode batch of %d failed: %s", len(texts), exc)
            stats["errors"] += len(texts)
            texts = []
            metas = []
            return
        points = [
            PointStruct(id=m["point_id"], vector=vec.tolist(), payload=m["payload"])
            for m, vec in zip(metas, vectors, strict=True)
        ]
        try:
            _upsert_batch(client, collection, points)
            stats["indexed"] += len(points)
        except Exception as exc:
            logger.warning("qdrant upsert of %d points failed: %s", len(points), exc)
            stats["errors"] += len(points)
        done += len(points)
        texts = []
        metas = []

    for row in _iter_claims(db_path):
        if limit is not None and stats["indexed"] + len(texts) >= limit:
            break
        claim_id, scope, subject, text, status, confidence = row
        embed_text = _claim_text(subject, text)
        if not embed_text:
            stats["skipped"] += 1
            continue
        payload = {
            "id": int(claim_id),
            "scope": scope or "",
            "subject": (subject or "")[:500],
            "status": status or "",
            "confidence": float(confidence or 0.0),
        }
        texts.append(embed_text[:2000])  # cap per claim for embedding speed
        metas.append({"point_id": _point_id(claim_id), "payload": payload})

        if len(texts) >= batch_size:
            _flush()
            now = time.monotonic()
            if now - last_progress > 2.0:
                print(_render_progress(done, total, started_at), flush=True)
                last_progress = now

    _flush()
    print(_render_progress(done, total, started_at), flush=True)
    logger.info(
        "Index complete: indexed=%d skipped=%d errors=%d (total candidates=%d)",
        stats["indexed"],
        stats["skipped"],
        stats["errors"],
        stats["total"],
    )
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="memorymaster.db",
                    help="Path to memorymaster SQLite DB (read-only).")
    ap.add_argument(
        "--qdrant-url",
        default=os.environ.get("MEMORYMASTER_QDRANT_URL") or DEFAULT_QDRANT_URL,
        help="Qdrant REST URL, e.g. http://localhost:6333",
    )
    ap.add_argument(
        "--collection",
        default=os.environ.get("MEMORYMASTER_QDRANT_COLLECTION") or DEFAULT_COLLECTION,
    )
    ap.add_argument(
        "--embed-model",
        default=os.environ.get("MEMORYMASTER_EMBED_MODEL") or DEFAULT_EMBED_MODEL,
    )
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap number of claims indexed (for smoke tests).")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = REPO / db_path
    if not db_path.exists():
        logger.error("DB not found: %s", db_path)
        return 2

    stats = index_claims(
        db_path=db_path,
        qdrant_url=args.qdrant_url,
        collection=args.collection,
        embed_model=args.embed_model,
        batch_size=args.batch_size,
        limit=args.limit,
    )
    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
