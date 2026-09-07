"""
Thin client for the Crystal Sports booking backend.

Reverse-engineered from booking.php (Sept 2026):

  GET  api_helper.php?action=getLocations
       -> [{"locId":"LOC001","locName":"Crystal Sports"}, {"locId":"LOC002","locName":"Crystal Sports G"}]

  GET  api_helper.php?action=getStadiums
       -> [{"stadiumId":"1","stadiumName":"North-1","locId":"LOC001","locName":"Crystal Sports","stadiumSort":3}, ...]

  POST api_helper.php?action=getAvailableStadiums   (JSON body)
       {"date":"YYYY-MM-DD","stadiumId":<id|null>,"locId":<"LOC00x"|null>}
       -> [{"stadiumtimeId":1,"stadiumId":1,"timeId":1,"timeStart":"06:00:00","timeEnd":"07:00:00",
            "stadiumName":"North-1","timeName":"06:00","stadiumtimePrice":"500.0000",
            "reservestatus":"1","locName":"Crystal Sports","locId":"LOC001"}, ...]

Notes verified against the live site:
  * No login / cookie is required for any of the three calls.
  * stadiumId=null AND locId=null returns every court at both venues for that date
    (17 courts x 18 hourly slots 06:00-23:00 = 306 rows) in one request.
  * OMITTING the stadiumId key (instead of sending null) makes the server hang. Always send the key.
  * reservestatus "1" = booked. The site's own UI treats anything else as bookable.
  * The UI exposes today + 14 days. The API answers for later dates too, but those are
    not bookable through the UI, so the watcher stays inside the UI window.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Iterable

import requests

log = logging.getLogger("crystal.api")

BASE_URL = "https://crystalsports-booking.kegroup.co.th"
API = f"{BASE_URL}/api_helper.php"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/booking.php",
    "X-Requested-With": "XMLHttpRequest",
}


@dataclass(frozen=True)
class Slot:
    date: str            # YYYY-MM-DD
    loc_id: str          # LOC001 / LOC002
    loc_name: str        # Crystal Sports / Crystal Sports G
    stadium_id: str      # court id
    stadium_name: str    # North-1, G South-2, ...
    stadiumtime_id: str  # id used by the booking form
    time_start: str      # "06:00"
    time_end: str        # "07:00"
    price: str           # "500.0000"
    reserve_status: str  # "1" = booked

    @property
    def key(self) -> str:
        return f"{self.date}|{self.loc_id}|{self.stadium_id}|{self.time_start}"

    @property
    def is_free(self) -> bool:
        # Mirror the site's own JS: it disables the checkbox only when reservestatus == "1".
        return self.reserve_status != "1"


class CrystalAPI:
    def __init__(self, timeout: float = 30.0, retries: int = 3, backoff: float = 3.0):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff

    # ---- low level -------------------------------------------------------
    def _request(self, method: str, params: dict, json_body: dict | None = None):
        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                r = self.s.request(method, API, params=params, json=json_body, timeout=self.timeout)
                r.raise_for_status()
                data = r.json()
                if not isinstance(data, list):
                    raise ValueError(f"unexpected payload type {type(data).__name__}: {str(data)[:200]}")
                return data
            except Exception as e:  # noqa: BLE001
                last_exc = e
                log.warning("API %s %s attempt %d/%d failed: %s", method, params.get("action"), attempt, self.retries, e)
                if attempt < self.retries:
                    time.sleep(self.backoff * attempt)
        assert last_exc is not None
        raise last_exc

    # ---- public ----------------------------------------------------------
    def get_locations(self) -> list[dict]:
        return self._request("GET", {"action": "getLocations"})

    def get_stadiums(self) -> list[dict]:
        return self._request("GET", {"action": "getStadiums"})

    def get_day(self, date: str, stadium_id: str | None = None, loc_id: str | None = None) -> list[Slot]:
        """All slots for one date. With both ids None -> every court at both venues."""
        raw = self._request(
            "POST",
            {"action": "getAvailableStadiums"},
            {"date": date, "stadiumId": stadium_id, "locId": loc_id},  # keys must be present (null ok)
        )
        return [parse_slot(date, row) for row in raw]


def parse_slot(date: str, row: dict) -> Slot:
    return Slot(
        date=date,
        loc_id=str(row.get("locId", "")),
        loc_name=str(row.get("locName", "")),
        stadium_id=str(row.get("stadiumId", "")),
        stadium_name=str(row.get("stadiumName", "")),
        stadiumtime_id=str(row.get("stadiumtimeId", "")),
        time_start=str(row.get("timeName") or str(row.get("timeStart", ""))[:5]),
        time_end=str(row.get("timeEnd", ""))[:5],
        price=str(row.get("stadiumtimePrice", "")),
        reserve_status=str(row.get("reservestatus", "")),
    )


def summarize(slots: Iterable[Slot]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for s in slots:
        counts[s.reserve_status] = counts.get(s.reserve_status, 0) + 1
    return counts
