"""Print local stdio MCP configuration. Does not edit any host settings."""
import json
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
print(json.dumps({'mcpServers': {'reference-panel': {
    'command': sys.executable,
    'args': [str(root / 'plugins/reference-native-test/server.py')],
}}}, ensure_ascii=False, indent=2))
