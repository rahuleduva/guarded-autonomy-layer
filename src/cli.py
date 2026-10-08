"""CLI migrations, synthetic evaluation harness, and explicit cloud seeding."""
import argparse
import json
from pathlib import Path
from alembic import command
from alembic.config import Config
from src.config import PROJECT_ROOT


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["init-db", "run-all-scenarios", "seed-advisories", "calibrate-semantic"])
    parser.add_argument("--output", type=Path, help="Save a calibration report as JSON")
    args = parser.parse_args()
    if args.output and args.command != "calibrate-semantic":
        parser.error("--output is only supported for calibrate-semantic")
    if args.command == "init-db":
        config = Config(str(PROJECT_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
        command.upgrade(config, "head")
    elif args.command == "run-all-scenarios":
        from src.evaluation import run_all_scenarios
        print(json.dumps(run_all_scenarios(), indent=2))
    else:
        from src.services.vector_setup import calibrate, seed
        result = seed() if args.command == "seed-advisories" else calibrate()
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
