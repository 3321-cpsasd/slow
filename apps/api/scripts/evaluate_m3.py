#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from app.evaluation.m3_acceptance import load_and_evaluate


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate a strict M3 evidence report")
    parser.add_argument("report")
    args = parser.parse_args()
    result = load_and_evaluate(args.report)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
