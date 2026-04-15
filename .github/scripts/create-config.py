"""Create config.json from environment variables for Electron build."""
import json
import os

KEYS = [
    "SUPABASE_URL", "SUPABASE_ANON_KEY",
    "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
    "NOTION_CLIENT_ID", "NOTION_CLIENT_SECRET",
    "GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET",
    "JIRA_CLIENT_ID", "JIRA_CLIENT_SECRET",
    "LINEAR_CLIENT_ID", "LINEAR_CLIENT_SECRET",
    "ASANA_CLIENT_ID", "ASANA_CLIENT_SECRET",
    "ADMIN_PUBLIC_KEY_PEM", "ANTHROPIC_API_KEY",
]

config = {k: os.environ.get(f"CFG_{k}", "") for k in KEYS}

with open("config.json", "w") as f:
    json.dump(config, f, indent=2)

print(f"config.json created with {sum(1 for v in config.values() if v)} keys set")
