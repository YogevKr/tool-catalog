import importlib.util
import json
import os
import plistlib
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/tool_catalog.py"
SPEC = importlib.util.spec_from_file_location("tool_catalog", SCRIPT)
catalog = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(catalog)
sys.modules["tool_catalog"] = catalog
INSTALL_SPEC = importlib.util.spec_from_file_location(
    "install_tool_catalog", SCRIPT.with_name("install_tool_catalog.py")
)
installer = importlib.util.module_from_spec(INSTALL_SPEC)
INSTALL_SPEC.loader.exec_module(installer)


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def executable(self, relative, text="#!/bin/sh\n# Custom test command\nexit 91\n"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o755)
        return path

    def test_scan_preserves_path_order_and_ignores_nonexecutables(self):
        first = self.executable("first/query")
        second = self.executable("second/query")
        (first.parent / "notes").write_text("not a command")
        (first.parent / "broken").symlink_to(self.root / "absent")
        warnings = []
        result = catalog.scan(
            [str(first.parent), str(first.parent), str(second.parent)], warnings
        )
        self.assertEqual(result, {"query": [str(first), str(second)]})
        self.assertEqual(warnings, [])

    def test_brew_symlink_maps_binary_to_formula(self):
        target = self.executable("Cellar/metrics/1.0/bin/inspect")
        link = self.root / "inspect"
        link.symlink_to(target)
        entry = catalog.describe(
            "inspect", [str(link)], ({"metrics": "Query Grafana metrics"}, [], []), []
        )
        self.assertEqual(entry["description"], "Query Grafana metrics")
        self.assertEqual(entry["resolved_path"], str(target.resolve()))

    def test_refresh_adds_and_removes_commands_without_running_them(self):
        marker = self.root / "executed"
        binary = self.executable(
            "bin/custom", f"#!/bin/sh\n# Query private dashboards\ntouch {marker}\n"
        )
        state = self.root / "state"
        config = {
            "paths": [str(binary.parent)],
            "extra_paths": [],
            "skill_roots": [],
            "man_roots": [],
        }
        catalog.write_json(state / "config.json", config)
        with mock.patch.object(catalog, "brew_metadata", return_value={}):
            first = catalog.refresh(state)
            self.assertEqual(
                first["tools"][0]["description"], "Query private dashboards"
            )
            self.assertFalse(marker.exists())
            binary.unlink()
            self.executable("bin/replacement")
            second = catalog.refresh(state)
        self.assertEqual([t["name"] for t in second["tools"]], ["replacement"])
        self.assertEqual(json.loads((state / "catalog.json").read_text()), second)

    def test_skill_description_supports_multiline_and_deduplicates_links(self):
        skill = self.root / "skills/custom-query"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: custom-query\ndescription: >\n  Query private metrics.\n  Inspect dashboards.\n---\n"
        )
        other = self.root / "other/custom-query"
        other.parent.mkdir()
        other.symlink_to(skill)
        entries = catalog.skill_metadata([skill.parent, other.parent], [])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0][1], "Query private metrics. Inspect dashboards.")

    def test_manual_name_section_and_mdoc(self):
        path = self.root / "custom.1"
        path.write_text(
            ".SH NAME\ncustom \\- Query metrics\n.SH OPTIONS\nDo not include this\n"
        )
        self.assertEqual(catalog.manual_description(path), "custom - Query metrics")
        path.write_text(".Nm custom\n.Nd Query metrics\n")
        self.assertEqual(catalog.manual_description(path), "Query metrics")

    def test_extracts_embedded_usage_without_running_script(self):
        path = self.executable(
            "bin/worktree",
            "#!/bin/sh\nset -eu\ncat <<'EOF'\nUsage: worktree [options]\n\nCreate an isolated Git worktree.\n\nOptions:\nEOF\n",
        )
        self.assertEqual(
            catalog.script_description(path), "Create an isolated Git worktree."
        )
        path.write_text(
            '#!/usr/bin/env python3\nimport argparse\nparser = argparse.ArgumentParser(description="Inspect Datadog errors")\n'
        )
        self.assertEqual(catalog.script_description(path), "Inspect Datadog errors")

    def test_search_prefers_capability_and_exact_name(self):
        entries = []
        for name, description in [
            ("vgraf", "Query Grafana metrics"),
            ("other", "Transform metrics"),
            ("grafana", "Launch browser"),
        ]:
            path = self.executable("bin/" + name)
            entries.append(
                {
                    "name": name,
                    "path": str(path),
                    "description": description,
                    "documents": [{"text": description}],
                }
            )
        data = {"tools": entries}
        self.assertEqual(
            catalog.search(data, "find a tool for grafana metrics", 2)[0]["name"],
            "vgraf",
        )
        self.assertEqual(catalog.search(data, "query metrics", 2)[0]["name"], "vgraf")
        self.assertEqual(catalog.search(data, "grafana", 2)[0]["name"], "grafana")
        self.assertEqual(catalog.search(data, "unrelated", 2), [])
        Path(entries[0]["path"]).unlink()
        self.assertNotIn(
            "vgraf", [t["name"] for t in catalog.search(data, "metrics", 8)]
        )

    def test_search_expands_task_terms(self):
        path = self.executable("bin/triage")
        data = {
            "tools": [
                {
                    "name": "triage",
                    "path": str(path),
                    "description": "Fetch Datadog error tracking issues",
                    "documents": [],
                }
            ]
        }
        self.assertEqual(
            catalog.search(data, "inspect application errors", 1)[0]["name"], "triage"
        )

    def test_search_expands_kubernetes_terms_to_kubectl(self):
        path = self.executable("bin/kubectl")
        data = {
            "tools": [
                {
                    "name": "kubectl",
                    "path": str(path),
                    "description": "No local description found.",
                    "documents": [],
                }
            ]
        }
        self.assertEqual(
            catalog.search(data, "kubernetes pod logs", 1)[0]["name"], "kubectl"
        )

    def test_jev_is_opt_in_and_falls_back_without_a_key(self):
        entries = [
            {
                "name": "tool",
                "description": "Inspect metrics",
                "help": "",
                "path": "/tool",
            }
        ]
        with mock.patch.dict(os.environ, {}, clear=True):
            result, message = catalog.jev_rerank("inspect metrics", entries)
        self.assertEqual(result, entries)
        self.assertIn("not set", message)

    def test_config_enables_jev_without_a_cli_flag(self):
        state = self.root / "state"
        catalog.write_json(state / "config.json", {"jev": {"enabled": True}})
        self.assertTrue(catalog.load_config(state)["jev"]["enabled"])

    def test_jev_reranks_and_keeps_scores_outside_the_catalog(self):
        entries = [
            {"name": "b", "description": "b", "help": "", "path": "/b"},
            {"name": "a", "description": "a", "help": "", "path": "/a"},
        ]

        class Response:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, _limit):
                return json.dumps(self.body).encode("utf-8")

        responses = iter(
            [
                Response({"answers": {"relevant": {"noul": 0.2}}}),
                Response({"answers": {"relevant": {"noul": 0.9}}}),
            ]
        )
        with mock.patch.dict(
            os.environ, {"TYPESAFE_API_KEY": "test-key"}, clear=True
        ), mock.patch.object(
            urllib.request,
            "urlopen",
            side_effect=lambda *args, **kwargs: next(responses),
        ) as urlopen:
            result, message = catalog.jev_rerank("metrics", entries)
        self.assertEqual([item["name"] for item in result], ["a", "b"])
        self.assertEqual(result[0]["jev_score"], 0.9)
        self.assertNotIn("jev_score", entries[0])
        self.assertIn("reranked 2", message)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.headers["Authorization"], "Bearer test-key")

    def test_capture_help_uses_only_help_flags(self):
        path = self.executable(
            "bin/helpful",
            "#!/bin/sh\nprintf '%s\\n' \"$@\" > '"
            + str(self.root / "args")
            + "'\nprintf 'Usage: helpful\\nInspect metrics\\n'\n",
        )
        entry = {"name": "helpful", "resolved_path": str(path)}
        help_text, flag = catalog.capture_help(entry, cwd=self.root)
        self.assertEqual(flag, "--help")
        self.assertIn("Inspect metrics", help_text)
        self.assertEqual((self.root / "args").read_text(), "--help\n")

    def test_inspect_caches_help_and_refreshes_on_request(self):
        path = self.executable("bin/helpful", "#!/bin/sh\nprintf 'First help\\n'\n")
        state = self.root / "state"
        catalog.write_json(
            state / "catalog.json",
            {
                "tools": [
                    {
                        "name": "helpful",
                        "path": str(path),
                        "resolved_path": str(path),
                        "description": "Help",
                        "documents": [],
                    }
                ]
            },
        )
        first = catalog.inspect_tool(
            catalog.load_catalog(state), "helpful", state, cwd=self.root
        )
        self.assertEqual(first["help"], "First help")
        path.write_text("#!/bin/sh\nprintf 'Second help\\n'\n")
        second = catalog.inspect_tool(
            catalog.load_catalog(state), "helpful", state, cwd=self.root
        )
        self.assertEqual(second["help"], "First help")
        third = catalog.inspect_tool(
            catalog.load_catalog(state), "helpful", state, force=True, cwd=self.root
        )
        self.assertEqual(third["help"], "Second help")

    def test_fresh_cache_avoids_refresh_and_old_cache_refreshes(self):
        state = self.root / "state"
        catalog.write_json(state / "catalog.json", {"tools": []})
        with mock.patch.object(
            catalog, "refresh", return_value={"new": True}
        ) as refresh:
            self.assertEqual(catalog.load_catalog(state), {"tools": []})
            refresh.assert_not_called()
            old = time.time() - 3601
            os.utime(state / "catalog.json", (old, old))
            self.assertEqual(catalog.load_catalog(state), {"new": True})
            refresh.assert_called_once_with(state)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)

    def test_installs_skill_and_hourly_job_with_explicit_paths(self):
        (self.home / ".claude").mkdir()
        with mock.patch.object(
            catalog, "refresh", return_value={"tools": []}
        ), mock.patch.object(installer.subprocess, "run") as run, mock.patch.object(
            installer.sys, "platform", "darwin"
        ):
            installer.install(self.home, schedule=True)
        binary = self.home / ".local/bin/tool-catalog"
        self.assertTrue(os.access(binary, os.X_OK))
        self.assertTrue((self.home / ".agents/skills/local-tools/SKILL.md").is_file())
        self.assertTrue((self.home / ".claude/skills/local-tools/SKILL.md").is_file())
        plist = self.home / "Library/LaunchAgents/ai.sawmills.tool-catalog.plist"
        with plist.open("rb") as stream:
            task = plistlib.load(stream)
        self.assertEqual(task["StartInterval"], 3600)
        self.assertEqual(task["ProgramArguments"][1], str(binary))
        self.assertEqual(task["ProgramArguments"][-1], "refresh")
        run.assert_called_once_with(
            ["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)],
            check=True,
        )

    def test_collision_preserves_existing_file_and_writes_nothing_else(self):
        binary = self.home / ".local/bin/tool-catalog"
        binary.parent.mkdir(parents=True)
        binary.write_text("user file")
        with self.assertRaises(FileExistsError):
            installer.install(self.home, schedule=False)
        self.assertEqual(binary.read_text(), "user file")
        self.assertFalse((self.home / ".local/share/tool-catalog").exists())

    def test_without_schedule_never_calls_launchctl(self):
        with mock.patch.object(
            catalog, "refresh", return_value={"tools": []}
        ), mock.patch.object(installer.subprocess, "run") as run:
            installer.install(self.home, schedule=False)
        run.assert_not_called()
        self.assertFalse((self.home / "Library/LaunchAgents").exists())


if __name__ == "__main__":
    unittest.main()
