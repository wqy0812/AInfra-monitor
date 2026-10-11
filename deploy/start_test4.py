"""First install only; shared pinned-image central startup. Execute via SSH MCP."""
from start import central,api_clients

central(api_clients())
