# Troubleshooting

Start here when MemoryMaster behaves differently on Windows, in Git hooks, or in Codex harness runs.

## Symptoms checklist

- `git checkout` silently exits 1 or switches branches but reports a hook failure.
- Codex headless fails with `windows sandbox: spawn setup refresh`.
- A Codex headless prompt fails because PowerShell rejects `&&` between commands.
- GitNexus reports stale or missing index data after commits.
- Wiki article edits trigger frontmatter validator warnings.

## Windows: `git checkout` exits 1 with no clear reason

Cause: on Windows-default Python installs, the `python3` executable often does not exist even when Python is installed. If `.git/hooks/post-checkout` or `.git/hooks/post-commit` invokes `python3`, the hook can fail and make a normal branch switch look broken.

Fix: patch the local Git hooks to call `python` instead of `python3`. This repo's current local hooks already use `python` for the graphify and GitNexus hook paths.

Hook files are local Git state, not versioned project files. Check them in `.git/hooks/post-checkout` and `.git/hooks/post-commit` when diagnosing a new clone or a different worktree.

Verify:

```powershell
git checkout main
echo $?
```

The final line should print `0`.

MemoryMaster itself supports Python 3.10 and newer. `pyproject.toml` declares `requires-python = ">=3.10"` and Ruff targets `py310`.

## codex headless on Windows: `windows sandbox: spawn setup refresh`

Cause: this is a Windows sandbox failure seen on the `codex:codex-rescue` Agent subagent path. It is not a general failure of the Codex CLI.

Workarounds:

- Wait 5-10 minutes and retry.
- Try `--model gpt-5.3-codex-spark` for a lighter execution path.
- Run Codex from WSL instead of native PowerShell.
- For highest reliability, dispatch via direct `codex exec` from a regular Bash or PowerShell shell. That code path is not affected by the rescue subagent sandbox bug.

## PowerShell does not support `&&`

Cause: legacy Windows PowerShell 5.1 does not support the pipeline chain operator. Commands like this fail before the second command can run:

```powershell
python -m pytest tests/ -q && ruff check memorymaster/
```

Workaround: split the commands, or use PowerShell-native success checks:

```powershell
python -m pytest tests/ -q
if ($?) { ruff check memorymaster/ }
```

Codex headless prompts for this repo should avoid `&&` when targeting native PowerShell.

Prefer separate prompt steps for fetch, checkout, validation, commit, and push. That keeps failures visible in the command that actually failed.

## GitNexus index stale after commits

Cause: GitNexus index data regenerates lazily, so a recent commit can leave the local graph behind the current worktree state.

The post-commit hook may also run analysis in the background. If a later tool or session still reports stale data, refresh the index explicitly.

Fix:

```powershell
npx gitnexus analyze
```

If the existing index includes embeddings, preserve them with:

```powershell
npx gitnexus analyze --embeddings
```

Check `.gitnexus/meta.json` if you need to confirm whether embeddings are present.

## Wiki article frontmatter validator fires warning

Cause: wiki articles are schema-enforced. The validator warns when required fields are missing or malformed.

Required frontmatter fields:

- `title`
- `description` with 50-200 characters
- `type`
- `scope`
- `tags`
- `date`

If the article body is longer than 300 characters, it must also include at least one `[[wikilink]]`.

See `AGENTS.md` Boundaries before changing generated wiki content. Generated Obsidian Bases under `obsidian-vault/bases/*.base` are regenerated automatically by `wiki-absorb`; do not hand-edit them.
