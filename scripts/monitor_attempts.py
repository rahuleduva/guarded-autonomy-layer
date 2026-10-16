"""Summarize sanitized attempt events; exit 2 when configured rates exceed limits."""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json

from sqlmodel import Session, select

from src.db.database import engine
from src.models.monitoring import RequestAttemptEvent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument("--min-attempts", type=int, default=10)
    parser.add_argument("--max-failure-rate", type=float, default=0.05)
    parser.add_argument("--max-fallback-rate", type=float, default=0.10)
    args = parser.parse_args()
    if args.hours <= 0 or args.min_attempts < 1 or not all(
            0 <= rate <= 1 for rate in (args.max_failure_rate, args.max_fallback_rate)):
        parser.error("hours and min-attempts must be positive; rates must be between 0 and 1")

    since = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    with Session(engine) as session:
        events = session.exec(select(RequestAttemptEvent).where(
            RequestAttemptEvent.created_at >= since)).all()

    attempts = {event.attempt_id for event in events if event.stage == "request"}
    failed = {event.attempt_id for event in events
              if event.stage == "request" and event.status == "failed"}
    fallbacks = {event.attempt_id for event in events
                 if event.stage == "analyzer_primary" and event.status == "failed"}
    errors = Counter((event.stage, event.error_type or "unknown") for event in events
                     if event.status in {"failed", "unavailable"})
    total = len(attempts)
    failure_rate = len(failed) / total if total else 0.0
    fallback_rate = len(fallbacks & attempts) / total if total else 0.0
    alert = total >= args.min_attempts and (
        failure_rate > args.max_failure_rate or fallback_rate > args.max_fallback_rate)
    print(json.dumps({
        "window_hours": args.hours,
        "attempts": total,
        "failed_requests": len(failed),
        "failure_rate": round(failure_rate, 4),
        "fallback_attempts": len(fallbacks & attempts),
        "fallback_rate": round(fallback_rate, 4),
        "stage_errors": [{"stage": stage, "error_type": error_type, "count": count}
                         for (stage, error_type), count in sorted(errors.items())],
        "alert": alert,
    }, indent=2))
    return 2 if alert else 0


if __name__ == "__main__":
    raise SystemExit(main())
