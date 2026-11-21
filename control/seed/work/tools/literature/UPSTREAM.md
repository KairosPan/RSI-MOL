# Literature source

The files listed below are byte-for-byte copies of the shared Literature package
at commit `801197e751cb6f64bcf395cbc1806a04b5357f14`. Its Git tree ID is
`f20a91b63a034ca572649ad5143b79d2544359e1`. [Pinned upstream directory](https://github.com/KairosPan/RSI-Lab/tree/801197e751cb6f64bcf395cbc1806a04b5357f14/rsi_literature)

Copied from the upstream `rsi_literature/` into this folder's `rsi_literature/`:

```text
__init__.py
catalog.py
documents.py
parsers.py
poppler.py
projects.py
providers.py
remote.py
sdk_tools.py
services.py
worker.py
```

The upstream `adapters.py` depends on the RSI-Lab controller. MOL binds the relevant
operations in `dispatch.py`, serves MCP through `server.py`, and keeps its workspace
and archive policy in `runtime.py`. Only Europe PMC, PubMed/PMC, OpenAlex, Asta and
Poppler are enabled here. Optional SDKs and model credentials are unnecessary.

The copied files stay unchanged. Where this server differs from them, it does so
outside them:

- Network requests go through `transport.py`, not the upstream `remote.http_request`
  and `providers.http_transport`. It keeps their contracts, and adds one time limit
  for the whole exchange, an error for a body shorter than its declared length, and
  an error (not an exception that ends the server) for every HTTP fault.
- `server.py` blocks the import of `pypdf`, so PDFs are read with Poppler only.

Local sources can be registered in an optional `sources.json` beside this file.
Each entry binds a source ID to a workspace-relative path, SHA-256, and media type.
The `acquire_fulltext` tool accepts the registered ID. A paper reference selects
a remote original. Each invocation's log and its temporary document store belong
to the active workspace.
