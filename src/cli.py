"""CLI migrations, synthetic evaluation harness, and explicit cloud seeding."""
import argparse
import json
from pathlib import Path
from alembic import command
from alembic.config import Config
from src.config import PROJECT_ROOT


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["init-db", "run-all-scenarios", "seed-advisories", "calibrate-semantic", "validate-semantic-v3", "seed-advisories-v3", "evaluate-semantic-v3"])
    parser.add_argument("--output", type=Path, help="Save a calibration report as JSON")
    parser.add_argument("--held-out", action="store_true", help="Explicitly evaluate the frozen held-out set; never use it for tuning")
    parser.add_argument("--case-id", help="Evaluate only this case ID (semantic evaluation only)")
    parser.add_argument("--suite", choices=["semantic-combined-v1", "semantic-development-v1"], help="Run a named local evaluation suite (combined: 88 cases; development: 56 cases)")
    parser.add_argument("--corpus", type=Path, help="Retrieval corpus JSON, relative to the project root (v3 validation/seeding only)")
    parser.add_argument("--collection", help="Target Qdrant collection (v3 seeding only; does not activate it)")
    args = parser.parse_args()
    if args.held_out and args.command != "evaluate-semantic-v3":
        parser.error("--held-out is only supported for evaluate-semantic-v3")
    if args.output and args.command not in {"calibrate-semantic", "evaluate-semantic-v3"}:
        parser.error("--output is supported for calibration and semantic evaluation")
    if args.case_id and args.command != "evaluate-semantic-v3":
        parser.error("--case-id is only supported for evaluate-semantic-v3")
    if args.suite and args.command != "evaluate-semantic-v3":
        parser.error("--suite is only supported for evaluate-semantic-v3")
    if args.suite and args.held_out:
        parser.error("--suite and --held-out cannot be used together")
    if args.corpus and args.command not in {"validate-semantic-v3", "seed-advisories-v3"}:
        parser.error("--corpus is only supported for v3 validation and seeding")
    if args.collection is not None and args.command != "seed-advisories-v3":
        parser.error("--collection is only supported for seed-advisories-v3")
    if args.command == "init-db":
        config = Config(str(PROJECT_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
        command.upgrade(config, "head")
    elif args.command == "run-all-scenarios":
        from src.evaluation import run_all_scenarios
        print(json.dumps(run_all_scenarios(), indent=2))
    elif args.command in {"validate-semantic-v3", "seed-advisories-v3", "evaluate-semantic-v3"}:
        from src.services.semantic_v3_corpus import load, seed, validate_evaluation_overlap
        if args.command == "validate-semantic-v3":
            corpus, evaluation = load(path=args.corpus), load("evaluation")
            validate_evaluation_overlap(corpus)
            result = {"corpus_count":len(corpus.cases), "evaluation_count":len(evaluation.cases),
                      "corpus_hash":corpus.fingerprint, "evaluation_hash":evaluation.fingerprint,
                      "review_status":corpus.review_status}
        elif args.command == "seed-advisories-v3":
            result = seed(corpus_path=args.corpus, collection=args.collection)
        else:
            from src.services.semantic_evaluation import run
            result = run(held_out=args.held_out, case_id=args.case_id, suite=args.suite)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
    else:
        from src.services.vector_setup import calibrate, seed
        result = seed() if args.command == "seed-advisories" else calibrate()
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
