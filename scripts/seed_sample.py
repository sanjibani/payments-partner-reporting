"""Generate a realistic 2-week sample CSV for local development.

Run via the CLI: `python scripts/run_local.py --seed`.
"""

from __future__ import annotations

import csv
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "sample_week.csv"

PARTNERS = ["P-001", "P-002", "P-003"]
REGIONS = {"P-001": "EU", "P-002": "APAC", "P-003": "LATAM"}
GATEWAYS = ["stripe", "braintree"]
FAILURE_CODES = {
    "stripe": ["200", "card_declined", "insufficient_funds", "3ds_failed"],
    "braintree": ["200", "processor_timeout", "gateway_unavailable", "avs_mismatch"],
}

# Anomaly window: Braintree EU has a transient failure spike.
ANOMALY_START = datetime(2026, 6, 23, 0, 0, tzinfo=timezone.utc)
ANOMALY_END = datetime(2026, 6, 26, 0, 0, tzinfo=timezone.utc)


def _generate_rows() -> list[dict[str, str]]:
    random.seed(42)
    rows: list[dict[str, str]] = []
    start = datetime(2026, 6, 15, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 29, 0, 0, tzinfo=timezone.utc)
    cursor = start
    while cursor < end:
        for partner in PARTNERS:
            for gw in GATEWAYS:
                volume = random.randint(3, 8)
                for _ in range(volume):
                    success_p = 0.95
                    if gw == "braintree":
                        success_p = 0.93
                    if (
                        gw == "braintree"
                        and partner == "P-001"
                        and ANOMALY_START <= cursor < ANOMALY_END
                    ):
                        success_p = 0.55
                    success = random.random() < success_p
                    code = "200" if success else random.choice(FAILURE_CODES[gw][1:])
                    latency = (
                        random.randint(120, 480)
                        if success
                        else random.randint(900, 2500)
                    )
                    rows.append(
                        {
                            "timestamp": cursor.isoformat(),
                            "partner_id": partner,
                            "region": REGIONS[partner],
                            "gateway": gw,
                            "success": str(success).lower(),
                            "result_code": code,
                            "latency_ms": str(latency),
                        }
                    )
        cursor += timedelta(hours=1)
    return rows


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    rows = _generate_rows()
    with OUT.open("w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "timestamp",
                "partner_id",
                "region",
                "gateway",
                "success",
                "result_code",
                "latency_ms",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} rows to {OUT}")


if __name__ == "__main__":
    main()