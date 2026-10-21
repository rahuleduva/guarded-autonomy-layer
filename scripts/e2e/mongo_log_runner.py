"""Run a command while streaming its redacted console output directly to MongoDB.

No local log file is created. Each output line is stored as a document in the
dedicated guarded-autonomy-layer-e2e-scratch-logs collection. The final record
contains a digest and a read-back verification result.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone

from pymongo import AsyncMongoClient

from scripts.e2e.initialize import secret_values
from scripts.e2e.reporting import redact, run_name
from src.config import settings


COLLECTION = "guarded-autonomy-layer-e2e-scratch-logs"


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


async def run(run_id: str, invocation_id: str, command: list[str]) -> int:
    if not command:
        raise ValueError("command is required after --")

    secrets = secret_values()
    client = AsyncMongoClient(
        settings.MONGODB_CONNECTION_STRING.get_secret_value(),
        serverSelectionTimeoutMS=10000,
        connectTimeoutMS=10000,
        socketTimeoutMS=20000,
        timeoutMS=30000,
        appname="guarded-autonomy-layers-e2e-console-logger",
    )
    process = None
    collection = None
    metadata_id = None
    owns_metadata = False
    finalized = False
    try:
        await client.admin.command("ping")
        collection = client[settings.MONGODB_DATABASE][COLLECTION]
        key = f"{run_id}/{invocation_id}"
        metadata_id = key + "/metadata"
        if await collection.find_one({"_id": metadata_id}, {"_id": 1}):
            raise ValueError("a console log already exists for this run invocation")

        started_at = timestamp()
        await collection.insert_one({
            "_id": metadata_id,
            "run_id": run_id,
            "invocation_id": invocation_id,
            "artifact_type": "runner-console-log",
            "status": "RUNNING",
            "started_at": started_at,
            "redacted": True,
            "line_count": 0,
        })
        owns_metadata = True

        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True,
        )
        interrupted = False

        def interrupt_child(_signum, _frame):
            nonlocal interrupted
            interrupted = True
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGINT)
                except ProcessLookupError:
                    pass

        previous_handlers = {
            sig: signal.signal(sig, interrupt_child)
            for sig in (signal.SIGINT, signal.SIGTERM)
        }
        digest = hashlib.sha256()
        byte_count = 0
        line_count = 0
        try:
            assert process.stdout is not None
            while True:
                line = await asyncio.to_thread(process.stdout.readline)
                if not line:
                    break
                clean = redact(line, secrets)
                encoded = clean.encode("utf-8")
                digest.update(encoded)
                byte_count += len(encoded)
                line_id = f"{key}/line/{line_count:06d}"
                await collection.insert_one({
                    "_id": line_id,
                    "run_id": run_id,
                    "invocation_id": invocation_id,
                    "artifact_type": "runner-console-line",
                    "line_index": line_count,
                    "content": clean,
                    "redacted": True,
                    "captured_at": timestamp(),
                })
                line_count += 1
                await collection.update_one({"_id": metadata_id}, {"$set": {
                    "line_count": line_count,
                    "last_line_at": timestamp(),
                }})
                sys.stdout.write(clean)
                sys.stdout.flush()
            return_code = await asyncio.to_thread(process.wait)
        except BaseException:
            if process.poll() is None:
                interrupt_child(signal.SIGINT, None)
                await asyncio.to_thread(process.wait)
            raise
        finally:
            for sig, previous_handler in previous_handlers.items():
                signal.signal(sig, previous_handler)

        status = "INTERRUPTED" if interrupted else "COMPLETED" if return_code == 0 else "FAILED"
        log_hash = digest.hexdigest()
        lines = await collection.find(
            {"run_id": run_id, "invocation_id": invocation_id,
             "artifact_type": "runner-console-line"},
            {"_id": 0, "line_index": 1, "content": 1},
        ).sort("line_index", 1).to_list(length=None)
        restored = "".join(row["content"] for row in lines)
        restored_bytes = restored.encode("utf-8")
        readback_hash = hashlib.sha256(restored_bytes).hexdigest()
        verified = len(lines) == line_count and readback_hash == log_hash
        await collection.update_one({"_id": metadata_id}, {"$set": {
            "status": status,
            "finished_at": timestamp(),
            "return_code": return_code,
            "line_count": line_count,
            "byte_count": byte_count,
            "sha256": log_hash,
            "readback_verified": verified,
        }})
        if not verified:
            raise RuntimeError("MongoDB console-log read-back verification failed")
        finalized = True
        print(json.dumps({
            "mongo_collection": COLLECTION,
            "record_id": metadata_id,
            "status": status,
            "return_code": return_code,
            "line_count": line_count,
            "byte_count": byte_count,
            "sha256": log_hash,
            "readback_verified": verified,
        }), flush=True)
        return return_code if return_code >= 0 else 128 - return_code
    except BaseException as exc:
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            await asyncio.to_thread(process.wait)
        if owns_metadata and collection is not None and metadata_id is not None:
            try:
                update = {"finished_at": timestamp(),
                          "logging_error_type": type(exc).__name__}
                if not finalized:
                    update["status"] = (
                        "INTERRUPTED" if isinstance(exc, KeyboardInterrupt)
                        else "LOGGER_FAILED"
                    )
                await collection.update_one({"_id": metadata_id}, {"$set": update})
            except Exception:
                pass
        raise
    finally:
        if process is not None and process.stdout is not None:
            process.stdout.close()
        await client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", type=run_name, required=True)
    parser.add_argument("--invocation-id", type=run_name, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="command to run, preceded by --")
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    return asyncio.run(run(args.run_id, args.invocation_id, command))


if __name__ == "__main__":
    raise SystemExit(main())
