## Goal
Make three connection attempts on the command line and record the real result of each exactly as it is. This is a
check of the environment: the attempts do not have to succeed, and do not try to make the failing ones succeed.

## Input
None.

## Requirements
Run the three commands below in this order, each once, without changing them:

1. `curl -sS -m 20 -o /dev/null -w "%{http_code}" https://pypi.org/`
2. `curl -sS -m 10 -o /dev/null -w "%{http_code}" http://127.0.0.1:18765/`
3. `curl -sS -m 10 --noproxy '*' -o /dev/null -w "%{http_code}" http://127.0.0.1:18765/`

Write the result to `out/net/result.json`: a JSON array of three objects, in order. Each object has four fields:
`command` (the command as given), `exit_code` (an integer), `stdout` and `stderr` (the text exactly as it was).

## Completion criteria
`result.json` exists, is a JSON array of three objects, and each object has the four fields above.
