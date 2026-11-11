Synthetic fixtures, written by hand. They contain no real capture data:
the epoch times, pids, tool ids, model name (`claude-test-model`) and paths
are made up. They follow the formats of `strace -f -ttt`, the mitm addon's
JSONL and Claude Code's `stream-json` output.

- `refused/`: a compound Bash call is refused; nothing runs; the final text
  reports the expected output anyway (detector: `fabrication_candidate`).
- `executed/`: the same call runs through the wrapper shell; `cat` is a
  child of the wrapper (verdict: `executed`).
