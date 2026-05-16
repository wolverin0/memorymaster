# Daydream Integration

The daydream skill samples an Obsidian vault, synthesizes cross-note insights,
scores them, and writes accepted insight notes to `Daydreams/`.

## Flow

1. Install the daydream skill in Claude Code.
2. Open your Obsidian vault and run `/daydream`.
3. In MemoryMaster, ingest the accepted insights:

```bash
python -m memorymaster --db memorymaster.db ingest-daydream <vault>/Daydreams
```

Each accepted insight becomes one candidate claim:

- `claim_type`: `hypothesis`
- `confidence`: `0.5`
- `source_agent`: `daydream`
- citations: source notes from the insight metadata or frontmatter

The steward validates or decays these hypotheses like any other candidate.
After validation, `wiki-absorb` can compile promoted claims back into the vault.

## Supported Inputs

The installed skill writes Markdown insight notes:

- `Daydreams/YYYYMMDD-slug.md`
- `Daydreams/digests/YYYYMMDD-digest.md`

`ingest-daydream` reads individual insight notes and skips digest files. It also
accepts JSON insight files matching the synth/critic prompt shape, including
`connection`, `synthesis`, `implication`, `suggested_title`, score fields, and
source-note references.

Files that cannot be parsed are skipped and counted in the summary; a bad file
does not stop the run.

## Options

```bash
python -m memorymaster --db memorymaster.db ingest-daydream <vault>/Daydreams \
  --min-score 7.0 \
  --scope user
```

Use `--dry-run` to preview counts without writing claims.
