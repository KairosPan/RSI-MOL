## Goal

In the original SAMtools paper, which two sections make up the SAM format, and how can a reader tell their lines
apart? Return one supporting passage from the body and one reference actually listed by the paper.

## Input

Li et al., *The Sequence Alignment/Map format and SAMtools*, DOI 10.1093/bioinformatics/btp352.
There are no input files.

## Requirements

Delegate to the native `literature` sub-agent. Have that agent use the local `literature` MCP server to search
Europe PMC by title, identify the target, and acquire its JATS/XML full text. Have it call `read_document` with a
small page (e.g. max_bytes=1200), then use that result's exact next cursor in a second read of the same document.
It may use `find_passages` to locate the answer. Read the supporting body passage with `read_document` before
quoting it. Use `get_citations` in the references direction and return the title of one reference actually
returned. Report any unavailable step. The lead should use the child's results to write the answer.

## Completion criteria

Write `out/lit-service/answer.json` with string fields `answer`, `doi`, `pmcid`, `document_id`,
`document_version`, `source_sha256`, `locator`, `quote`, `reference_title` and `coverage`. Give a verbatim,
contiguous quote from a single body block and its exact block locator. Explain the reading scope in `coverage`.
