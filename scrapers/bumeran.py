"""
Bumeran scraper — REST API (Navent network, x-site-id: BMAR).
Same API structure as ZonaJobs, different site ID and domain.
No login needed for browsing. No Playwright required.
"""
from typing import Optional

import requests
import structlog

from core.models import Job
from scrapers.base import BaseScraper

logger = structlog.get_logger()

BASE_URL = "https://www.bumeran.com.ar"
SEARCH_URL = f"{BASE_URL}/api/avisos/searchV2"

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Origin": BASE_URL,
    "Referer": BASE_URL + "/",
    "x-site-id": "BMAR",
}

SEARCH_QUERIES = [
    ("desarrollador", "tecnologia-sistemas-y-telecomunicaciones"),
    ("programador",   "tecnologia-sistemas-y-telecomunicaciones"),
    ("full stack",    "tecnologia-sistemas-y-telecomunicaciones"),
    ("python",        "tecnologia-sistemas-y-telecomunicaciones"),
    ("java",          "tecnologia-sistemas-y-telecomunicaciones"),
]

PAGE_SIZE = 20


class BumeranScraper(BaseScraper):
    platform_name = "bumeran"

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
                logger.debug("bumeran.query_done", keyword=keyword, found=len(jobs))
            except Exception as e:
                logger.error("bumeran.query_error", keyword=keyword, error=str(e))

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
        # Skip crossbrand listings — ZonaJobs jobs appear in Bumeran results
        if raw.get("portal", "bumeran") != "bumeran":
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
