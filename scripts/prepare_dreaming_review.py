"""Create a local, source-bound Dreaming human-review packet and static page."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memorymaster.dreaming.evaluation import load_jsonl  # noqa: E402
from memorymaster.dreaming.review_packet import build_review_packet  # noqa: E402


def safe_embedded_json(value: object) -> str:
    """Encode JSON safe for a literal embedded in an HTML script element."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).translate(str.maketrans({
        "<": "\\u003c", ">": "\\u003e", "&": "\\u0026",
        "\u2028": "\\u2028", "\u2029": "\\u2029",
    }))


def render_review_html(packet: dict) -> str:
    from memorymaster.surfaces.cohort_review_template import REVIEW_HTML

    marker = '<script type="application/json" id="preloaded-packet">__REVIEW_PACKET_JSON__</script>'
    if marker not in REVIEW_HTML:
        raise ValueError("review template does not contain the packet script marker")
    return REVIEW_HTML.replace(
        marker,
        marker.replace("__REVIEW_PACKET_JSON__", safe_embedded_json(packet)),
        1,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort", type=Path, required=True)
    parser.add_argument("--ai-labels", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("--output must name a new directory")
    manifest_path = args.cohort / "manifest.json"
    sources_path = args.cohort / "sources.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        sources = json.loads(sources_path.read_text(encoding="utf-8"))
        packet = build_review_packet(manifest, sources, load_jsonl(args.ai_labels))
        html = render_review_html(packet)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    args.output.mkdir(parents=True)
    (args.output / "packet.json").write_text(json.dumps(packet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.output / "review.html").write_text(html, encoding="utf-8")
    print(json.dumps({key: packet[key] for key in (
        "schema", "cohort_version", "selected_captures", "total_decisions", "emitted_decisions",
        "required_human_reviews",
    )}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
