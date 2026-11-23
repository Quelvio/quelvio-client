"""registry.py — the tools the one MCP server "quelvio" offers, by product. A tool registers with its title and the four MCP hints,
readOnlyHint, destructiveHint, openWorldHint and idempotentHint, or not at all. Hints go to the agent's client in the tool list and are never read to decide what a call may do.

Both products are always on (no memory_on, no knowledge_on): every registered tool is listed and callable wherever the
server is deployed. The contract, quelvio/contract/mcp.json, holds each tool's name, title, hints and input schema, and the sentences."""
import copy
import json
import os
import re
from dataclasses import dataclass, field

CONTRACT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "contract", "mcp.json")
PRODUCTS = ("memory", "knowledge")
HINTS = ("readOnlyHint", "destructiveHint", "openWorldHint", "idempotentHint")
NAME = re.compile(r"(memory|knowledge)_[a-z][a-z0-9_]*")                 # a tool's name: memory_ or knowledge_, then lower case
TYPES = {"string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,), "array": (list,), "object": (dict,)}


class RegistryError(ValueError):
    """A tool that can't be registered; nothing was registered."""


@dataclass(frozen=True)
class Caller:
    """Who is calling, as the key says: the agent, whether its organisation is open (an agreement date and its own store), and what else the wrapper resolved. Never taken from a tool's arguments."""
    agent: str
    open: bool                                   # required: a wiring that forgets it must fail, never count an organisation as open
    context: dict = field(default_factory=dict, compare=False)


def _read(path):
    """A file beside the code, through this module's own loader: from a folder or from a zip alike (the client's .pyz),
    where no path inside the archive can be opened. A loader with no get_data (none that Python ships) falls back to the file."""
    get_data = getattr(globals().get("__loader__"), "get_data", None)
    if get_data is not None: return get_data(path)
    with open(path, "rb") as f: return f.read()


def contract():
    return json.loads(_read(CONTRACT).decode("utf-8"))


def contract_tools():
    return copy.deepcopy(contract()["tools"])


def _check_tool(tool):
    if not isinstance(tool, dict): raise RegistryError("a tool is an object with its name, product, title and four hints")
    product, name = tool.get("product"), tool.get("name")
    if product not in PRODUCTS: raise RegistryError("a tool's product is one of %s" % ", ".join(PRODUCTS))
    if not isinstance(name, str) or not name.startswith(product + "_"): raise RegistryError("a %s tool's name starts with %s_" % (product, product))
    if not NAME.fullmatch(name): raise RegistryError("a tool's name is lower-case words joined by _: %r" % (name,))
    if not isinstance(tool.get("title"), str) or not tool["title"].strip(): raise RegistryError("%s has no title" % name)
    hints = tool.get("annotations")
    if not isinstance(hints, dict): raise RegistryError("%s declares no hint: it needs %s" % (name, ", ".join(HINTS)))
    for h in HINTS:
        if not isinstance(hints.get(h), bool): raise RegistryError("%s needs %s, true or false" % (name, h))


def _check_schema(name, schema):
    if not isinstance(schema, dict) or schema.get("type") != "object" or not isinstance(schema.get("properties"), dict):
        raise RegistryError("%s's input schema is an object with its properties" % name)
    if schema.get("additionalProperties") is not False: raise RegistryError("%s's input schema must refuse other properties" % name)
    missing = [r for r in schema.get("required", []) if r not in schema["properties"]]
    if missing: raise RegistryError("%s's input schema requires what it doesn't define: %s" % (name, ", ".join(missing)))


def check_args(schema, args):
    """The first argument that breaks the tool's schema, by name; None when they all hold. An unknown argument, a missing required one, a
    value of the wrong type (true is not an integer), outside its enum, below its minimum or above its maximum, a string over its maxLength
    or off its pattern, or an array over its maxItems or holding an item of the wrong type, or a string item over the items' maxLength or
    off their pattern (an email address in `people`, memory 5.14)."""
    props = schema["properties"]
    for k in args:
        if k not in props: return k
    for k in schema.get("required", []):
        if k not in args: return k
    for k, v in args.items():
        p = props[k]
        kinds = TYPES.get(p.get("type"), (object,))
        if v is None or not isinstance(v, kinds) or (isinstance(v, bool) and bool not in kinds): return k
        if "enum" in p and v not in p["enum"]: return k
        if "minimum" in p and v < p["minimum"]: return k
        if "maximum" in p and v > p["maximum"]: return k
        if isinstance(v, str) and ("maxLength" in p and len(v) > p["maxLength"] or "pattern" in p and not re.search(p["pattern"], v)): return k
        if isinstance(v, list):
            if "maxItems" in p and len(v) > p["maxItems"]: return k
            items = p.get("items") or {}
            item = TYPES.get(items.get("type"), (object,))
            if any(x is None or not isinstance(x, item) or (isinstance(x, bool) and bool not in item) for x in v): return k
            if any(isinstance(x, str) and ("maxLength" in items and len(x) > items["maxLength"]
                                           or "pattern" in items and not re.search(items["pattern"], x)) for x in v): return k
    return None


class Registry:
    def __init__(self):
        self._tools = {}

    def register(self, tool, schema, handler):
        """One tool: its title and four hints, a closed input schema, and the handler that answers it, handler(args, caller) -> text."""
        _check_tool(tool)
        name = tool["name"]
        _check_schema(name, schema)
        if not callable(handler): raise RegistryError("%s needs a handler" % name)
        if name in self._tools: raise RegistryError("%s is already registered" % name)
        listing = {"name": name, "title": tool["title"], **({"description": tool["description"]} if tool.get("description") else {}),
                   "inputSchema": copy.deepcopy(schema), "annotations": {**{h: tool["annotations"][h] for h in HINTS}, "title": tool["title"]}}
        self._tools[name] = (tool["product"], listing, handler)

    def tools(self):
        """The tool list: every registered tool, in the order registered."""
        return [copy.deepcopy(listing) for product, listing, _ in self._tools.values()]

    def find(self, name):
        return self._tools.get(name) if isinstance(name, str) else None

    def product_of(self, name):
        entry = self._tools.get(name)
        return entry[0] if entry else None

    def call(self, name, args, caller=None):
        """A call without the protocol around it: the handler's text, after the arguments are checked."""
        entry = self.find(name)
        if entry is None: raise KeyError(name)
        _, listing, handler = entry
        bad = check_args(listing["inputSchema"], args)
        if bad: raise ValueError(bad)
        return handler(args, caller)
