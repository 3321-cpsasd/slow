"""Repair published legacy rank identities; dry-run unless --apply is given."""

import argparse
import json

from app.core.config import settings
from app.infrastructure.database import build_database
from app.modules.learning.historical_rank_repair import (
    repair_published_historical_rank_identities,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="建立历史能力目标的段位身份映射并重放学习画像",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="提交修复；默认仅执行事务内演练并回滚",
    )
    arguments = parser.parse_args()
    engine, sessions = build_database(settings.database_url)
    try:
        with sessions() as db:
            summary = repair_published_historical_rank_identities(db)
            if arguments.apply:
                db.commit()
            else:
                db.rollback()
            print(
                json.dumps(
                    {**summary, "mode": "applied" if arguments.apply else "dry_run"},
                    ensure_ascii=False,
                    indent=2,
                )
            )
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
