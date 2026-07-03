"""Application Insights / Log Analytics KQL client.

Two modes:
- Real mode: hits https://api.applicationinsights.io/v1/apps/{appId}/query
  with an API key. Used in production.
- CSV fallback: reads a local CSV file. Used for local development and CI.

Both return the same list[RawRow]. Downstream nodes do not know which
mode ran.

KQL shape used in real mode (for reference):

    requests
    | where timestamp between (startOfWeek .. endOfWeek)
    | extend partnerId   = tostring(customDimensions.partnerId),
             region      = tostring(customDimensions.region),
             gateway     = tostring(customDimensions.gateway),
             resultCode  = tostring(customDimensions.resultCode)
    | where partnerId in ({partners})
    | project timestamp, partnerId, region, gateway, success, resultCode,
              latency_ms = duration
"""

from __future__ import annotations

import csv
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx

from ..state import RawRow

log = logging.getLogger(__name__)


class AppInsightsClient:
    """Queries App Insights via REST. Falls back to CSV when not configured."""

    def __init__(
        self,
        *,
        app_id: str | None = None,
        api_key: str | None = None,
        csv_path: str | Path | None = None,
    ) -> None:
        self.app_id = app_id or os.getenv("APPINSIGHTS_APP_ID", "")
        self.api_key = api_key or os.getenv("APPINSIGHTS_API_KEY", "")
        self.csv_path = Path(csv_path) if csv_path else None
        self._http: httpx.AsyncClient | None = None

    def is_configured(self) -> bool:
        return bool(self.app_id and self.api_key)

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def fetch_rows(
        self,
        week_start: datetime,
        week_end: datetime,
        partners: list[str],
    ) -> list[RawRow]:
        """Return RawRow list. Real KQL if configured, else CSV."""
        if self.is_configured():
            log.info("appinsights.mode=real")
            return await self._fetch_kql(week_start, week_end, partners)
        if self.csv_path and self.csv_path.exists():
            log.info("appinsights.mode=csv path=%s", self.csv_path)
            return self._fetch_csv(week_start, week_end, partners)
        raise RuntimeError(
            "No data source configured: set APPINSIGHTS_APP_ID + "
            "APPINSIGHTS_API_KEY, or provide a csv_path argument."
        )

    # ---- Real KQL mode ----

    async def _fetch_kql(
        self,
        week_start: datetime,
        week_end: datetime,
        partners: list[str],
    ) -> list[RawRow]:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=30.0)
        partner_list = ", ".join(f"'{p}'" for p in partners)
        query = (
            "requests "
            f"| where timestamp between (datetime({week_start.isoformat()}) "
            f".. datetime({week_end.isoformat()})) "
            "| extend partnerId  = tostring(customDimensions.partnerId), "
            "         region     = tostring(customDimensions.region), "
            "         gateway    = tostring(customDimensions.gateway), "
            "         resultCode = tostring(customDimensions.resultCode) "
            f"| where partnerId in ({partner_list}) "
            "| project timestamp, partnerId, region, gateway, success, "
            "          resultCode, latency_ms = duration"
        )
        url = (
            f"https://api.applicationinsights.io/v1/apps/{self.app_id}/query"
        )
        headers = {"x-api-key": self.api_key}
        resp = await self._http.post(url, headers=headers, json={"query": query})
        resp.raise_for_status()
        tables = resp.json().get("tables", [])
        if not tables:
            return []
        columns = [c["name"] for c in tables[0].get("columns", [])]
        rows: list[RawRow] = []
        for r in tables[0].get("rows", []):
            d = dict(zip(columns, r))
            rows.append(
                RawRow(
                    timestamp=datetime.fromisoformat(
                        d["timestamp"].replace("Z", "+00:00")
                    ),
                    partner_id=str(d["partnerId"]),
                    region=str(d["region"]),
                    gateway=str(d["gateway"]),
                    success=bool(d["success"]),
                    result_code=str(d["resultCode"]),
                    latency_ms=int(d.get("latency_ms") or 0),
                )
            )
        return rows

    # ---- CSV fallback mode ----

    def _fetch_csv(
        self,
        week_start: datetime,
        week_end: datetime,
        partners: list[str],
    ) -> list[RawRow]:
        assert self.csv_path is not None
        partner_set = set(partners)
        rows: list[RawRow] = []
        with self.csv_path.open() as f:
            reader = csv.DictReader(f)
            for row in reader:
                if partner_set and row["partner_id"] not in partner_set:
                    continue
                ts = datetime.fromisoformat(row["timestamp"]).replace(
                    tzinfo=timezone.utc
                )
                if ts < week_start or ts >= week_end:
                    continue
                rows.append(
                    RawRow(
                        timestamp=ts,
                        partner_id=row["partner_id"],
                        region=row["region"],
                        gateway=row["gateway"],
                        success=row["success"].lower() == "true",
                        result_code=row["result_code"],
                        latency_ms=int(row.get("latency_ms") or 0),
                    )
                )
        return rows


__all__ = ["AppInsightsClient"]