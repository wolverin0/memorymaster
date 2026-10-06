"""Resident dense-recall service: EmbeddingGemma 2 over confirmed claims, ids and scores only.

Runs in its own runtime (sentence-transformers >= 6.1, CUDA torch, torchvision,
pillow), never in the hook process: loading the model costs ~5.5 s and ~1.3 GB.
The index is a derived view. It reads the authoritative SQLite read-only, keeps
one vector per confirmed claim, and answers ``POST /search`` with candidate ids.
Callers rehydrate and authorize those ids through the governed store, so a
stale or wrong vector can propose a claim but never expose one.

Measured 2026-10-06 (200 real prompts, blind graded judgments): with the prompt
hook's scope, nDCG@10 0.337 against 0.167 for the lexical hook; see
artifacts/2026-10-06-embeddinggemma2-benchmark.html.

Deployment notes measured the same day: run it on CPU (``--device cpu``); with
prompts seconds apart an idle desktop GPU stays in its P8 power state and
answered in 80-322 ms against 54-109 ms on CPU. Under Task Scheduler give the
task normal priority (4): the default (7, below normal) tripled CPU latency.

    python -m memorymaster.recall.dense_server --db memorymaster.db --port 8767 --device cpu
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import numpy as np

LOGGER = logging.getLogger("memorymaster.dense_server")

MODEL_NAME = "google/embeddinggemma-2"
DEFAULT_PORT = 8767
DEFAULT_DIM = 768
# Long claims beyond this are truncated; it also bounds VRAM per batch
# (unbounded sequences peaked at 5.9 GB with batch 32 in the benchmark).
MAX_SEQ_TOKENS = 1024
EMBED_BATCH = 8

Encoder = Callable[[Sequence[str], str], np.ndarray]


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def read_confirmed_claims(db_path: str | Path) -> list[tuple[int, str, str]]:
    """``(id, scope, text)`` of every confirmed claim, read-only."""
    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    try:
        return [
            (int(row[0]), str(row[1] or ""), str(row[2] or ""))
            for row in conn.execute("SELECT id, scope, text FROM claims WHERE status = 'confirmed'")
        ]
    finally:
        conn.close()


class DenseIndex:
    """One normalized vector per confirmed claim; re-embeds only new or changed text."""

    def __init__(self, encode: Encoder, *, cache_path: Path | None = None) -> None:
        self._encode = encode
        self._cache_path = cache_path
        self._lock = threading.Lock()
        self.ids = np.zeros(0, dtype=np.int64)
        self.scopes: list[str] = []
        self.hashes: list[str] = []
        self.vectors = np.zeros((0, 0), dtype=np.float32)
        self.last_sync: float | None = None
        self.last_sync_embedded = 0
        self._load_cache()

    def _load_cache(self) -> None:
        if self._cache_path is None or not self._cache_path.exists():
            return
        try:
            data = np.load(self._cache_path, allow_pickle=False)
            self.ids = data["ids"].astype(np.int64)
            self.hashes = [str(h) for h in data["hashes"]]
            self.scopes = [str(s) for s in data["scopes"]]
            self.vectors = data["vectors"].astype(np.float32)
        except Exception as exc:  # a bad cache only costs a full re-embed
            LOGGER.warning("dense index cache ignored: %s", type(exc).__name__)

    def _save_cache(self) -> None:
        if self._cache_path is None:
            return
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._cache_path.with_suffix(".tmp.npz")
        np.savez(tmp, ids=self.ids, hashes=np.array(self.hashes), scopes=np.array(self.scopes),
                 vectors=self.vectors)
        os.replace(tmp, self._cache_path)

    def sync(self, claims: Sequence[tuple[int, str, str]]) -> int:
        """Make the index match ``claims``; returns how many texts were embedded."""
        known = {int(cid): (h, row) for row, (cid, h) in enumerate(zip(self.ids.tolist(), self.hashes))}
        keep_rows: list[int] = []
        new_ids: list[int] = []
        new_scopes: list[str] = []
        new_hashes: list[str] = []
        new_texts: list[str] = []
        ids: list[int] = []
        scopes: list[str] = []
        hashes: list[str] = []
        for cid, scope, text in claims:
            digest = _text_hash(text)
            previous = known.get(cid)
            if previous is not None and previous[0] == digest:
                keep_rows.append(previous[1])
                ids.append(cid)
                scopes.append(scope)
                hashes.append(digest)
            else:
                new_ids.append(cid)
                new_scopes.append(scope)
                new_hashes.append(digest)
                new_texts.append(text)
        # Small chunks: the encoder lock is released between them, so a burst of
        # newly confirmed claims (CPU: ~11 claims/s) never holds a query past its timeout.
        chunks = [self._encode(new_texts[i:i + EMBED_BATCH], "document") for i in range(0, len(new_texts), EMBED_BATCH)]
        fresh = np.vstack(chunks) if chunks else None
        kept = self.vectors[keep_rows] if keep_rows else None
        parts = [part for part in (kept, fresh) if part is not None and len(part)]
        vectors = np.vstack(parts).astype(np.float32) if parts else np.zeros((0, 0), dtype=np.float32)
        with self._lock:
            self.ids = np.array(ids + new_ids, dtype=np.int64)
            self.scopes = scopes + new_scopes
            self.hashes = hashes + new_hashes
            self.vectors = vectors
            self.last_sync = time.time()
            self.last_sync_embedded = len(new_texts)
        if new_texts or len(keep_rows) != len(known):
            self._save_cache()
        return len(new_texts)

    def search(self, query_vector: np.ndarray, *, scopes: Sequence[str] | None, k: int) -> list[tuple[int, float]]:
        with self._lock:
            ids, vectors, claim_scopes = self.ids, self.vectors, self.scopes
        if not len(ids):
            return []
        scores = vectors @ query_vector.astype(np.float32)
        if scopes:
            allowed = set(scopes)
            mask = np.fromiter((scope in allowed for scope in claim_scopes), dtype=bool, count=len(claim_scopes))
            scores = np.where(mask, scores, -np.inf)
        top = np.argsort(-scores)[: max(0, k)]
        return [(int(ids[i]), float(scores[i])) for i in top if np.isfinite(scores[i])]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {"indexed": int(len(self.ids)), "dim": int(self.vectors.shape[1]) if self.vectors.size else 0,
                    "last_sync": self.last_sync, "last_sync_embedded": self.last_sync_embedded}


def load_encoder(dim: int, device: str | None = None) -> tuple[Encoder, str]:
    """EmbeddingGemma 2 text-only encoder with the model card's retrieval prompts."""
    import torch
    from sentence_transformers import SentenceTransformer

    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    # The card forbids float16 (silent NaN); bf16 on GPU, fp32 on CPU.
    dtype = torch.bfloat16 if device == "cuda" else torch.float32
    model = SentenceTransformer(MODEL_NAME, device=device, model_kwargs={"torch_dtype": dtype},
                                config_kwargs={"vision_config": None, "audio_config": None})
    model.max_seq_length = MAX_SEQ_TOKENS
    lock = threading.Lock()

    def encode(texts: Sequence[str], kind: str) -> np.ndarray:
        prompt = "SearchQuery" if kind == "query" else "Document"
        with lock:
            vectors = model.encode(list(texts), prompt_name=prompt, batch_size=EMBED_BATCH,
                                   truncate_dim=dim if dim != DEFAULT_DIM else None,
                                   normalize_embeddings=True, convert_to_numpy=True)
        return np.asarray(vectors, dtype=np.float32)

    return encode, device


class DenseService:
    def __init__(self, db_path: str | Path, encode: Encoder, *, model: str, dim: int, device: str,
                 cache_path: Path | None = None, sync_interval_s: float = 60.0) -> None:
        self.db_path = str(db_path)
        self.encode = encode
        self.model = model
        self.dim = dim
        self.device = device
        self.index = DenseIndex(encode, cache_path=cache_path)
        self.sync_interval_s = sync_interval_s
        self.started = time.time()
        self.sync_errors = 0
        self._stop = threading.Event()

    def sync_once(self) -> int:
        return self.index.sync(read_confirmed_claims(self.db_path))

    def _sync_loop(self) -> None:
        while not self._stop.is_set():
            try:
                embedded = self.sync_once()
                if embedded:
                    LOGGER.info("dense index synced: %d embedded, %d indexed", embedded, self.index.stats()["indexed"])
            except Exception as exc:  # the index keeps serving its last good state
                self.sync_errors += 1
                LOGGER.warning("dense index sync failed: %s", exc)
            self._stop.wait(self.sync_interval_s)

    def start_sync(self) -> threading.Thread:
        thread = threading.Thread(target=self._sync_loop, name="dense-sync", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()

    def search(self, query: str, scopes: Sequence[str] | None, k: int) -> dict[str, Any]:
        started = time.perf_counter()
        vector = self.encode([query], "query")[0]
        embedded = time.perf_counter()
        results = self.index.search(vector, scopes=scopes, k=k)
        done = time.perf_counter()
        return {"results": [{"id": cid, "score": round(score, 5)} for cid, score in results],
                "embed_ms": round((embedded - started) * 1000, 2), "search_ms": round((done - embedded) * 1000, 2),
                "indexed": self.index.stats()["indexed"], "model": self.model, "dim": self.dim}

    def health(self) -> dict[str, Any]:
        return {"ok": True, "model": self.model, "dim": self.dim, "device": self.device,
                "uptime_s": round(time.time() - self.started), "sync_errors": self.sync_errors, **self.index.stats()}


def make_handler(service: DenseService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._send(200, service.health())
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/search":
                self._send(404, {"error": "not found"})
                return
            try:
                length = min(int(self.headers.get("Content-Length") or 0), 1_000_000)
                request = json.loads(self.rfile.read(length) or b"{}")
                query = str(request.get("query") or "")
                scopes = request.get("scopes")
                if scopes is not None and not (isinstance(scopes, list) and all(isinstance(s, str) for s in scopes)):
                    raise ValueError("scopes must be a list of strings")
                k = max(1, min(int(request.get("k") or 10), 100))
                if not query.strip():
                    raise ValueError("query is required")
            except (ValueError, TypeError) as exc:
                self._send(400, {"error": str(exc)})
                return
            try:
                self._send(200, service.search(query, scopes, k))
            except Exception as exc:  # noqa: BLE001 — the client falls back to lexical recall
                LOGGER.warning("dense search failed: %s", type(exc).__name__)
                self._send(500, {"error": type(exc).__name__})

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — quiet access log
            return

    return Handler


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="MemoryMaster dense-recall service (EmbeddingGemma 2)")
    parser.add_argument("--db", default=os.environ.get("MEMORYMASTER_DEFAULT_DB", "memorymaster.db"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--dim", type=int, default=DEFAULT_DIM, choices=(128, 256, 512, 768))
    parser.add_argument("--device", default=None)
    parser.add_argument("--cache", default=str(Path.home() / ".memorymaster" / "dense" / "eg2-index.npz"))
    parser.add_argument("--sync-interval", type=float, default=60.0)
    parser.add_argument("--log-file", default=str(Path.home() / ".memorymaster" / "logs" / "dense-recall.log"))
    args = parser.parse_args(argv)
    Path(args.log_file).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=args.log_file, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    encode, device = load_encoder(args.dim, args.device)
    cache = Path(args.cache)
    if args.dim != DEFAULT_DIM:
        cache = cache.with_name(f"{cache.stem}-{args.dim}{cache.suffix}")
    service = DenseService(args.db, encode, model=MODEL_NAME, dim=args.dim, device=device, cache_path=cache,
                           sync_interval_s=args.sync_interval)
    embedded = service.sync_once()
    LOGGER.info("dense service ready on %s:%d (%s, %d indexed, %d embedded at start)",
                args.host, args.port, device, service.index.stats()["indexed"], embedded)
    service.start_sync()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(service))
    try:
        server.serve_forever()
    finally:
        service.stop()


if __name__ == "__main__":
    main()
