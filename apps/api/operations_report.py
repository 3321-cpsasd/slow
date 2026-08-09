import argparse
import json

from app.core.config import settings
from app.infrastructure.database import build_database
from app.operations.report import build_operations_snapshot


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export an internal Slow operations snapshot as JSON.",
    )
    parser.add_argument(
        "--include-identifiers",
        action="store_true",
        help="Include internal user IDs and usernames for the protected operator ledger.",
    )
    arguments = parser.parse_args()
    engine, sessions = build_database(settings.database_url)
    try:
        with sessions() as db:
            snapshot = build_operations_snapshot(
                db,
                include_identifiers=arguments.include_identifiers,
            )
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
