"""Run one test command on GitHub and, if it fails, show the end of its output
as error notes ("annotations") on the pull request page.

Without this, a failed check only says "exit code 1" and you have to dig
through the full log. Locally there's no need for it: just run the command.

    python tests/ci_run.py <command> [arguments...]
"""
from __future__ import annotations

import subprocess
import sys
from collections import deque

LINES_SHOWN = 150      # how much of the end of the output to show
LINES_PER_NOTE = 50    # GitHub shows at most 10 error notes per step
TITLE = "Last lines of the output"


def escape(text: str) -> str:
    # GitHub's workflow-command format: these characters must be encoded.
    return text.replace("%", "%25").replace("\r", "").replace("\n", "%0A")


def main(command: list) -> int:
    if not command:
        print(__doc__)
        return 2
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows runners default to cp1252
    tail: deque = deque(maxlen=LINES_SHOWN)
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace", bufsize=1)
    assert process.stdout is not None
    for line in process.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        tail.append(line.rstrip("\n"))
    code = process.wait()
    if code != 0:
        lines = list(tail)
        chunks = [lines[i:i + LINES_PER_NOTE] for i in range(0, len(lines), LINES_PER_NOTE)] or [[]]
        for number, chunk in enumerate(chunks, 1):
            title = f"{TITLE} ({number} of {len(chunks)}) - exit code {code}"  # no commas or colons allowed here
            print(f"::error title={title}::" + escape("\n".join(chunk)), flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
