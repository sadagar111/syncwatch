"""Tries the real YouTube with SyncWatch's resolver: information for the GitHub checks page.

    python tests/youtube_live.py [--runtime]

YouTube often answers GitHub's machines with "Sign in to confirm you're not a bot", so a refusal
is only a notice here. What does fail: yt-dlp or Deno missing (the installation is broken), or
--runtime and deno.exe isn't inside the portable runtime.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from syncwatch.ui.youtube_ui import find_js_runtime  # noqa: E402
from syncwatch.youtube import resolve  # noqa: E402

VIDEOS = ("aqz-KE-bpKQ", "jNQXAC9IVRw")   # Big Buck Bunny (Blender, CC BY) and "Me at the zoo"


def main(argv) -> int:
    deno = find_js_runtime("")
    if deno is None:
        print("::error title=YouTube::Deno isn't installed (the deno package)")
        return 1
    print(f"Deno: {deno}")
    for video in VIDEOS:
        started = time.monotonic()
        result = subprocess.run(resolve.command(video, "--js", f"deno={deno}", "--timeout", "30"),
                                capture_output=True, text=True, timeout=180)
        took = time.monotonic() - started
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        message = json.loads(lines[-1]) if lines else {"error": "other", "message": result.stderr[-500:]}
        if "ok" in message:
            found = message["ok"]
            audio = (found.get("audio") or {}).get("codec", "-")
            print(f"::notice title=YouTube works::{video} '{found['title']}' in {took:.1f} s: "
                  f"{found['height']}p {found['video']['codec']} + {audio}")
            return 0
        kind, text = message.get("error"), message.get("message", "")[:300]
        if kind == "missing":
            print(f"::error title=YouTube::yt-dlp is missing: {text}")
            return 1
        print(f"::notice title=YouTube said no ({kind})::{video} after {took:.1f} s: {text}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
