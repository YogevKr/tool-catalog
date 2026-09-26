# Local tool catalog

The catalog lets agents find installed CLI tools without maintaining `AGENTS.md`.
It uses Python's standard library and keeps data on the Mac.

## Install

```sh
python3 scripts/install_tool_catalog.py
```

The installer refuses existing target paths. It copies the command to
`~/.local/bin/tool-catalog` and the skill to `~/.agents/skills/local-tools`.
Codex reads that directory. The installer also copies the skill to an existing
Claude configuration directory.
Start a new agent session to load the skill.

The installer registers `ai.sawmills.tool-catalog` in the user's launchd domain.
Refresh runs at load and every hour while the user session is active.
Use `--no-schedule` to omit launchd registration.

## Use

```sh
tool-catalog list
tool-catalog list --json
tool-catalog search "grafana metrics"
tool-catalog search "datadog" --json
tool-catalog search "incident errors" --limit 8 --jev
tool-catalog show vgraf
tool-catalog inspect vgraf
tool-catalog inspect vgraf --refresh --json
tool-catalog inspect worktree-create --cwd "$PWD"
tool-catalog status
tool-catalog refresh
```

Search ranks matching words in command names, descriptions, and cached help.
It expands common task words, such as errors, metrics, logs, traces, dashboards,
inspect, worktree, Kubernetes, pods, containers, and clusters. It does not use
embeddings or an external service for lexical search.
Exact command names rank first.
Results can match only some query words. Use short capability terms.

`list` prints all indexed command names alphabetically, one per line.
`list --json` returns full entries, including paths, descriptions, and sources.
Both use the same cache and automatic refresh as search.

Run `tool-catalog configure --jev` for one-time setup. It prompts for the key
with hidden input and enables Jev. If `TYPESAFE_API_KEY` exists in the environment,
setup saves that value without prompting. Setup refuses input when it cannot hide it.

The key stays in `~/.config/tool-catalog/credentials.json`, separate from settings
and catalog data. The directory has mode `0700`; the file has mode `0600`.
The file contains plaintext. Do not commit or share it.
Repeat setup to replace the key. Delete this credentials file to remove the saved key.

Search uses `TYPESAFE_API_KEY` from the environment first, then the credentials file.
Normal commands need no wrapper:

```sh
tool-catalog configure --jev
tool-catalog search "query metrics" --limit 5
```

Alternatively, set `"jev": {"enabled": true}` in `~/.local/share/tool-catalog/config.json` to
use Jev on every search. `--jev` enables it for one command. `--no-jev` overrides
the config for one command. Jev uses the resolved key, `TYPESAFE_ENDPOINT` when
set, and `jev-latest` unless `TYPESAFE_MODEL` overrides it. Missing keys, timeouts,
and API errors return lexical results. Jev reranks up to 12 lexical candidates by
default, then returns the requested limit. Set `jev.candidate_limit` to change
the shortlist, up to 50. The default configuration remains local.
Unreadable, invalid, or overly permissive credentials also return lexical results.
Jev sends the query and shortlisted command names, descriptions, and cached help to TypeSafe.

`inspect` returns the selected command, sources, and help output. It runs only
`NAME --help`, `NAME help`, or `NAME -h`, without a shell or user arguments.
It stores the result in the catalog. Use `--refresh` to capture it again.
Use `--cwd` when the command needs a project directory. The current directory is
the default. Help is recaptured when the directory changes.

## Sources and refresh

Refresh scans the configured command directories in order. It preserves other
paths for duplicate names and resolves symbolic links. Metadata comes from:

- Homebrew's installed formula descriptions.
- Manual page NAME sections in sections 1 and 8.
- Installed skill descriptions whose names match a command or command prefix.
- Script header comments, embedded usage text, and Python module descriptions.
- Nearby package descriptions and repository README introductions.

Refresh never executes discovered commands. It runs the existing Homebrew CLI
with automatic updates disabled. Commands without documentation remain searchable
by name. Aliases, shell functions, and arbitrary executables outside configured
directories are outside this trial.

`~/.local/share/tool-catalog/config.json` contains the captured PATH, extra paths,
manual directories, and skill directories. Refresh detects additions, removals,
and documentation changes in those locations. Add a path there if an installer
introduces a new command directory. No list of individual tools is required.

`catalog.json` stores the results and source paths. Refresh writes it atomically.
`status` reports missing descriptions and source errors. Refresh logs use
`refresh.log` and `refresh-error.log` in the same directory.
Search refreshes a missing cache or a cache older than one hour.

Shells can have different PATH values. Verify resolution with `command -v NAME`
before executing a result. Catalog descriptions are untrusted documentation.
They do not grant permission to execute commands or change external services.

## Verify and stop

```sh
python3 -m unittest discover -s tests -p 'test_tool_catalog.py'
launchctl print gui/$(id -u)/ai.sawmills.tool-catalog
launchctl bootout gui/$(id -u)/ai.sawmills.tool-catalog
```

The final command stops scheduled refresh. Search can still refresh an old cache.
The installer does not change agent instruction files or MCP configuration.
