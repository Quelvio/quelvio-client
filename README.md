# quelvio

Quelvio's agent memory client: the hooks Claude Code runs and the MCP server it talks to, on your machine.

    pipx install quelvio==2.0.0        # or: uv tool install quelvio==2.0.0
    quelvio setup claude-code --agent <agent>       # asks for the agent's key once, checks it, keeps it in your keychain
    quelvio check --agent <agent>
    quelvio --version

Standard library only. The key goes to your keychain (or QUELVIO_KEY where the machine has none), never to a file, an argument or a log.
Licensed under the Apache License 2.0 (LICENSE).
The hooks and the MCP entry run this package's python isolated (-I -S), so nothing on PYTHONPATH, in a .pth file or in the folder they start
in runs beside it.
Its files live in ~/.config/quelvio and ~/.local/state/quelvio. quelvio reads no server address and keeps nothing through a linked folder
there (as GNU stow makes), so whoever can change where a link leads can't choose where your key goes. With ~/.config linked and no
address behind it, Quelvio's own address is used. `quelvio check` says when a folder there is a link.
