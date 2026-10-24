from __future__ import annotations

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(description="V2G hackathon orchestration")
    parser.add_argument("--stage", choices=["ingest", "forecast", "optimise", "fleet", "grid", "econ", "sweep"], default="forecast")
    args = parser.parse_args()
    print(f"V2G stage selected: {args.stage}")


if __name__ == "__main__":
    main()
