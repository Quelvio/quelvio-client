"""quelvio/mcp — the one MCP server "quelvio": the registry, where every tool declares its title and four hints; the server over
streamable HTTP; the stdio shim; the sentences a tool returns on a failure; and the fake. The contract is quelvio/contract/mcp.json."""
from quelvio.mcp.errors import Refused, ToolFailure, sentences
from quelvio.mcp.registry import Caller, Registry, RegistryError, contract, contract_tools

__all__ = ["Caller", "Refused", "Registry", "RegistryError", "ToolFailure", "contract", "contract_tools", "sentences"]
