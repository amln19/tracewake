"""Corpus generation: clone repos, inject bugs, run the agent against them."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import aligneval, backend, counterfactual, fidelity, label, repos, runner, tasks


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
    sub.add_parser(
        "export-labels",
        help="Write blinded divergence-labeling packets for the evaluation set.",
    )
    lab = sub.add_parser(
        "label", help="Label the blinded packets one at a time, resumable."
    )
    lab.add_argument("--sheet", default="pass1", help="Sheet to fill (default: pass1).")
    lab.add_argument(
        "--shuffle",
        action="store_true",
        help="Present packets in a different order — use this for a second pass.",
    )
    replay = sub.add_parser(
        "replay-fidelity",
        help="Record fresh runs and replay them to measure cassette fidelity.",
    )
    replay_sub = replay.add_subparsers(dest="replay_command", required=True)
    replay_sub.add_parser("record", help="Record N fresh runs into the fidelity store.")
    replay_sub.add_parser("measure", help="Replay those recordings with the network blocked.")
    replay_sub.add_parser("report", help="Print the replay-fidelity number.")
    sub.add_parser(
        "fidelity-gate",
        help="Print both fidelity numbers (run-to-run divergence and replay).",
    )
    align = sub.add_parser(
        "align-eval",
        help="Align the evaluation pairs and score against hand labels.",
    )
    align.add_argument(
        "--lexical",
        action="store_true",
        help="Score reasoning text lexically instead of with the pinned embedder.",
    )
    align.add_argument(
        "--score",
        action="store_true",
        help="Score against a label sheet. Off by default so a labeling pass stays blind.",
    )
    align.add_argument(
        "--labels",
        type=Path,
        default=None,
        help="Label JSONL (default: corpus/labels/pass1.jsonl when --score).",
    )
    sub.add_parser(
        "init-pass2",
        help="Create a blank pass2.jsonl over the existing labeling packets.",
    )
    sub.add_parser(
        "self-agreement",
        help="Report annotator self-agreement between pass1 and pass2.",
    )
    judge = sub.add_parser(
        "llm-judge",
        help="Run the LLM-as-judge baseline on the blinded packets.",
    )
    judge.add_argument(
        "--model",
        default=None,
        help=f"Model id (default: {backend.DEFAULT_MODEL}).",
    )
    sub.add_parser(
        "ablations",
        help="Score pre-specified aligner ablations against pass1 labels.",
    )
    cf = sub.add_parser(
        "intervene",
        help="Re-run a recorded attempt with context blocks dropped, into a new run.",
    )
    cf.add_argument("run", help="Run id or cassette name to fork.")
    cf.add_argument(
        "--drop-tag", action="append", required=True, help="Provenance tag to remove."
    )
    cf.add_argument("--from-step", type=int, default=0, help="First model call to change.")
    cf.add_argument("--max-steps", type=int, default=18)
    cf.add_argument("--temperature", type=float, default=0.7)
    cf.add_argument("--model", default=backend.DEFAULT_MODEL)

    cfd = sub.add_parser(
        "fork-diff", help="Align a forked run against the run it was forked from."
    )
    cfd.add_argument("run", help="Forked run id.")
    cfd.add_argument("--lexical", action="store_true")

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
        case "export-labels":
            dest = label.export_packets()
            n = len(list((dest / "packets").glob("*.md")))
            print(f"{n} blinded packets written under {dest}")
        case "label":
            print(label.label_interactively(sheet=args.sheet, shuffle=args.shuffle))
        case "replay-fidelity":
            match args.replay_command:
                case "record":
                    fidelity.record_replay_arm()
                case "measure":
                    print(fidelity.measure_replay_arm())
                case "report":
                    print(fidelity.replay_report())
        case "fidelity-gate":
            print(fidelity.fidelity_gate())
        case "align-eval":
            print(
                aligneval.predict_and_score(
                    lexical=args.lexical,
                    labels=args.labels,
                    score_labels=args.score,
                )
            )
        case "init-pass2":
            path = aligneval.init_pass_sheet("pass2")
            print(f"pass2 sheet ready at {path}")
        case "self-agreement":
            print(aligneval.self_agreement())
        case "llm-judge":
            print(aligneval.run_llm_judge(model_id=args.model))
        case "ablations":
            print(aligneval.run_ablations())
        case "intervene":
            print(
                counterfactual.fork(
                    args.run,
                    drop_tags=args.drop_tag,
                    from_turn=args.from_step,
                    max_steps=args.max_steps,
                    model_id=args.model,
                    temperature=args.temperature,
                ).format()
            )
        case "fork-diff":
            print(counterfactual.fork_diff(args.run, lexical=args.lexical))
    return 0


if __name__ == "__main__":
    sys.exit(main())
