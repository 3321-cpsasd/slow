"""Validate or import a versioned curriculum-baseline candidate."""

import argparse
from pathlib import Path

from app.core.config import settings
from app.infrastructure.database import build_database
from app.modules.curriculum.baselines import CurriculumBaselineService


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("package", type=Path)
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate schema and closed references without writing the database.",
    )
    parser.add_argument(
        "--review",
        type=Path,
        help="Apply a separate human review manifest to the frozen candidate.",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Publish after importing and applying the approved review manifest.",
    )
    args = parser.parse_args()
    if args.publish and not args.review:
        parser.error("--publish requires --review")

    package = CurriculumBaselineService.read_package(args.package)
    review = (
        CurriculumBaselineService.read_review(args.review)
        if args.review
        else None
    )
    if args.validate_only:
        if review and (
            review.baseline_key != package.baseline_key
            or review.baseline_version != package.version
            or review.baseline_content_hash != package.content_hash()
        ):
            parser.error("review manifest does not match the frozen candidate")
        print(
            f"valid candidate: {package.baseline_key} v{package.version} "
            f"({len(package.objectives)} objectives, {len(package.concepts)} concepts, "
            f"{len(package.gaps)} explicit gaps)"
        )
        if review:
            print(
                f"valid review: reviewer={review.reviewer_id}; "
                f"decision={review.final_decision}; "
                f"{len(review.relations)} relation decisions"
            )
        return

    _engine, session_factory = build_database(settings.database_url)
    with session_factory() as db:
        service = CurriculumBaselineService(db)
        baseline = service.import_candidate(package)
        if review:
            baseline = service.apply_review(baseline.id, review)
        if args.publish and review:
            baseline = service.publish(
                baseline.id,
                reviewer_id=review.reviewer_id,
                review_note="Published from the frozen human review manifest.",
            )
        print(
            f"imported baseline {baseline.id}; status={baseline.status}; "
            f"review={'applied' if review else 'not-applied'}"
        )


if __name__ == "__main__":
    main()
