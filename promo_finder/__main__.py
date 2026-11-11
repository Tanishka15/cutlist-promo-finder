"""Run with python3 -m promo_finder."""
from __future__ import annotations
import argparse
from pathlib import Path
import sys

from . import pipeline


def episode_list(value):
    try:
        numbers = [int(part.strip()) for part in value.split(",")]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use comma-separated episode numbers, such as 2,3,6") from exc
    if not numbers or any(number < 1 for number in numbers):
        raise argparse.ArgumentTypeError("Episode numbers must be positive")
    return numbers


def main(argv=None):
    parser = argparse.ArgumentParser(prog="promo_finder", description="Find and review exact continuous promo cuts from timestamped screenplays.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("audit", "benchmark", "run", "render"):
        command = commands.add_parser(name)
        command.add_argument("input", type=Path, help="A screenplay .txt file or folder of screenplays")
        command.add_argument("--out", type=Path, required=True, help="Private output folder, separate from public source")
        if name != "benchmark":
            command.add_argument("--expect-pack", action="store_true", help="Require 50 episodes / 100 scenes / 3,727 entries")
        if name == "run":
            command.add_argument("--episodes", type=episode_list)
            command.add_argument("--provider", choices=("codex", "openai"), default="codex")
            command.add_argument("--model", help="Explicit provider model; required for OpenAI, optional for Codex")
            command.add_argument("--workers", type=int, default=1)
            command.add_argument("--timeout", type=int, default=240, help="Seconds per model request")
            command.add_argument("--retry-failed", action="store_true", help="Create a new immutable attempt for failed cached calls/responses")
    serve = commands.add_parser("serve")
    serve.add_argument("--out", type=Path, required=True)
    serve.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        if args.command == "audit":
            pipeline.audit(args.input, args.out, args.expect_pack)
        elif args.command == "benchmark":
            pipeline.benchmark(args.input, args.out)
        elif args.command == "run":
            result = pipeline.run(args.input, args.out, episodes_requested=args.episodes,
                provider=args.provider, model=args.model, workers=args.workers,
                timeout=args.timeout, retry_failed=args.retry_failed, expect_pack=args.expect_pack)
            return 0 if result["selection_status"] == "complete" else 1
        elif args.command == "render":
            pipeline.render(args.input, args.out, args.expect_pack)
        elif args.command == "serve":
            from .server import serve
            serve(args.out, args.port)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
