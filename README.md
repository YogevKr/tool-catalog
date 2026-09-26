# tool-catalog

Discover installed command line tools for coding agents on macOS.

The catalog scans local PATH directories, Homebrew metadata, manual pages,
agent skill descriptions, and cached command help. It refreshes through launchd.
Agents search it with short capability phrases.

```sh
python3 scripts/install_tool_catalog.py
tool-catalog search "kubernetes pod logs"
tool-catalog search "github pull request"
```

Enable optional Jev ranking once:

```sh
tool-catalog configure --jev
tool-catalog search "kubernetes pod logs"
```

Setup prompts for a hidden API key and stores it in a protected local credentials file.
Normal searches need no wrapper or secret manager.

See [docs/local-tool-catalog.md](docs/local-tool-catalog.md) for configuration,
Jev reranking, inspection, and verification.
