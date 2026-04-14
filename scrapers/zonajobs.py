"""
ZonaJobs scraper — REST API (Navent network, x-site-id: ZJAR).
ZonaJobs is a React SPA; job data comes from POST /api/avisos/searchV2.
No login needed for browsing. No Playwright required.
"""
from typing import Optional

import requests
import structlog

from core.models import Job
from scrapers.base import BaseScraper

logger = structlog.get_logger()

BASE_URL = "https://www.zonajobs.com.ar"
SEARCH_URL = f"{BASE_URL}/api/avisos/searchV2"

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Origin": BASE_URL,
    "Referer": BASE_URL + "/",
    "x-site-id": "ZJAR",
}

# (query_keyword, area_filter_value or None)
# area "tecnologia-sistemas-y-telecomunicaciones" = id 19 in Navent
SEARCH_QUERIES = [
    ("desarrollador", "tecnologia-sistemas-y-telecomunicaciones"),
    ("programador",   "tecnologia-sistemas-y-telecomunicaciones"),
    ("full stack",    "tecnologia-sistemas-y-telecomunicaciones"),
    ("python",        "tecnologia-sistemas-y-telecomunicaciones"),
    ("java",          "tecnologia-sistemas-y-telecomunicaciones"),
]

PAGE_SIZE = 20


class ZonaJobsScraper(BaseScraper):
    platform_name = "zonajobs"

    def fetch_jobs(self) -> list[dict]:
        all_jobs: list[dict] = []
        seen: set[str] = set()

        for keyword, area in SEARCH_QUERIES:
            try:
                jobs = self._fetch_query(keyword, area)
                for job in jobs:
                    jid = str(job.get("id", ""))
                    if jid and jid not in seen:
                        seen.add(jid)
                        all_jobs.append(job)
                logger.debug("zonajobs.query_done", keyword=keyword, found=len(jobs))
            except Exception as e:
                logger.error("zonajobs.query_error", keyword=keyword, error=str(e))

        return all_jobs

    def _fetch_query(self, keyword: str, area: Optional[str]) -> list[dict]:
        filtros = []
        if area:
            filtros.append({"id": "area", "value": area})

        body = {"filtros": filtros, "query": keyword}
        params = {"pageSize": PAGE_SIZE, "page": 0, "sort": "RELEVANTES"}

        resp = requests.post(
            SEARCH_URL,
            json=body,
            params=params,
            headers=HEADERS,
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json().get("content", [])

    def parse_job(self, raw: dict) -> Optional[Job]:
        # Skip crossbrand listings — Bumeran jobs appear in ZonaJobs results
        if raw.get("portal", "zonajobs") != "zonajobs":
            return None

        external_id = str(raw.get("id", ""))
        title = (raw.get("titulo") or "").strip()

        if not external_id or not title:
            return None

        url = f"{BASE_URL}/empleos/{external_id}"

        empresa = raw.get("empresa", "")
        company = empresa.get("nombre", "") if isinstance(empresa, dict) else str(empresa)

        loc = raw.get("localizacion", "")
        location = str(loc) if loc else ""

        modal_raw = (raw.get("modalidadTrabajo") or "").lower()
        if "remoto" in modal_raw or "teletrabajo" in modal_raw:
            modality = "remoto"
        elif "híbrido" in modal_raw or "hibrido" in modal_raw:
            modality = "hibrido"
        else:
            modality = "presencial"

        description = (raw.get("detalle") or "")[:1000]

        return Job(
            external_id=external_id,
            title=title,
            company=company,
            location=location,
            url=url,
            description=description,
            modality=modality,
            status="PENDING",
        )
