"""Print local stdio MCP configuration. Does not edit any host settings."""
import argparse
import json
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--project', type=Path, help='Project folder shared by this MCP server')
args = parser.parse_args()
server = {
    'command': sys.executable,
    'args': [str(root / 'plugins/reference-native-test/server.py')],
}
if args.project:
    server['env'] = {'REFERENCE_PROJECT_ROOT': str(args.project.expanduser().resolve())}
print(json.dumps({'mcpServers': {'reference-panel': server}}, ensure_ascii=False, indent=2))
