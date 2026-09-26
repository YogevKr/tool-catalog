#!/usr/bin/env python3
"""Install the local catalog, discovery skill, and optional macOS refresh task."""

import argparse
import os
import plistlib
import shutil
import subprocess  # nosec B404: fixed launchctl bootstrap command below
import sys
from pathlib import Path

import tool_catalog

LABEL = "ai.sawmills.tool-catalog"


def install(home, schedule):
    repo = Path(__file__).resolve().parents[1]
    state = home / ".local/share/tool-catalog"
    binary = home / ".local/bin/tool-catalog"
    plist = home / f"Library/LaunchAgents/{LABEL}.plist"
    skill_dirs = [home / ".agents/skills/local-tools"]
    for agent in (".claude",):
        if (home / agent).is_dir():
            skill_dirs.append(home / agent / "skills/local-tools")
    targets = [state, binary, *skill_dirs]
    if schedule:
        targets.append(plist)
    conflicts = [str(p) for p in targets if p.exists() or p.is_symlink()]
    if conflicts:
        raise FileExistsError("Existing installation paths: " + ", ".join(conflicts))
    if schedule and sys.platform != "darwin":
        raise RuntimeError("Scheduled refresh requires macOS. Use --no-schedule.")
    config = tool_catalog.default_config()
    config["jev"] = {
        "enabled": False,
        "model": "jev-latest",
        "timeout": 8,
        "candidate_limit": 12,
    }
    tool_catalog.write_json(state / "config.json", config)
    binary.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(repo / "scripts/tool_catalog.py", binary)
    binary.chmod(0o755)
    for directory in skill_dirs:
        directory.mkdir(parents=True)
        shutil.copyfile(repo / "skills/local-tools/SKILL.md", directory / "SKILL.md")
    catalog = tool_catalog.refresh(state)
    print(f"Installed {binary}: {len(catalog['tools'])} commands")
    if schedule:
        task = {
            "Label": LABEL,
            "ProgramArguments": [
                sys.executable,
                str(binary),
                "--state-dir",
                str(state),
                "refresh",
            ],
            "EnvironmentVariables": {"PATH": os.pathsep.join(config["paths"])},
            "StartInterval": 3600,
            "RunAtLoad": True,
            "StandardOutPath": str(state / "refresh.log"),
            "StandardErrorPath": str(state / "refresh-error.log"),
        }
        plist.parent.mkdir(parents=True, exist_ok=True)
        with plist.open("wb") as stream:
            plistlib.dump(task, stream)
        subprocess.run(  # nosec B603: fixed launchd registration, no shell
            ["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)],
            check=True,
        )
        print(f"Enabled hourly refresh: {LABEL}")
    print("Start a new agent session to load the local-tools skill.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-schedule", action="store_true")
    args = parser.parse_args()
    try:
        install(Path.home(), not args.no_schedule)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Install failed: {error}\n")


if __name__ == "__main__":
    main()
