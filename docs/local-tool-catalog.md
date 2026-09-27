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

Human search output shows command names, Jev scores when available, and short descriptions.
Rows fit the terminal width. Narrow terminals use stacked rows.
Use `show NAME` for full descriptions and executable paths, or `--json` for structured results.

`list` prints all indexed command and MCP server names alphabetically, one per line.
MCP servers use the `mcp:NAME` selector and have a `[mcp]` suffix.
Aliases and functions use `alias:NAME` and have an `[alias]` suffix.
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

## Shell aliases and functions

Add an `aliases` list to the existing `~/.local/share/tool-catalog/config.json`.
Keep the other configuration fields. For example:

```json
{
  "aliases": [
    {
      "name": "co",
      "description": "Preferred launcher for Codex.",
      "shell": "zsh",
      "source": "~/.zshrc",
      "command": "codex",
      "preferred_for": ["codex"]
    }
  ]
}
```

Each entry requires a name and description. The shell, source, and command fields are optional text.
The command field documents the expansion. The catalog does not execute it.
Use the same format for shell functions. Do not store secrets or function bodies in these fields.
Names are case-sensitive. Duplicate names keep the first valid entry and produce a warning.
Invalid entries produce warnings in `status` without removing valid entries.

```sh
tool-catalog refresh
tool-catalog search "codex" --no-jev
tool-catalog show alias:co
tool-catalog inspect alias:co
```

Descriptions make aliases searchable by capability. State the preferred use in the description.
Use `preferred_for` to list commands that this alias should replace in search results.
Preferred aliases rank first for exact command-name searches, including searches with Jev.
Other searches use the existing ranking rules. Registration does not force agents to execute an alias.
Names such as `alias:co` identify catalog entries. Invoke the shell name `co`.
Bare-name lookup selects an executable first when names conflict. Use `alias:NAME` to select the alias explicitly.

`inspect` displays alias metadata without running help commands or loading shell files.
Alias availability remains unverified. Check `type co` in the indicated shell before execution.
An agent's shell can lack aliases that your interactive shell loads.
Use a shell with the required definitions when you invoke an alias or function.

Refresh reads these explicit descriptions. It does not parse shell files or start an interactive shell.
This keeps descriptions stable and prevents shell startup actions during scheduled refresh.
Remove an entry from the configuration and refresh to remove it from the catalog.
Jev receives alias names and descriptions when alias entries reach its shortlist.

## Sources and refresh

Refresh scans the configured command directories and MCP client configs.
It preserves other paths for duplicate command names and resolves symbolic links.
Metadata comes from:

- Homebrew's installed formula descriptions.
- Manual page NAME sections in sections 1 and 8.
- Installed skill descriptions whose names match a command or command prefix.
- Script header comments, embedded usage text, and Python module descriptions.
- Nearby package descriptions and repository README introductions.
- MCP server names from Codex, Claude, Cursor, and OpenCode configuration files.

MCP discovery reads configuration only. It does not start servers or store
command arguments, URLs, environment values, or other server settings.

Refresh never executes discovered commands. It runs the existing Homebrew CLI
with automatic updates disabled. Commands without documentation remain searchable
by name. Configure aliases and shell functions in the `aliases` list.
Executables outside configured directories remain outside discovery.

`~/.local/share/tool-catalog/config.json` contains the captured PATH, extra paths,
manual directories, skill directories, and MCP config paths. Refresh detects
additions, removals, and documentation changes in those locations. Add a path
there if an installer introduces a new command or MCP config directory.
No list of individual tools is required.

`catalog.json` stores command results in `tools` and MCP results in
`mcp_servers`. The `aliases` array stores configured aliases and functions.
Refresh writes the catalog atomically.
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
