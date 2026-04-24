"""Wiki-similarity feature for the steward classifier v3 (task #129, v3).

Computes cosine similarity between a claim (text + subject concatenated) and
the best-matching wiki article's *compiled-truth* section — the prose between
the closing frontmatter ``---`` and the first standalone ``---`` divider that
separates it from the append-only timeline.

Two backends:

1. **sentence-transformers** (preferred): if the ``all-MiniLM-L6-v2`` model
   is loadable, we encode claim and article into 384-d vectors.
2. **TF-IDF fallback**: when the model is absent, we use a character-4-gram
   TF-IDF vectoriser on the combined corpus (wiki + seed claim). This is
   deterministic given the corpus and requires no external downloads.

Embeddings are cached on disk at ``artifacts/feature-cache/`` keyed by
``{claim_id}-{content_hash}.npy`` so re-running training / backtest is fast.
Wiki article vectors are cached in-process per call-site — callers should
hold a ``WikiCorpus`` instance across many claim lookups.

Scope of this module: feature extraction only. The module never writes to the
claims DB and never loads the classifier; it is pure compute.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

_LOG = logging.getLogger(__name__)

# Deterministic TF-IDF fallback: character 4-grams + ASCII-lowercase.
# sklearn is an existing dependency; no new install required.
_TFIDF_MIN_DF = 1
_TFIDF_MAX_DF = 1.0
_TFIDF_NGRAM = (3, 5)

# Sentence-transformer model identifier (small, fast, local download).
_ST_MODEL_ID = "all-MiniLM-L6-v2"

# On-disk cache root (relative to repo root).
_DEFAULT_CACHE_DIR = Path("artifacts/feature-cache")
_CACHE_ENV = "MEMORYMASTER_STEWARD_FEATURE_CACHE"

# Wiki root default — resolved relative to the repo root when the caller does
# not provide one. Worktrees (under ``.claude/worktrees/``) defer to the main
# repo's vault path because Obsidian vaults are gitignored and therefore not
# replicated per-worktree.
_DEFAULT_WIKI_ROOT = Path("obsidian-vault/wiki/project-memorymaster")
_WIKI_ROOT_ENV = "MEMORYMASTER_WIKI_ROOT"

# Root that *contains* per-scope dirs (used when scopes="*" or multi-scope).
_WIKI_VAULT_ROOT = Path("obsidian-vault/wiki")

# Matches the very first line starting with three dashes followed by a blank
# line (closing frontmatter), then everything up to the first standalone ---
# at the start of a line (timeline divider). Multiline.
_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)
_TIMELINE_DIVIDER_RE = re.compile(r"\n---\s*\n", re.MULTILINE)

# Token-overlap fallback: strip punctuation, lowercase, unique words.
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{2,}")


@dataclass
class WikiArticle:
    slug: str
    compiled_truth: str
    # Which scope directory this article was loaded from (canonical scope
    # string, e.g. ``"project:memorymaster"``). Set at load time so
    # ``compute_wiki_similarity`` can filter to articles that belong to the
    # claim's own scope even when the corpus aggregates many scopes.
    article_scope: str = ""
    # populated lazily
    embedding: np.ndarray | None = None


@dataclass
class WikiCorpus:
    """In-memory cache of compiled-truth wiki articles, optionally spanning
    multiple scope directories.

    Hold one instance across many claim lookups; article vectors are computed
    once per instance. ``articles`` maps a *fully-qualified* key
    ``"{article_scope}::{slug}"`` -> WikiArticle when the corpus spans more
    than one scope, and a bare ``slug`` -> WikiArticle otherwise (backwards
    compatible with the single-scope case used by v3.0 before the multi-scope
    change in item 11.5).

    ``scope`` stays present for backwards compatibility: it is the *primary*
    scope (first entry of ``scopes``). ``scopes`` is the full list of scopes
    that contributed articles.

    ``tfidf_matrix`` and ``tfidf_vectorizer`` are set lazily when the TF-IDF
    fallback is active.
    """

    scope: str
    articles: dict[str, WikiArticle] = field(default_factory=dict)
    embedding_backend: str = "none"  # "sentence-transformers" | "tfidf" | "none"
    scopes: tuple[str, ...] = ()
    _tfidf_matrix: Any | None = None
    _tfidf_vectorizer: Any | None = None
    _st_model: Any | None = None

    def is_empty(self) -> bool:
        return not self.articles

    def iter_articles_for_scope(self, scope: str | None) -> list[WikiArticle]:
        """Return the list of loaded articles whose ``article_scope`` matches
        ``scope``. If ``scope`` is falsy, return every article (callers can
        opt out of scope filtering for test fixtures / global lookups)."""
        if not scope:
            return list(self.articles.values())
        return [a for a in self.articles.values() if a.article_scope == scope]


# ---------------------------------------------------------------------------
# Article loading
# ---------------------------------------------------------------------------


def _strip_frontmatter_and_timeline(markdown: str) -> str:
    """Return the body between the closing frontmatter and the first timeline
    divider. Falls back to whole body if neither marker is found."""
    if not markdown:
        return ""
    fm = _FRONTMATTER_RE.match(markdown)
    body = markdown[fm.end():] if fm else markdown
    # first standalone --- (timeline divider) or EOF
    split = _TIMELINE_DIVIDER_RE.split(body, maxsplit=1)
    compiled = split[0] if split else body
    return compiled.strip()


_SCOPE_DIRNAME_SAFE_RE = re.compile(r"[^a-z0-9\-_]+")


def _scope_to_dirname(scope: str) -> str:
    """Mirror ``memorymaster.wiki_engine._scope_dirname`` locally to avoid a
    cross-module import at load time. ``project:memorymaster`` ->
    ``project-memorymaster``; ``user`` -> ``user``; ``global`` -> ``global``.
    Keeps the first two ``:``-split tokens so ``project:x:y`` becomes
    ``project-x``; the rest is discarded — matching ``wiki_engine``."""
    if not scope:
        return "default"
    parts = scope.split(":")
    name = "-".join(parts[:2]) if len(parts) >= 2 else parts[0]
    clean = _SCOPE_DIRNAME_SAFE_RE.sub("-", name.lower()).strip("-")
    return clean or "default"


def _resolve_wiki_root(override: Path | None, repo_root: Path | None) -> Path:
    if override is not None:
        return override
    env = os.environ.get(_WIKI_ROOT_ENV)
    if env:
        return Path(env)
    # Worktrees store code only; vaults live at the main repo root. Walk up
    # from the worktree to find a sibling ``obsidian-vault`` dir.
    if repo_root is not None:
        candidate = repo_root / _DEFAULT_WIKI_ROOT
        if candidate.exists():
            return candidate
        # Probe the main repo by walking out of ``.claude/worktrees/...``.
        parts = repo_root.resolve().parts
        for i in range(len(parts) - 1, 0, -1):
            if parts[i] == "worktrees" and i > 0 and parts[i - 1] == ".claude":
                main_repo = Path(*parts[: i - 1])
                main_candidate = main_repo / _DEFAULT_WIKI_ROOT
                if main_candidate.exists():
                    return main_candidate
                break
    return _DEFAULT_WIKI_ROOT


def _resolve_wiki_vault_root(
    override: Path | None, repo_root: Path | None
) -> Path:
    """Resolve the *parent* dir that contains per-scope dirs (one level up
    from the default single-scope wiki root). Used when ``scopes="*"`` walks
    every scope dir."""
    if override is not None:
        return override
    env = os.environ.get(_WIKI_ROOT_ENV)
    if env:
        return Path(env)
    if repo_root is not None:
        candidate = repo_root / _WIKI_VAULT_ROOT
        if candidate.exists():
            return candidate
        parts = repo_root.resolve().parts
        for i in range(len(parts) - 1, 0, -1):
            if parts[i] == "worktrees" and i > 0 and parts[i - 1] == ".claude":
                main_repo = Path(*parts[: i - 1])
                main_candidate = main_repo / _WIKI_VAULT_ROOT
                if main_candidate.exists():
                    return main_candidate
                break
    return _WIKI_VAULT_ROOT


def _discover_scope_dirs(vault_root: Path) -> dict[str, Path]:
    """Given the obsidian-vault/wiki root, return ``{scope_string: dir}`` for
    every child directory that looks like a scope (skips ``_``-prefixed and
    the ``bases``/``entities`` helper dirs). Scope strings are best-effort
    reconstructions: ``project-xyz`` -> ``project:xyz``; anything else keeps
    its literal name (``global`` stays ``global``, ``user`` stays ``user``).
    """
    out: dict[str, Path] = {}
    if not vault_root.exists() or not vault_root.is_dir():
        return out
    # Skip vault-helper dirs that are NOT scope dirs: ``bases`` (Obsidian
    # Bases .base files), ``entities`` (entity registry exports),
    # ``sources`` (ingest-source blobs), ``raw`` (clipper staging),
    # and the bare ``project`` fallback dir that callers use when the
    # ingester failed to set a proper ``project:<slug>`` scope (audit
    # flag — never a real project).
    _RESERVED = {"bases", "entities", "raw", "sources", "project"}
    for child in sorted(vault_root.iterdir()):
        if not child.is_dir():
            continue
        name = child.name
        if name.startswith("_") or name in _RESERVED:
            continue
        if name.startswith("project-"):
            scope = "project:" + name[len("project-"):]
        elif name.startswith("team-"):
            scope = "team:" + name[len("team-"):]
        else:
            scope = name  # ``global`` / ``user`` / etc.
        out[scope] = child
    return out


def _load_articles_from_dir(
    corpus: WikiCorpus, scope: str, root: Path, *, key_prefix: bool
) -> int:
    """Append articles found under ``root`` into ``corpus.articles``. Returns
    the number of articles added. ``key_prefix=True`` disambiguates multi-
    scope corpora by keying articles as ``"{scope}::{slug}"`` so two scopes
    can ship an article with the same slug (e.g. ``general.md``) without
    clobbering each other."""
    if not root.exists() or not root.is_dir():
        _LOG.info("wiki_similarity: wiki scope dir not found at %s — skipping", root)
        return 0
    added = 0
    for path in sorted(root.glob("*.md")):
        if path.name.startswith("_"):
            continue  # skip _index.md etc.
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            _LOG.warning("wiki_similarity: failed to read %s: %s", path, exc)
            continue
        body = _strip_frontmatter_and_timeline(raw)
        if not body:
            continue
        slug = path.stem
        key = f"{scope}::{slug}" if key_prefix else slug
        # Preserve the bare slug for single-scope corpora so callers that
        # look up ``corpus.articles[slug]`` (e.g. via claim.wiki_article)
        # still hit; for multi-scope corpora callers must go through the
        # scope-filtered helper ``iter_articles_for_scope``.
        corpus.articles[key] = WikiArticle(
            slug=slug, compiled_truth=body, article_scope=scope,
        )
        added += 1
    return added


def load_wiki_corpus(
    scope: str = "project:memorymaster",
    *,
    scopes: list[str] | tuple[str, ...] | str | None = None,
    wiki_root: Path | None = None,
    repo_root: Path | None = None,
    prefer_embeddings: bool = True,
) -> WikiCorpus:
    """Load every ``*.md`` article in the requested scope directory (or
    directories) into a ``WikiCorpus`` with compiled-truth bodies extracted.

    * ``scope`` (legacy, default ``project:memorymaster``): single-scope
      behaviour. Used when ``scopes`` is ``None``.
    * ``scopes``: either a list/tuple of scope strings, or the literal
      ``"*"`` meaning "auto-discover every scope dir under the vault root".
      When provided, ``scope`` becomes the *primary* scope (for the
      ``WikiCorpus.scope`` field and any legacy single-scope consumers).

    Does not raise on missing dirs — returns an empty corpus with
    ``embedding_backend = 'none'`` so feature extraction falls back to 0.0.
    """
    corpus = WikiCorpus(scope=scope)

    # Resolve which scope dirs to scan.
    scope_dirs: dict[str, Path] = {}
    multi_scope = False

    if scopes is None:
        root = _resolve_wiki_root(wiki_root, repo_root)
        scope_dirs = {scope: root}
    elif isinstance(scopes, str) and scopes == "*":
        vault_root = _resolve_wiki_vault_root(wiki_root, repo_root)
        scope_dirs = _discover_scope_dirs(vault_root)
        multi_scope = len(scope_dirs) > 1
    elif isinstance(scopes, (list, tuple)):
        # Caller passed explicit scope strings; resolve each to its dir under
        # the vault root. Use ``wiki_root`` (if provided) as the vault root so
        # tests can point at a tmp_path that holds ``project-a/`` etc.
        vault_root = _resolve_wiki_vault_root(wiki_root, repo_root)
        for s in scopes:
            if not s:
                continue
            scope_dirs[s] = vault_root / _scope_to_dirname(s)
        multi_scope = len(scope_dirs) > 1
    else:
        raise TypeError(f"scopes must be None, '*', or a list/tuple — got {type(scopes).__name__}")

    corpus.scopes = tuple(scope_dirs.keys())
    for s, d in scope_dirs.items():
        _load_articles_from_dir(corpus, s, d, key_prefix=multi_scope)

    if not corpus.articles:
        return corpus

    # Initialize the embedding backend now so every claim lookup reuses it.
    if prefer_embeddings:
        model = _try_load_sentence_transformer()
        if model is not None:
            corpus._st_model = model
            corpus.embedding_backend = "sentence-transformers"
            texts = [a.compiled_truth for a in corpus.articles.values()]
            try:
                vecs = model.encode(
                    texts, convert_to_numpy=True, normalize_embeddings=True,
                    show_progress_bar=False,
                )
            except Exception as exc:  # noqa: BLE001
                _LOG.warning(
                    "wiki_similarity: sentence-transformers encode failed: %s — falling back",
                    exc,
                )
            else:
                for article, vec in zip(corpus.articles.values(), vecs):
                    article.embedding = vec.astype(np.float32)
                return corpus

    # TF-IDF fallback (deterministic, no downloads).
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
    except ImportError:
        _LOG.warning("wiki_similarity: sklearn missing — similarity disabled")
        corpus.embedding_backend = "none"
        return corpus

    vectorizer = TfidfVectorizer(
        analyzer="char_wb",
        ngram_range=_TFIDF_NGRAM,
        min_df=_TFIDF_MIN_DF,
        max_df=_TFIDF_MAX_DF,
        lowercase=True,
        strip_accents="unicode",
    )
    texts = [a.compiled_truth for a in corpus.articles.values()]
    try:
        matrix = vectorizer.fit_transform(texts)
    except ValueError as exc:
        _LOG.warning("wiki_similarity: TF-IDF fit failed: %s", exc)
        corpus.embedding_backend = "none"
        return corpus

    corpus._tfidf_vectorizer = vectorizer
    corpus._tfidf_matrix = matrix
    corpus.embedding_backend = "tfidf"
    return corpus


def _try_load_sentence_transformer() -> Any | None:
    try:
        # Env escape hatch for CI environments that ship without torch.
        if os.environ.get("MEMORYMASTER_DISABLE_ST", "").strip() in ("1", "true", "yes"):
            return None
        from sentence_transformers import SentenceTransformer
    except ImportError:
        return None
    except Exception as exc:  # noqa: BLE001
        _LOG.info("wiki_similarity: sentence-transformers import failed: %s", exc)
        return None
    try:
        return SentenceTransformer(_ST_MODEL_ID)
    except Exception as exc:  # noqa: BLE001
        _LOG.info("wiki_similarity: loading %s failed: %s", _ST_MODEL_ID, exc)
        return None


# ---------------------------------------------------------------------------
# Similarity
# ---------------------------------------------------------------------------


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.clip(np.dot(a, b) / (na * nb), 0.0, 1.0))


def _claim_content(claim: dict[str, Any]) -> str:
    parts: list[str] = []
    subject = claim.get("subject")
    if subject:
        parts.append(str(subject))
    text = claim.get("text")
    if text:
        parts.append(str(text))
    return "\n".join(parts).strip()


def _content_hash(content: str) -> str:
    return hashlib.sha1(content.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _cache_path(cache_dir: Path, claim_id: int, chash: str) -> Path:
    return cache_dir / f"{claim_id}-{chash}.npy"


def _resolve_cache_dir(override: Path | None) -> Path | None:
    if override is not None:
        return override
    env = os.environ.get(_CACHE_ENV)
    if env == "off":
        return None
    if env:
        return Path(env)
    return _DEFAULT_CACHE_DIR


def _token_overlap_best_key(
    content: str, corpus: WikiCorpus, *, scope: str | None = None
) -> str | None:
    """When the claim has no wiki_article column, fall back to a cheap
    lexical match — article whose compiled-truth shares the most unique
    tokens with the claim's content. Ties broken by key alphabetical.

    When ``scope`` is provided, only articles whose ``article_scope``
    matches are considered. Returns the corpus key (either bare ``slug`` or
    ``"{scope}::{slug}"`` depending on how the corpus was loaded)."""
    if corpus.is_empty():
        return None
    query_tokens = {w.lower() for w in _WORD_RE.findall(content)}
    if not query_tokens:
        return None
    best_key: str | None = None
    best_score = -1
    for key, article in corpus.articles.items():
        if scope and article.article_scope and article.article_scope != scope:
            continue
        article_tokens = {w.lower() for w in _WORD_RE.findall(article.compiled_truth)}
        if not article_tokens:
            continue
        score = len(query_tokens & article_tokens)
        if score > best_score or (score == best_score and best_key is not None and key < best_key):
            best_key = key
            best_score = score
    return best_key if best_score > 0 else None


def _resolve_article_key(
    corpus: WikiCorpus, slug: str, *, scope: str | None
) -> str | None:
    """Map a claim-supplied ``slug`` to the real corpus key. For single-
    scope corpora the key equals the slug. For multi-scope corpora the key
    is ``"{scope}::{slug}"``; when the claim provides a scope we require
    that the article belongs to it (cross-scope explicit slugs return None
    so the feature falls back to 0)."""
    if slug in corpus.articles:
        article = corpus.articles[slug]
        if scope and article.article_scope and article.article_scope != scope:
            return None
        return slug
    if scope:
        prefixed = f"{scope}::{slug}"
        if prefixed in corpus.articles:
            return prefixed
    return None


def _encode_claim(
    content: str, corpus: WikiCorpus
) -> np.ndarray | None:
    if corpus.embedding_backend == "sentence-transformers":
        model = corpus._st_model
        if model is None:
            return None
        try:
            vec = model.encode(
                [content], convert_to_numpy=True, normalize_embeddings=True,
                show_progress_bar=False,
            )[0]
            return vec.astype(np.float32)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("wiki_similarity: claim encode failed: %s", exc)
            return None
    if corpus.embedding_backend == "tfidf":
        vectorizer = corpus._tfidf_vectorizer
        if vectorizer is None:
            return None
        try:
            vec = vectorizer.transform([content])
            arr = vec.toarray()[0].astype(np.float32)
            # Normalize to unit length so _cosine == dot product.
            n = float(np.linalg.norm(arr))
            if n > 0.0:
                arr = arr / n
            return arr
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("wiki_similarity: claim TF-IDF transform failed: %s", exc)
            return None
    return None


def compute_wiki_similarity(
    claim: dict[str, Any],
    corpus: WikiCorpus,
    *,
    cache_dir: Path | None = None,
) -> float:
    """Return cosine similarity in [0, 1] between ``claim`` and the best
    matching wiki article in ``corpus``. Returns 0.0 when the corpus is empty,
    the claim has no text+subject, or any backend fails.

    Scope filtering (item 11.5): when the claim carries a ``scope`` key AND
    the corpus is multi-scope, only articles whose ``article_scope`` matches
    the claim's scope are considered. This prevents e.g. a ``project:X``
    claim from being scored against a ``project:Y`` wiki article. For
    single-scope corpora loaded the legacy way (``scopes=None``) the filter
    is a no-op because every article shares one scope anyway."""
    if corpus.is_empty() or corpus.embedding_backend == "none":
        return 0.0
    content = _claim_content(claim)
    if not content:
        return 0.0

    # Claim scope gates which articles are eligible. For single-scope corpora
    # (legacy load_wiki_corpus(scope=...) without ``scopes``) we intentionally
    # skip the filter so test fixtures and the existing v3.0 behaviour are
    # untouched — ``multi_scope`` flips the gate on.
    claim_scope = (claim.get("scope") or "").strip() or None
    multi_scope = len(corpus.scopes) > 1
    filter_scope = claim_scope if multi_scope else None

    # Pick candidate key: explicit column takes precedence, else lexical match.
    raw_slug = (claim.get("wiki_article") or "").strip() or None
    key: str | None = None
    if raw_slug:
        key = _resolve_article_key(corpus, raw_slug, scope=filter_scope)
    if key is None:
        key = _token_overlap_best_key(content, corpus, scope=filter_scope)
    if key is None or key not in corpus.articles:
        # explicit slug missing from corpus (e.g., different scope) — silent 0.
        return 0.0

    article = corpus.articles[key]

    # Embedding cache — keyed by claim id + content hash so ingest edits
    # invalidate the cache automatically.
    claim_id = int(claim.get("id") or 0)
    chash = _content_hash(f"{content}|{corpus.embedding_backend}|{key}")
    resolved_cache = _resolve_cache_dir(cache_dir)
    cached_sim: float | None = None
    cache_path: Path | None = None
    if claim_id and resolved_cache is not None:
        resolved_cache.mkdir(parents=True, exist_ok=True)
        cache_path = _cache_path(resolved_cache, claim_id, chash)
        if cache_path.exists():
            try:
                cached = np.load(cache_path, allow_pickle=False)
                if cached.shape == ():  # stored as scalar
                    cached_sim = float(cached)
                elif cached.size == 1:
                    cached_sim = float(cached.reshape(-1)[0])
            except Exception as exc:  # noqa: BLE001
                _LOG.info("wiki_similarity: cache read failed for %s: %s", cache_path, exc)

    if cached_sim is not None:
        return float(np.clip(cached_sim, 0.0, 1.0))

    # Encode the claim if we don't already have the similarity cached.
    claim_vec = _encode_claim(content, corpus)
    article_vec = article.embedding
    if corpus.embedding_backend == "tfidf":
        # Article embeddings are rows of the TF-IDF matrix; materialize on demand.
        if article.embedding is None and corpus._tfidf_matrix is not None:
            idx = list(corpus.articles.keys()).index(key)
            arr = corpus._tfidf_matrix[idx].toarray()[0].astype(np.float32)
            n = float(np.linalg.norm(arr))
            if n > 0.0:
                arr = arr / n
            article.embedding = arr
        article_vec = article.embedding

    if claim_vec is None or article_vec is None:
        return 0.0

    sim = _cosine(claim_vec, article_vec)

    if cache_path is not None:
        try:
            np.save(cache_path, np.asarray(sim, dtype=np.float32), allow_pickle=False)
        except OSError as exc:
            _LOG.info("wiki_similarity: cache write failed for %s: %s", cache_path, exc)

    return sim
