"""Endpoint reconnaissance.

Run this FIRST, before writing any loader. It probes each candidate p2b.*
service, saves a sample response to data/samples/, and prints the actual XML
structure it found: element paths, attribute names, and repetition counts.

Nothing downstream should be written against a guessed schema. Run this, read
the output, then build the loaders against what actually came back.

    uv run recon
    uv run recon --votes -40127 -40200   # probe a vote id range
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from xml.etree import ElementTree as ET

from .config import CANDIDATE_SERVICES, SAMPLES_DIR, service_url
from .http import fetch


def strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def describe(root: ET.Element) -> tuple[dict[str, int], dict[str, set[str]]]:
    """Walk the tree, returning element-path counts and attributes per path."""
    counts: Counter[str] = Counter()
    attrs: dict[str, set[str]] = defaultdict(set)

    def walk(el: ET.Element, path: str) -> None:
        here = f"{path}/{strip_ns(el.tag)}"
        counts[here] += 1
        for key in el.attrib:
            attrs[here].add(strip_ns(key))
        for child in el:
            walk(child, here)

    walk(root, "")
    return dict(counts), dict(attrs)


def report(name: str, body: str) -> bool:
    """Parse and print a structural summary. Returns True if it parsed."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        head = body.strip()[:200].replace("\n", " ")
        print(f"  NOT XML ({exc}). First 200 chars: {head}")
        return False

    counts, attrs = describe(root)
    print(f"  root <{strip_ns(root.tag)}>, {len(body)} bytes")
    for path in sorted(counts, key=lambda p: (-counts[p], p))[:25]:
        attr_list = ", ".join(sorted(attrs.get(path, ())))
        suffix = f"  attrs: {attr_list}" if attr_list else ""
        print(f"    {counts[path]:>5} x {path}{suffix}")
    return True


def probe_services(force: bool = False) -> dict[str, bool]:
    results: dict[str, bool] = {}
    for name, params in CANDIDATE_SERVICES.items():
        url = service_url(name, params)  # type: ignore[arg-type]
        print(f"\n=== {name}\n  {url}")
        body = fetch(url, force=force)
        if body is None:
            print("  no response / non-200")
            results[name] = False
            continue

        sample = SAMPLES_DIR / f"{name}.xml"
        sample.write_text(body, encoding="utf-8")
        results[name] = report(name, body)
    return results


def probe_votes(start: int, end: int, force: bool = False) -> None:
    """Probe a range of balsavimo_id values to find where votes actually live.

    IDs are negative and descend. Confirmed working example: -40127.
    """
    step = 1 if end > start else -1
    hits = 0
    for vote_id in range(start, end + step, step):
        url = service_url("ad_sp_balsavimo_rezultatai", {"balsavimo_id": vote_id})
        body = fetch(url, force=force)
        ok = body is not None and body.strip().startswith("<") and len(body) > 400
        if ok:
            hits += 1
        print(f"  {vote_id}: {'HIT ' + str(len(body)) + 'b' if ok else 'miss'}")
    print(f"\n{hits} hits in range {start}..{end}")
    if hits:
        first = SAMPLES_DIR / "vote_sample.xml"
        print(f"Inspect {first} to design the votes table.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Probe Seimas open data endpoints")
    ap.add_argument("--force", action="store_true", help="bypass the cache")
    ap.add_argument(
        "--votes",
        nargs=2,
        type=int,
        metavar=("START", "END"),
        help="probe a balsavimo_id range, e.g. --votes -40127 -40200",
    )
    args = ap.parse_args()

    if args.votes:
        probe_votes(args.votes[0], args.votes[1], force=args.force)
        return 0

    results = probe_services(force=args.force)

    print("\n" + "=" * 60)
    working = [n for n, ok in results.items() if ok]
    broken = [n for n, ok in results.items() if not ok]
    print(f"PARSED OK ({len(working)}): {', '.join(working) or 'none'}")
    print(f"FAILED    ({len(broken)}): {', '.join(broken) or 'none'}")
    print(f"\nSamples written to {SAMPLES_DIR}")
    print("Next: read the samples, then write loaders against the real schema.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
