"""Run the rollback_replay group through the common suite runner."""
from scripts.e2e.run import main

if __name__ == "__main__":
    main(default_group="rollback_replay")
