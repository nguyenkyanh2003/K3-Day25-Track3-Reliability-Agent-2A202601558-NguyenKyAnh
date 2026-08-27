from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="reports/quality.json")
    args = parser.parse_args()

    commands = {
        "ruff": [sys.executable, "-m", "ruff", "check", "--no-cache", "src", "tests", "scripts"],
        "mypy": [sys.executable, "-m", "mypy", "--no-incremental", "src", "scripts"],
    }
    evidence: dict[str, dict[str, str | int]] = {}
    failed = False
    for name, command in commands.items():
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        output = (result.stdout + result.stderr).strip()
        evidence[name] = {
            "command": subprocess.list2cmdline(["python", *command[1:]]),
            "returncode": result.returncode,
            "output": output,
        }
        print(f"{name}: returncode={result.returncode}")
        if output:
            print(output)
        failed = failed or result.returncode != 0

    destination = Path(args.out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"wrote {destination}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
