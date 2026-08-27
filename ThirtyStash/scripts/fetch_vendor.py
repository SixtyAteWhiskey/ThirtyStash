#!/usr/bin/env python3
"""Fetch pinned browser assets used by ThirtyStash.

The Docker image runs this during build so barcode scanning has no runtime CDN
dependency. Local non-Docker development can run this script manually.
"""
from __future__ import annotations

import argparse
import pathlib
import time
import urllib.request

QUAGGA2_VERSION = "1.12.1"
QUAGGA2_URLS = [
    f"https://cdn.jsdelivr.net/npm/@ericblade/quagga2@{QUAGGA2_VERSION}/dist/quagga.min.js",
    f"https://unpkg.com/@ericblade/quagga2@{QUAGGA2_VERSION}/dist/quagga.min.js",
]


def fetch(destination: pathlib.Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    errors = []
    for url in QUAGGA2_URLS:
        request = urllib.request.Request(url, headers={"User-Agent": "ThirtyStash-build/1.2"})
        for attempt in range(1, 4):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    payload = response.read()
                if len(payload) < 100_000:
                    raise RuntimeError(f"Downloaded vendor asset is unexpectedly small ({len(payload)} bytes).")
                destination.write_bytes(payload)
                print(f"Fetched Quagga2 {QUAGGA2_VERSION} -> {destination} ({len(payload)} bytes)")
                return
            except Exception as exc:  # build-time helper: keep exact error for diagnostics
                errors.append(f"{url} attempt {attempt}: {exc}")
                if attempt < 3:
                    time.sleep(attempt * 2)
    raise SystemExit("Unable to fetch pinned Quagga2 browser bundle:\n" + "\n".join(errors))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="static/vendor/quagga.min.js")
    args = parser.parse_args()
    fetch(pathlib.Path(args.output))


if __name__ == "__main__":
    main()
