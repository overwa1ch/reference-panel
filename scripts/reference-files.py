#!/usr/bin/env python3
"""Explicit local reference import/classification, using the same API as MCP.

No discovery or recursion. --project and --scope-id must identify the intended
shelf; --paths-json is a JSON array of exact paths. No source file is modified.
"""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/reference-native-test"))
import server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["import", "categorize"])
    parser.add_argument("--project", required=True)
    parser.add_argument("--scope-id", required=True, help="scopeId returned by read_probe_context")
    parser.add_argument("--paths-json", required=True)
    parser.add_argument("--category", default=None)
    args = parser.parse_args()
    try:
        root = Path(args.project)
        if not root.is_absolute() or not root.is_dir():
            raise ValueError("--project 必须是已有项目的绝对目录")
        configured = os.environ.get("REFERENCE_PROJECT_ROOT")
        if configured and Path(configured).resolve() != root.resolve():
            raise ValueError("--project 与配置的项目目录不一致")
        scope = server.ProjectScopes().resolve({"projectRoot": str(root.resolve())})
        paths = json.loads(Path(args.paths_json).read_text(encoding="utf-8"))
        result = server.edit_reference_files(scope, paths, args.scope_id, args.category, args.action == "categorize")
        print(json.dumps({"scopeId": scope["scopeId"], "revision": result["revision"],
                          "total": len(result["references"]), "added": result["added"],
                          "categorized": result["categorized"], "skipped": result["skipped"]}, ensure_ascii=False))
    except (OSError, ValueError, server.ToolError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
