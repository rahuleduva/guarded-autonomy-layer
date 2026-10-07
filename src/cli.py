"""CLI migrations, synthetic evaluation harness, and explicit cloud seeding."""
import argparse
import json
from alembic import command
from alembic.config import Config
from src.config import PROJECT_ROOT


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["init-db", "run-all-scenarios", "seed-advisories", "calibrate-semantic"])
    args = parser.parse_args()
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
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
