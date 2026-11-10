"""Add new hearings: download captions and dockets, split cases, build chunks, embed them.

Usage:
    dmr-update              # everything new since the last run
    dmr-update --limit 3    # only scrape 3 new hearings (for a quick test)

Each step is a module and can also be run alone, e.g. python -m dmr_hearings.pipeline.cases.
Stop the MCP server (close the chat app) first if a step reports the database is locked.
"""

import argparse
import subprocess
import sys


def run(step: str, *args: str) -> None:
    print(f"\n=== {step} {' '.join(args)}".rstrip(), flush=True)
    subprocess.run([sys.executable, "-m", f"dmr_hearings.pipeline.{step}", *args], check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, help="only scrape this many new hearings")
    args = parser.parse_args()

    run("scrape", *(["--limit", str(args.limit)] if args.limit else []))
    run("dockets")  # before cases, which uses the docket to tell which case numbers are real
    run("cases")
    run("chunks")  # skips hearings whose captions and cases haven't changed
    run("embed")  # rebuilds keyword indexes and embeds only the new chunks
    print("\nUpdate complete.")


if __name__ == "__main__":
    main()
