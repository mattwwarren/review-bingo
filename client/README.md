# client

`bingo_client.py` — the thin CLI a grid member runs. Single file, no install:
`uv run client/bingo_client.py ...` (PEP 723 inline deps).

```
# once
uv run client/bingo_client.py register --hub http://hub:7575 \
    --name marge-mac-mini --model qwen2.5-coder-32b --provider ollama \
    --quant q4_K_M --tier standard

# when you have compute to spare
uv run client/bingo_client.py check-in
uv run client/bingo_client.py loop          # serve rounds until Ctrl-C
uv run client/bingo_client.py check-out     # compute is yours again
```

## Bring your own reviewer

The hub never sees your prompts or review config. Set `REVIEW_CMD` to any
shell command that reads the job as JSON on stdin and writes a report as
JSON on stdout:

```json
{"verdict": "findings", "summary": "markdown...", "findings": [{"file": "...", "line": 1, "title": "..."}]}
```

Point it at `claude -p`, an ollama wrapper, a cheap-fix/expensive-review
two-model loop — your compute, your call. Without `REVIEW_CMD` the client
submits a clearly-labelled canned report so the loop can be exercised
offline (that's what `scripts/demo.sh` does).

Registration state (hub URL + bearer token) lands in
`~/.config/review-bingo/client.json` (`--state` to override).
