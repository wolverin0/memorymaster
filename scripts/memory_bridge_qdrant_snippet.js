/**
 * memory-bridge.cjs — Qdrant upsert step
 *
 * Add this alongside the existing ruflo indexing in your memory-bridge.cjs.
 * For each .md file read from the VM, after chunking by ## headers:
 *
 *   1. Embed via qwen3-embedding:8b (Ollama at 192.168.100.155:11434)
 *   2. Upsert to Qdrant collection "agent-memories" at 192.168.100.186:6333
 *
 * This makes the bridge push to BOTH ruflo (local, fast) and Qdrant (shared network).
 */

const OLLAMA_URL = process.env.OLLAMA_URL || "http://192.168.100.155:11434";
const QDRANT_URL = process.env.QDRANT_URL || "http://192.168.100.186:6333";
const QDRANT_COLLECTION = "agent-memories";
const EMBED_MODEL = "qwen3-embedding:8b";
const EMBED_TIMEOUT_MS = 120_000;

const crypto = require("crypto");

/**
 * Generate a deterministic UUID-v5 from a chunk identifier.
 * Uses the same namespace as the Python QdrantBackend for consistency.
 */
function chunkPointId(filePath, chunkIdx) {
  const seed = `bridge-${filePath}-chunk-${chunkIdx}`;
  return crypto
    .createHash("sha256")
    .update(seed)
    .digest("hex")
    .replace(/^(.{8})(.{4})(.{4})(.{4})(.{12}).*/, "$1-$2-$3-$4-$5");
}

/**
 * Embed text via Ollama qwen3-embedding:8b.
 * Returns a 4096-dim float array or null on failure.
 */
async function embedText(text) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), EMBED_TIMEOUT_MS);
  try {
    const resp = await fetch(`${OLLAMA_URL}/api/embed`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: EMBED_MODEL, input: [text] }),
      signal: controller.signal,
    });
    clearTimeout(timer);
    if (!resp.ok) {
      console.warn(`[bridge-qdrant] Ollama embed HTTP ${resp.status}`);
      return null;
    }
    const data = await resp.json();
    const vectors = data.embeddings || [];
    if (vectors.length > 0 && vectors[0].length === 4096) {
      return vectors[0];
    }
    console.warn(`[bridge-qdrant] unexpected dims: ${vectors[0]?.length}`);
    return null;
  } catch (err) {
    clearTimeout(timer);
    console.warn(`[bridge-qdrant] Ollama embed failed: ${err.message}`);
    return null;
  }
}

/**
 * Upsert a single point to Qdrant.
 */
async function qdrantUpsert(pointId, vector, payload) {
  try {
    const resp = await fetch(
      `${QDRANT_URL}/collections/${QDRANT_COLLECTION}/points`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          points: [{ id: pointId, vector, payload }],
        }),
      }
    );
    if (!resp.ok) {
      console.warn(`[bridge-qdrant] Qdrant upsert HTTP ${resp.status}`);
      return false;
    }
    return true;
  } catch (err) {
    console.warn(`[bridge-qdrant] Qdrant upsert failed: ${err.message}`);
    return false;
  }
}

/**
 * Chunk a markdown file by ## headers and upsert each chunk to Qdrant.
 *
 * Call this from your existing bridge loop, e.g.:
 *
 *   for (const mdFile of vmFiles) {
 *     const content = readFromVM(mdFile);
 *     await indexToRuflo(mdFile, content);         // existing
 *     await indexToQdrant(mdFile.path, content);   // NEW
 *   }
 */
async function indexToQdrant(filePath, markdownContent) {
  const chunks = markdownContent
    .split(/^(?=## )/m)
    .map((c) => c.trim())
    .filter(Boolean);

  let synced = 0;
  let skipped = 0;

  for (let i = 0; i < chunks.length; i++) {
    const chunk = chunks[i];
    const vector = await embedText(chunk);
    if (!vector) {
      skipped++;
      continue;
    }

    const pointId = chunkPointId(filePath, i);
    const headerMatch = chunk.match(/^## (.+)/);
    const payload = {
      claim_id: null,
      subject: headerMatch ? headerMatch[1].trim() : filePath,
      predicate: "documented_in",
      object: filePath,
      claim_text: chunk.slice(0, 2000),
      state: "confirmed",
      confidence: 0.7,
      source: "bridge",
      created_at: new Date().toISOString(),
      workspace: "main",
    };

    const ok = await qdrantUpsert(pointId, vector, payload);
    if (ok) synced++;
    else skipped++;
  }

  console.log(
    `[bridge-qdrant] ${filePath}: ${synced} chunks synced, ${skipped} skipped`
  );
  return { synced, skipped };
}

module.exports = { embedText, qdrantUpsert, indexToQdrant, chunkPointId };
