"""Corpus generation: clone repos, inject bugs, run the agent against them."""

from __future__ import annotations

import argparse
import sys

from . import backend, fidelity, repos, runner, tasks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bench", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    setup = sub.add_parser("setup", help="Clone the pinned repos and build their environment.")
    setup.add_argument("--force", action="store_true", help="Re-clone and rebuild from scratch.")

    build = sub.add_parser("build-tasks", help="Inject bugs and write the task manifest.")
    build.add_argument("--per-repo", type=int, default=4)
    build.add_argument("--seed", type=int, default=20260729)

    run = sub.add_parser("run", help="Run the agent over the manifest and record every attempt.")
    run.add_argument("--runs", type=int, default=5, help="Attempts per task.")
    run.add_argument("--limit", type=int, default=None, help="Use only the first N tasks.")
    run.add_argument("--max-steps", type=int, default=18)
    run.add_argument("--temperature", type=float, default=0.7)
    run.add_argument(
        "--model",
        default=backend.DEFAULT_MODEL,
        help=f"Model id. Larger option: {backend.LARGER_MODEL}",
    )
    run.add_argument("--shard", type=int, default=0, help="Which shard this worker runs.")
    run.add_argument("--shards", type=int, default=1, help="How many workers share the job.")

    sub.add_parser("status", help="Outcome rates and how many tasks came out mixed.")
    sub.add_parser("verify", help="Check every pinned repo is green on an untouched checkout.")
    sub.add_parser(
        "divergence", help="How far two runs of the same task agree before they part."
    )

    args = parser.parse_args(argv)

    match args.command:
        case "setup":
            repos.setup(force=args.force)
            print(f"{len(repos.REPOS)} repositories ready under {repos.CLONE_ROOT}")
        case "verify":
            return repos.main(["--force"] if getattr(args, "force", False) else [])
        case "build-tasks":
            built = tasks.build(per_repo=args.per_repo, seed=args.seed)
            path = tasks.save(built)
            by_operator: dict[str, int] = {}
            for task in built:
                by_operator[task.operator] = by_operator.get(task.operator, 0) + 1
            print(f"{len(built)} tasks written to {path}")
            for operator, count in sorted(by_operator.items()):
                print(f"  {operator:<16} {count}")
        case "run":
            runner.batch(
                runs=args.runs,
                limit=args.limit,
                max_steps=args.max_steps,
                temperature=args.temperature,
                model_id=args.model,
                shard=args.shard,
                shards=args.shards,
            )
        case "status":
            print(runner.status())
            print(runner.store_summary())
        case "divergence":
            print(fidelity.report())
    return 0


if __name__ == "__main__":
    sys.exit(main())
