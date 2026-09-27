#!/usr/bin/env python3
"""Discover installed commands and search their local documentation."""

import argparse
import collections
import getpass
import gzip
import json
import os
import re
import shlex
import shutil
import stat
import subprocess  # nosec B404: only fixed Homebrew metadata command below
import tempfile
import textwrap
import time
import urllib.error
import urllib.request
import warnings
from pathlib import Path

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.10 and older
    tomllib = None

STATE = Path.home() / ".local/share/tool-catalog"
MAX_TEXT = 131072
MAX_HELP = 24576
HELP_TIMEOUT = 5
STOP_WORDS = set(
    "a an and any can do find for from i in me my of on please the to tool tools use with".split()
)
TERM_ALIASES = {
    "applications": {"application", "app", "service"},
    "apps": {"app", "application", "service"},
    "dashboards": {"dashboard", "grafana", "visualization"},
    "error": {"error", "failure", "issue", "problem"},
    "errors": {"error", "failure", "issue", "problem"},
    "inspect": {"inspect", "examine", "diagnose", "triage", "debug"},
    "log": {"logs", "logging", "events"},
    "logs": {"log", "logging", "events"},
    "metric": {"metric", "metrics", "promql", "monitoring", "telemetry", "grafana"},
    "metrics": {"metric", "metrics", "promql", "monitoring", "telemetry", "grafana"},
    "queries": {"query", "search", "lookup", "inspect"},
    "trace": {"trace", "tracing", "spans", "telemetry"},
    "traces": {"trace", "tracing", "spans", "telemetry"},
    "kubernetes": {"k8s", "kubectl", "cluster", "pod", "pods"},
    "k8s": {"kubernetes", "kubectl", "cluster", "pod", "pods"},
    "cluster": {"kubernetes", "k8s", "kubectl"},
    "pod": {"pods", "kubernetes", "k8s", "kubectl", "container", "containers"},
    "pods": {"pod", "kubernetes", "k8s", "kubectl", "container", "containers"},
    "container": {"containers", "kubernetes", "k8s", "kubectl", "pod", "pods"},
    "containers": {"container", "kubernetes", "k8s", "kubectl", "pod", "pods"},
    "worktree": {"worktree", "git", "branch"},
    "worktrees": {"worktree", "git", "branch"},
}


def read_text(path):
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as stream:
        return stream.read(MAX_TEXT)


def clean(text):
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
    return " ".join(text.split())[:1800]


def clean_help(text):
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
    return text.replace("\x00", "")[:MAX_HELP].strip()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, indent=2)
        stream.write("\n")
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def scan(directories, warnings):
    commands = collections.defaultdict(list)
    for directory in dict.fromkeys(directories):
        root = Path(directory).expanduser()
        if not root.is_absolute():
            continue
        try:
            entries = sorted(root.iterdir())
        except FileNotFoundError:
            continue
        except OSError as error:
            warnings.append(f"Cannot scan {root}: {error}")
            continue
        for path in entries:
            if path.is_file() and os.access(path, os.X_OK):
                commands[path.name].append(str(path))
    return commands


def brew_metadata(warnings):
    brew = shutil.which("brew")
    if not brew:
        return {}
    try:
        result = subprocess.run(  # nosec B603: fixed metadata command, no shell
            [brew, "info", "--json=v2", "--installed"],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
            env={**os.environ, "HOMEBREW_NO_AUTO_UPDATE": "1"},
        )
        formulae = json.loads(result.stdout)["formulae"]
        return {f["name"]: f.get("desc") or "" for f in formulae}
    except (OSError, subprocess.SubprocessError, ValueError, KeyError) as error:
        warnings.append(f"Homebrew metadata unavailable: {error}")
        return {}


def skill_metadata(roots, warnings):
    descriptions = []
    seen = set()
    for root in roots:
        for path in sorted(Path(root).expanduser().glob("*/SKILL.md")):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                text = read_text(path)
            except OSError as error:
                warnings.append(f"Cannot read skill {path}: {error}")
                continue
            match = re.search(r"(?ms)^description:\s*(.*?)(?=^\S|\Z)", text)
            if match:
                description = clean(match[1].lstrip(">|- \n").strip("\"'"))
                descriptions.append((path.parent.name, description, str(path)))
    return descriptions


def _mcp_configs(data, source, client, scope="global"):
    """Return MCP server definitions from known client config shapes."""
    if not isinstance(data, dict):
        return []
    found = []
    for key in ("mcpServers", "mcp_servers", "mcp"):
        servers = data.get(key)
        if not isinstance(servers, dict):
            continue
        for name, config in servers.items():
            if isinstance(name, str) and isinstance(config, dict):
                found.append((name, config, source, client, scope))
    projects = data.get("projects")
    if isinstance(projects, dict):
        for project, project_config in projects.items():
            if not isinstance(project, str) or not isinstance(project_config, dict):
                continue
            found.extend(_mcp_configs(project_config, source, client, project))
    return found


def _mcp_client(path):
    parts = Path(path).parts
    if ".codex" in parts:
        return "codex"
    if ".claude.json" in parts:
        return "claude"
    if ".cursor" in parts:
        return "cursor"
    if "opencode" in parts:
        return "opencode"
    return "unknown"


def _mcp_transport(config):
    if isinstance(config.get("url"), str) or config.get("type") == "remote":
        return "remote"
    if isinstance(config.get("command"), (str, list)):
        return "stdio"
    return "unknown"


def _mcp_command(config):
    command = config.get("command")
    if isinstance(command, list):
        command = command[0] if command else ""
    if not isinstance(command, str) or not command:
        return ""
    try:
        parts = shlex.split(command)
    except ValueError:
        return ""
    return Path(parts[0]).name if parts else ""


def mcp_metadata(paths, warnings):
    """Read configured MCP server names without executing or exposing config values."""
    if not isinstance(paths, (list, tuple)):
        warnings.append("MCP configuration paths must be a list")
        return []
    entries = {}
    configured_paths = []
    for configured_path in paths:
        if not isinstance(configured_path, (str, os.PathLike)):
            warnings.append("Skipping MCP config path that is not a string")
            continue
        configured_path = str(configured_path)
        if configured_path not in configured_paths:
            configured_paths.append(configured_path)
    for configured_path in configured_paths:
        path = Path(configured_path).expanduser()
        if not path.is_file():
            continue
        try:
            if path.suffix == ".toml":
                if tomllib is None:
                    warnings.append(f"Cannot read MCP config {path}: TOML is unavailable")
                    continue
                with path.open("rb") as stream:
                    data = tomllib.load(stream)
            else:
                with path.open(encoding="utf-8", errors="replace") as stream:
                    data = json.load(stream)
        except (OSError, ValueError) as error:
            warnings.append(f"Cannot read MCP config {path}: {error}")
            continue
        client = _mcp_client(path)
        for name, config, source, source_client, scope in _mcp_configs(
            data, str(path), client
        ):
            key = name
            entry = entries.setdefault(
                key,
                {
                    "name": name,
                    "kind": "mcp",
                    "path": source,
                    "resolved_path": source,
                    "other_paths": [],
                    "description": f"MCP server {name}",
                    "documents": [],
                    "mcp": {"clients": [], "transports": [], "scopes": [], "commands": []},
                },
            )
            if source not in [entry["path"], *entry["other_paths"]]:
                entry["other_paths"].append(source)
            details = entry["mcp"]
            if source_client not in details["clients"]:
                details["clients"].append(source_client)
            transport = _mcp_transport(config)
            if transport not in details["transports"]:
                details["transports"].append(transport)
            if scope not in details["scopes"]:
                details["scopes"].append(scope)
            command = _mcp_command(config)
            if command and command not in details["commands"]:
                details["commands"].append(command)
            document = f"MCP server {name} configured for {source_client} ({transport})."
            if scope != "global":
                document += f" Scope: {scope}."
            if not any(item["text"] == document and item["source"] == source for item in entry["documents"]):
                entry["documents"].append({"text": document, "source": source})
    for entry in entries.values():
        clients = ", ".join(entry["mcp"]["clients"])
        transports = ", ".join(entry["mcp"]["transports"])
        entry["description"] = f"MCP server {entry['name']} ({transports}; configured for {clients})."
    return sorted(entries.values(), key=lambda entry: entry["name"])


def alias_metadata(values, warnings):
    """Build searchable entries for user-defined shell aliases and functions."""
    if not isinstance(values, list):
        warnings.append("Aliases must be a list")
        return []
    entries = {}
    for index, value in enumerate(values):
        fields = ("name", "description", "shell", "source", "command")
        if not isinstance(value, dict) or any(
            not isinstance(value.get(field, ""), str) for field in fields
        ):
            warnings.append(f"Skipping alias at index {index}: fields must be strings")
            continue
        name = value.get("name", "")
        description = clean(value.get("description", ""))
        preferred_for = value.get("preferred_for", [])
        if not isinstance(preferred_for, list) or any(
            not isinstance(command, str) or not command.strip()
            for command in preferred_for
        ):
            warnings.append(f"Skipping alias at index {index}: preferred_for must contain command names")
            continue
        if not re.fullmatch(r"[A-Za-z0-9_.+-]+", name) or not description:
            warnings.append(f"Skipping alias at index {index}: name and description are required")
            continue
        if name in entries:
            warnings.append(f"Skipping duplicate alias {name}")
            continue
        source = value.get("source") or "config:aliases"
        entry = {
            "name": name,
            "kind": "alias",
            "path": source,
            "resolved_path": "",
            "other_paths": [],
            "description": description,
            "documents": [{"text": description, "source": source}],
            "availability": "unverified",
            "preferred_for": [command.strip() for command in preferred_for],
        }
        entry.update({field: value[field] for field in ("shell", "command") if value.get(field)})
        entries[name] = entry
    return sorted(entries.values(), key=lambda entry: entry["name"])


def manual_description(path):
    text = read_text(path)
    nd = re.search(r"(?m)^\.Nd\s+(.+)", text)
    if nd:
        return clean(nd[1])
    section = re.search(r'(?is)\.SH\s+"?NAME"?\s*\n(.*?)(?=\n\.SH|\Z)', text)
    if not section:
        return ""
    text = re.sub(r"(?m)^\.[A-Za-z]+\s*", "", section[1])
    text = re.sub(r"\\f(?:\[[^]]*\]|.)", "", text)
    return clean(text.replace(r"\-", "-").replace(r"\&", ""))


def script_description(target):
    with target.open("rb") as stream:
        head = stream.read(MAX_TEXT)
    if not head.startswith(b"#!") or b"\0" in head:
        return ""
    text = head.decode("utf-8", errors="replace")
    patterns = [
        r'\A#![^\n]*\n\s*(?:"""|\'\'\')(.*?)(?:"""|\'\'\')',
        r"(?m)^Usage:[^\n]*\n\s*\n([^\n]+)",
        r"""ArgumentParser\(\s*description\s*=\s*["']([^"']+)""",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.S)
        if match:
            return clean(match[1])
    comments = re.match(r"\A#![^\n]*\n\s*((?:#[^\n]*\n)+)", text)
    return clean(re.sub(r"(?m)^#\s?", "", comments[1])) if comments else ""


def local_description(target):
    """Read descriptions, never import packages or execute discovered programs."""
    text = script_description(target)
    if text:
        return text, str(target)
    for parent in list(target.parents)[:4]:
        package = parent / "package.json"
        if package.is_file():
            description = json.loads(read_text(package)).get("description", "")
            if description:
                return clean(description), str(package)
        if (parent / ".git").exists():
            readme = parent / "README.md"
            if readme.is_file():
                paragraphs = re.split(r"\n\s*\n", read_text(readme))
                prose = next((p for p in paragraphs if re.match(r"^[A-Za-z]", p)), "")
                return clean(prose), str(readme)
            break
    return "", ""


def manual_documents(name, roots, warnings):
    documents = []
    for root in roots:
        for suffix in (".1", ".1.gz", ".8", ".8.gz"):
            path = Path(root) / ("man" + suffix[1]) / (name + suffix)
            if not path.is_file():
                continue
            try:
                text = manual_description(path)
                if text:
                    documents.append({"text": text, "source": str(path)})
            except (OSError, EOFError) as error:
                warnings.append(f"Cannot read manual {path}: {error}")
    return documents


def describe(name, paths, sources, warnings):
    target = Path(paths[0]).resolve()
    documents = []
    formulae, skills, man_roots = sources
    parts = target.parts
    if "Cellar" in parts:
        formula = parts[parts.index("Cellar") + 1]
        if formulae.get(formula):
            documents.append({"text": formulae[formula], "source": f"brew:{formula}"})
    for skill_name, text, path in skills:
        if skill_name == name or skill_name.startswith(name + "-"):
            documents.append({"text": text, "source": path})
    documents.extend(manual_documents(name, man_roots, warnings))
    if not documents:
        try:
            text, source = local_description(target)
            if text:
                documents.append({"text": text, "source": source})
        except (OSError, ValueError) as error:
            warnings.append(f"Cannot read description for {name}: {error}")
    return {
        "name": name,
        "path": paths[0],
        "resolved_path": str(target),
        "other_paths": paths[1:],
        "description": (
            documents[0]["text"] if documents else "No local description found."
        ),
        "documents": documents,
    }


def capture_help(entry, timeout=HELP_TIMEOUT, cwd=None):
    """Capture a bounded help response without invoking a shell or user arguments."""
    command = entry["resolved_path"]
    if not Path(command).is_file() or not os.access(command, os.X_OK):
        return "", "unavailable"
    for flag in ("--help", "help", "-h"):
        try:
            result = subprocess.run(  # nosec B603: fixed executable and help flags only
                [command, flag],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                cwd=str(cwd or Path.cwd()),
                env={"PATH": os.environ.get("PATH", ""), "HOME": str(Path.home())},
            )
        except (OSError, subprocess.SubprocessError):
            continue
        output = clean_help(
            "\n".join(part for part in (result.stdout, result.stderr) if part)
        )
        if output:
            return output, flag
    return "", "unavailable"


def inspect_tool(catalog, name, state, force=False, cwd=None):
    entry = find_catalog_entry(catalog, name)
    if entry is None:
        return None
    if entry.get("kind") in ("mcp", "alias"):
        return entry
    help_cwd = str(cwd or Path.cwd())
    if force or not entry.get("help") or entry.get("help_cwd") != help_cwd:
        help_text, help_flag = capture_help(entry, cwd=cwd)
        entry["help"] = help_text
        entry["help_flag"] = help_flag
        entry["help_cwd"] = help_cwd
        entry["help_captured_at"] = time.time()
        write_json(state / "catalog.json", catalog)
    return entry


def default_config():
    home = Path.home()
    path = [
        p
        for p in os.environ.get("PATH", "").split(os.pathsep)
        if p and "/.codex/tmp/" not in p
    ]
    return {
        "paths": path,
        "extra_paths": [
            str(home / p) for p in (".local/bin", "bin", "go/bin", ".cargo/bin")
        ]
        + ["/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin"],
        "skill_roots": [
            str(home / p) for p in (".agents/skills", ".codex/skills", ".claude/skills")
        ],
        "man_roots": [
            "/usr/share/man",
            "/usr/local/share/man",
            "/opt/homebrew/share/man",
            str(home / ".local/share/man"),
        ],
        "mcp_configs": [
            str(home / ".claude.json"),
            str(home / ".cursor/mcp.json"),
            str(home / ".codex/config.toml"),
            str(home / ".config/opencode/opencode.json"),
        ],
        "aliases": [],
    }


def refresh(state):
    config_path = state / "config.json"
    config = (
        json.loads(read_text(config_path)) if config_path.exists() else default_config()
    )
    warnings = []
    commands = scan(config["paths"] + config["extra_paths"], warnings)
    sources = (
        brew_metadata(warnings),
        skill_metadata(config["skill_roots"], warnings),
        config["man_roots"],
    )
    entries = [
        describe(name, paths, sources, warnings)
        for name, paths in sorted(commands.items())
    ]
    mcp_servers = mcp_metadata(
        config.get("mcp_configs", default_config()["mcp_configs"]), warnings
    )
    aliases = alias_metadata(config.get("aliases", []), warnings)
    catalog = {
        "version": 3,
        "updated_at": time.time(),
        "tools": entries,
        "mcp_servers": mcp_servers,
        "aliases": aliases,
        "warnings": warnings,
    }
    write_json(state / "catalog.json", catalog)
    return catalog


def load_catalog(state):
    path = state / "catalog.json"
    if not path.exists() or time.time() - path.stat().st_mtime > 3600:
        return refresh(state)
    with path.open() as stream:
        return json.load(stream)


def load_config(state):
    path = state / "config.json"
    if not path.exists():
        return {}
    try:
        with path.open() as stream:
            config = json.load(stream)
        return config if isinstance(config, dict) else {}
    except (OSError, ValueError):
        return {}


def catalog_entries(catalog):
    """Return all entries while accepting older catalog files."""
    return (
        list(catalog.get("tools", []))
        + list(catalog.get("mcp_servers", []))
        + list(catalog.get("aliases", []))
    )


def find_catalog_entry(catalog, name):
    kind = None
    target = name
    for prefix, candidate in (("mcp:", "mcp"), ("alias:", "alias")):
        if name.startswith(prefix):
            kind = candidate
            target = name[len(prefix) :]
            break
    return next(
        (
            entry
            for entry in catalog_entries(catalog)
            if entry["name"] == target and (kind is None or entry.get("kind") == kind)
        ),
        None,
    )


def entry_display_name(entry):
    kind = entry.get("kind")
    return f"{kind}:{entry['name']}" if kind in ("mcp", "alias") else entry["name"]


def credentials_path():
    return Path.home() / ".config/tool-catalog/credentials.json"


def load_api_key():
    key = os.environ.get("TYPESAFE_API_KEY")
    if key:
        return key
    path = credentials_path()
    if not path.exists():
        return ""
    for location in (path.parent, path):
        info = location.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("Credentials require a private directory and file.")
    with path.open() as stream:
        data = json.load(stream)
    if not isinstance(data, dict) or not isinstance(data.get("TYPESAFE_API_KEY"), str):
        raise ValueError("Invalid credentials format.")
    return data["TYPESAFE_API_KEY"].strip()


def configure_jev(state):
    config_path = state / "config.json"
    config = (
        json.loads(read_text(config_path)) if config_path.exists() else default_config()
    )
    if not isinstance(config, dict) or not isinstance(config.get("jev", {}), dict):
        raise ValueError("Invalid configuration format.")
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                key = getpass.getpass("TypeSafe API key (hidden): ")
        except (getpass.GetPassWarning, EOFError, KeyboardInterrupt):
            raise ValueError(
                "Key entry cancelled or hidden input unavailable."
            ) from None
    key = key.strip()
    if not key:
        raise ValueError("The API key must not be empty.")
    path = credentials_path()
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("Credentials paths must not be symbolic links.")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    write_json(path, {"TYPESAFE_API_KEY": key})
    config.setdefault("jev", {})["enabled"] = True
    write_json(config_path, config)
    print(f"Saved credentials to {path}. Jev is enabled for searches.")


def tokens(text):
    values = set()
    for value in re.findall(r"[a-z0-9]+", text.lower()):
        if value in STOP_WORDS:
            continue
        values.add(
            value.rstrip("s") if len(value) > 4 and value.endswith("s") else value
        )
    return values


def expanded_terms(text):
    terms = tokens(text)
    for term in tuple(terms):
        terms.update(TERM_ALIASES.get(term, ()))
    return terms


def jev_rerank(query, entries, timeout=8, settings=None):
    """Optionally rerank a lexical shortlist with a TypeSafe Noul judgment."""
    settings = settings or {}
    try:
        api_key = load_api_key()
    except (OSError, ValueError):
        return (
            entries,
            "Jev skipped: credentials unavailable; run tool-catalog configure --jev.",
        )
    if not api_key:
        return (
            entries,
            "Jev skipped: API key is not set; run tool-catalog configure --jev.",
        )
    endpoint = settings.get(
        "endpoint",
        os.environ.get("TYPESAFE_ENDPOINT", "https://api.typesafe.ai/v1/systemone"),
    )
    model = settings.get("model", os.environ.get("TYPESAFE_MODEL", "jev-latest"))
    scored = []
    for entry in entries:
        state = {
            "query": query,
            "candidate": {
                "name": entry["name"],
                "description": entry["description"],
                "help": entry.get("help", "")[:4000],
            },
        }
        payload = {
            "state": state,
            "model": model,
            "questions": {
                "relevant": {
                    "type": "noul",
                    "instructions": "Does this available tool help complete the user's request?",
                    "criteria": {
                        "true": "The command directly supports the requested task or is a strong practical match.",
                        "false": "The command is unrelated, too general, or only shares a broad word.",
                    },
                }
            },
        }
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310: endpoint is explicit or user-configured
                body = json.loads(response.read(MAX_TEXT).decode("utf-8"))
            score = float(body["answers"]["relevant"]["noul"])
        except (OSError, urllib.error.URLError, ValueError, KeyError, TypeError):
            return entries, "Jev failed; lexical ranking used."
        copy = dict(entry)
        copy["jev_score"] = score
        scored.append((score, copy))
    scored.sort(key=lambda item: (-item[0], item[1]["name"]))
    return [entry for _, entry in scored], f"Jev reranked {len(scored)} candidates."


def search(catalog, query, limit):
    terms = expanded_terms(query)
    ranked = []
    for entry in catalog_entries(catalog):
        name_terms = tokens(entry["name"])
        description_terms = tokens(entry["description"])
        document_terms = tokens(" ".join(d["text"] for d in entry["documents"]))
        preferred_terms = tokens(" ".join(entry.get("preferred_for", [])))
        matched = terms & (name_terms | description_terms | document_terms | preferred_terms)
        if not matched:
            continue
        score = 10 * len(terms & name_terms) + 4 * len(terms & description_terms)
        score += len(matched) + 2 * len(terms & tokens(entry.get("help", "")))
        score += 20 * (query.strip().lower() in (
            entry["name"].lower(), entry_display_name(entry).lower()
        ))
        score += 10 * bool(tokens(query) <= matched)
        score += 3 * bool(entry["documents"])
        if entry.get("kind") == "alias":
            available = True
        elif entry.get("kind") == "mcp":
            available = Path(entry["path"]).is_file()
        else:
            available = Path(entry["path"]).is_file() and os.access(
                entry["path"], os.X_OK
            )
        if available:
            ranked.append((score, entry))
    ranked.sort(key=lambda pair: (
        not preferred_alias(query, pair[1]), -pair[0], pair[1]["name"]
    ))
    return [entry for _, entry in ranked[:limit]]


def preferred_alias(query, entry):
    """Honor explicit alias preferences for exact command-name searches."""
    return entry.get("kind") == "alias" and query.strip().lower() in (
        command.lower() for command in entry.get("preferred_for", [])
    )


def prioritize_exact(query, entries):
    """Keep exact MCP server names ahead of semantic matches."""
    target = query.strip().lower()
    return sorted(
        enumerate(entries),
        key=lambda item: (
            not preferred_alias(query, item[1]),
            not (
                item[1].get("kind") == "mcp"
                and item[1]["name"].lower() == target
            ),
            target not in (
                item[1]["name"].lower(), entry_display_name(item[1]).lower()
            ),
            item[0],
        ),
    )


def print_search_results(entries, jev_message=""):
    if not entries:
        print("No matching commands. Try fewer words or run tool-catalog refresh.")
        return
    width = min(shutil.get_terminal_size((100, 24)).columns, 120)
    scored = any("jev_score" in entry for entry in entries)
    name_width = max(7, max(len(entry_display_name(entry)) for entry in entries))
    score_width = 7 if scored else 0
    description_width = width - name_width - score_width - 2
    compact = description_width < 25
    ranking = "Jev" if scored else "Local"
    noun = "result" if len(entries) == 1 else "results"
    print(f"{len(entries)} {noun} · {ranking} ranking\n")
    if not compact:
        score_header = "  SCORE" if scored else ""
        print(f"{'COMMAND':<{name_width}}{score_header}  DESCRIPTION")
    for entry in entries:
        name = entry_display_name(entry)
        description = clean(entry["description"])
        description = re.sub(r"^" + re.escape(name) + r"\s+[—–-]\s+", "", description)
        score = (
            f"  {entry['jev_score']:5.2f}"
            if "jev_score" in entry
            else " " * score_width
        )
        if compact:
            print(f"{name}{score}")
            print(
                "  "
                + textwrap.shorten(
                    description, width=max(4, width - 2), placeholder="…"
                )
            )
        else:
            summary = textwrap.shorten(
                description, width=description_width, placeholder="…"
            )
            print(f"{name:<{name_width}}{score}  {summary}")
    if jev_message and not scored:
        print(f"\n{jev_message}")
    print("\nDetails: tool-catalog show <name>")


def catalog_status(catalog, state):
    return {
        "commands": len(catalog["tools"]),
        "described": sum(bool(t["documents"]) for t in catalog["tools"]),
        "mcp_servers": len(catalog.get("mcp_servers", [])),
        "aliases": len(catalog.get("aliases", [])),
        "age_seconds": round(time.time() - catalog["updated_at"]),
        "catalog": str(state / "catalog.json"),
        "warnings": catalog["warnings"],
    }


def search_catalog(args, catalog, config):
    jev_config = config.get("jev", {})
    use_jev = args.jev if args.jev is not None else bool(jev_config.get("enabled", False))
    requested_limit = max(1, args.limit)
    search_limit = requested_limit
    if use_jev:
        try:
            configured_limit = int(jev_config.get("candidate_limit", max(requested_limit, 12)))
        except (TypeError, ValueError):
            configured_limit = max(requested_limit, 12)
        search_limit = max(requested_limit, min(configured_limit, 50))
    query = " ".join(args.query)
    output = search(catalog, query, search_limit)
    if use_jev and output:
        output, message = jev_rerank(
            query, output, timeout=float(jev_config.get("timeout", 8)), settings=jev_config
        )
        output = [entry for _, entry in prioritize_exact(query, output)]
        return output[:requested_limit], message
    return output, ""


def print_alias_details(entry):
    """Show invocation metadata without running a shell or the alias."""
    print("  type: shell alias or function")
    print(f"  invocation: {entry['name']}")
    for field, label in (("shell", "shell"), ("command", "expansion"), ("path", "source")):
        if entry.get(field):
            print(f"  {label}: {entry[field]}")
    if entry.get("preferred_for"):
        print(f"  preferred for: {', '.join(entry['preferred_for'])}")
    print("  availability: unverified; check type NAME in the indicated shell")


def command_output(args, catalog, config, state, parser):
    if args.command in ("refresh", "status"):
        return catalog_status(catalog, state)
    if args.command == "list":
        output = sorted(catalog_entries(catalog), key=lambda entry: entry["name"])
        if not args.json:
            for entry in output:
                suffix = {"mcp": " [mcp]", "alias": " [alias]"}.get(entry.get("kind"), "")
                print(entry_display_name(entry) + suffix)
            return None
        return output
    if args.command in ("show", "inspect"):
        if args.command == "show":
            output = find_catalog_entry(catalog, args.name)
        else:
            output = inspect_tool(catalog, args.name, state, args.refresh, args.cwd)
        if output is None:
            parser.exit(1, f"Tool not found in catalog: {args.name}\n")
        if args.command == "inspect" and not args.json:
            print(f"{output['name']} — {output['description'][:240]}")
            if output.get("kind") == "mcp":
                print(f"  config: {output['path']}")
                print(f"  clients: {', '.join(output['mcp']['clients'])}")
                print(f"  transport: {', '.join(output['mcp']['transports'])}")
            elif output.get("kind") == "alias":
                print_alias_details(output)
            else:
                print(f"  path: {output['path']}")
                print(f"  help flag: {output.get('help_flag', 'cached')}")
                print("\n" + (output.get("help") or "No help output captured."))
            return None
        return output
    output, message = search_catalog(args, catalog, config)
    if not args.json:
        print_search_results(output, message)
        return None
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=STATE)
    sub = parser.add_subparsers(dest="command", required=True)
    configure = sub.add_parser("configure", help="Store credentials and enable Jev")
    configure.add_argument("--jev", action="store_true", required=True)
    sub.add_parser(
        "refresh", help="Rebuild metadata without executing discovered commands"
    )
    sub.add_parser("status", help="Show catalog age, coverage, and warnings")
    listing = sub.add_parser("list", help="List all indexed commands alphabetically")
    listing.add_argument(
        "--json", action="store_true", help="Include paths and descriptions"
    )
    find = sub.add_parser("search", help="Search names and local descriptions")
    find.add_argument("query", nargs="+")
    find.add_argument("--limit", type=int, default=8)
    find.add_argument("--json", action="store_true")
    jev = find.add_mutually_exclusive_group()
    jev.add_argument(
        "--jev", dest="jev", action="store_true", help="Enable Jev semantic reranking"
    )
    jev.add_argument(
        "--no-jev",
        dest="jev",
        action="store_false",
        help="Disable configured Jev reranking",
    )
    find.set_defaults(jev=None)
    inspect = sub.add_parser("inspect", help="Show a command and cached help output")
    inspect.add_argument("name")
    inspect.add_argument("--refresh", action="store_true", help="Capture help again")
    inspect.add_argument(
        "--cwd", type=Path, default=Path.cwd(), help="Directory for help capture"
    )
    inspect.add_argument("--json", action="store_true")
    show = sub.add_parser("show", help="Show paths, documentation, and sources")
    show.add_argument("name")
    args = parser.parse_args()
    try:
        if args.command == "configure":
            configure_jev(args.state_dir)
            return
        config = load_config(args.state_dir)
        catalog = (
            refresh(args.state_dir)
            if args.command == "refresh"
            else load_catalog(args.state_dir)
        )
        output = command_output(args, catalog, config, args.state_dir, parser)
        if output is not None:
            print(json.dumps(output, indent=2))
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f"Catalog error: {error}\n")


if __name__ == "__main__":
    main()
