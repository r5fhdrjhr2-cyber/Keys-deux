import argparse
import json
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Retrieve FL Keys public records for a parcel")
    parser.add_argument("--mls", help="MLS number")
    parser.add_argument("--parcel", help="Monroe County parcel/folio ID")
    parser.add_argument("--address", help="Street address (used to seed resolution)")
    parser.add_argument("--config", default=str(Path(__file__).parent / "config.yaml"))
    parser.add_argument("--validate", action="store_true", help="Run validation harness")
    args = parser.parse_args()

    if args.validate:
        from keys_records.validation import run_validation
        result = run_validation(args.config)
        print(json.dumps(result, indent=2, default=str))
        return

    if not any([args.mls, args.parcel, args.address]):
        parser.error("Provide at least one of --mls, --parcel, or --address")

    from keys_records.orchestrator import run
    record = run(
        mls_number=args.mls,
        parcel_id=args.parcel,
        address=args.address,
        config_path=args.config,
    )

    import dataclasses
    print(json.dumps(dataclasses.asdict(record), indent=2, default=str))


if __name__ == "__main__":
    main()
