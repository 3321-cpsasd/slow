#!/usr/bin/env python3
import argparse
import json

from app.core.config import settings
from app.infrastructure.database import build_database
from app.modules.learning.rebuild import rebuild_user_projections


def main():
    parser = argparse.ArgumentParser(
        description="Rebuild one user's learning projections from facts."
    )
    parser.add_argument("user_id")
    arguments = parser.parse_args()
    engine, sessions = build_database(settings.database_url)
    try:
        with sessions() as db:
            report = rebuild_user_projections(
                db,
                user_id=arguments.user_id,
            )
        print(json.dumps(report, ensure_ascii=False))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
