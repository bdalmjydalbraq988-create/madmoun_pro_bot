from __future__ import annotations

import argparse

from app.crypto import PayloadCipher


def main() -> None:
    parser = argparse.ArgumentParser(description="Digital Store Bot utilities")
    parser.add_argument("command", choices=["generate-key"])
    args = parser.parse_args()
    if args.command == "generate-key":
        print(PayloadCipher.generate_key())


if __name__ == "__main__":
    main()
