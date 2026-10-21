"""Public, read-only GitHub and Hugging Face discovery with deterministic ranking."""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import quote
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

from . import db, supabase_store
from .auth import Principal
from .config import Settings, get_settings

Platform = Literal["github", "huggingface"]
HFKind = Literal["model", "dataset", "space"]
Status = Literal["proposed", "saved", "dismissed"]
TOPIC_QUERY = "machine learning deep learning computer vision robotics autonomous systems perception reproducible research"
GITHUB_TOPIC_QUERY = "machine-learning OR deep-learning OR computer-vision OR robotics OR perception OR reproducible-research"


class DiscoveryRunRequest(BaseModel):
    query: str = Field(default="", max_length=160)
    platforms: list[Platform] = Field(default_factory=lambda: ["github", "huggingface"], max_length=2)
    hf_types: list[HFKind] = Field(default_factory=lambda: ["model", "dataset", "space"], max_length=3)
    limit: int = Field(default=20, ge=1, le=50)
    page: int = Field(default=1, ge=1, le=100)

    @field_validator("query")
    @classmethod
    def strip_query(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_sources(self):
        if not self.platforms:
            raise ValueError("Select at least one discovery source")
        if "huggingface" in self.platforms and not self.hf_types:
            raise ValueError("Select at least one Hugging Face content type")
        return self


class DiscoveryItem(BaseModel):
    id: str
    platform: Platform
    kind: str
    source_identity: str
    source_url: HttpUrl
    title: str
    description: str
    metadata: dict[str, Any]
    ranking_score: float = Field(ge=0, le=100)
    ranking_reasons: list[str]
    status: Status
    created_at: str | None = None

    @field_validator("source_url")
    @classmethod
    def provider_host_only(cls, value: HttpUrl, info):
        platform = info.data.get("platform")
        expected = "github.com" if platform == "github" else "huggingface.co"
        if value.host != expected or value.scheme != "https":
            raise ValueError("Discovery links must point to the expected HTTPS provider host")
        return value


class SourceResult(BaseModel):
    source: str
    status: Literal["ok", "rate_limited", "error", "timeout"]
    count: int = 0
    detail: str | None = None


class DiscoveryResponse(BaseModel):
    items: list[DiscoveryItem]
    sources: list[SourceResult]
    query: str


def _clean_text(value: Any, maximum: int = 3000) -> str:
    if not isinstance(value, str):
        return ""
    # Remove control characters; all returned strings remain untrusted display data.
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value).strip()[:maximum]


def _source_status(exc: Exception) -> SourceResult:
    if isinstance(exc, httpx.TimeoutException):
        return SourceResult(source="", status="timeout", detail="Provider request timed out.")
    if isinstance(exc, _RateLimited):
        return SourceResult(source="", status="rate_limited", detail="Provider rate limit reached; try again later.")
    return SourceResult(source="", status="error", detail="Provider response was unavailable or invalid.")


class _RateLimited(RuntimeError):
    pass


def _fetch_json(client: httpx.Client, url: str, *, params: dict[str, Any], headers: dict[str, str]) -> Any:
    response = client.get(url, params=params, headers=headers)
    if response.status_code == 429:
        raise _RateLimited()
    if response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0":
        raise _RateLimited()
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as exc:
        raise ValueError("invalid provider JSON") from exc


def _github_item(repo: dict[str, Any]) -> dict[str, Any] | None:
    identity = _clean_text(repo.get("full_name"), 250)
    url = _clean_text(repo.get("html_url"), 1000)
    parsed_url = urlsplit(url)
    if not identity or parsed_url.scheme != "https" or parsed_url.hostname != "github.com" or not parsed_url.path.strip("/"):
        return None
    stars = repo.get("stargazers_count") if type(repo.get("stargazers_count")) is int else None
    topics = repo.get("topics") if isinstance(repo.get("topics"), list) else []
    license_info = repo.get("license") if isinstance(repo.get("license"), dict) else {}
    return {
        "platform": "github", "kind": "repository", "source_identity": identity,
        "source_url": url, "title": identity, "description": _clean_text(repo.get("description")),
        "metadata": {"topics": [_clean_text(t, 80) for t in topics[:30]], "language": _clean_text(repo.get("language"), 80),
                     "stars": stars, "updated_at": _clean_text(repo.get("updated_at"), 80),
                     "created_at": _clean_text(repo.get("created_at"), 80), "license": _clean_text(license_info.get("spdx_id"), 80)},
    }


def _hf_item(entry: dict[str, Any], kind: HFKind) -> dict[str, Any] | None:
    identity = _clean_text(entry.get("id") or entry.get("modelId"), 250)
    if not identity or "/" not in identity or ".." in identity:
        return None
    url = f"https://huggingface.co/{kind}s/{quote(identity, safe='/')}"
    tags = entry.get("tags") if isinstance(entry.get("tags"), list) else []
    return {
        "platform": "huggingface", "kind": kind, "source_identity": f"{kind}:{identity}",
        "source_url": url, "title": identity, "description": "",
        "metadata": {"tags": [_clean_text(tag, 100) for tag in tags[:40]], "downloads": entry.get("downloads") if type(entry.get("downloads")) is int else None,
                     "likes": entry.get("likes") if type(entry.get("likes")) is int else None,
                     "last_modified": _clean_text(entry.get("lastModified"), 80), "pipeline_tag": _clean_text(entry.get("pipeline_tag"), 100),
                     "library_name": _clean_text(entry.get("library_name"), 100)},
    }


def rank_item(item: dict[str, Any], query: str, now: datetime | None = None) -> tuple[float, list[str]]:
    """Simple reproducible score from source metadata, textual topic matches and recency."""
    now = now or datetime.now(timezone.utc)
    text = " ".join([item.get("title", ""), item.get("description", ""), " ".join(item.get("metadata", {}).get("topics", [])), " ".join(item.get("metadata", {}).get("tags", []))]).lower()
    query_terms = [term.lower() for term in re.findall(r"[a-z0-9+#-]+", query) if len(term) > 2]
    topic_terms = ["machine learning", "deep learning", "computer vision", "robotics", "autonomous", "perception", "reproducible", "experiment"]
    reasons: list[str] = []
    matched_query = [term for term in query_terms if term in text]
    matched_topics = [term for term in topic_terms if term in text]
    score = 10 + min(40, 15 * len(matched_query)) + min(30, 8 * len(matched_topics))
    reasons.append("Retrieved from an official public provider API")
    if matched_query:
        reasons.append("Matches your search: " + ", ".join(matched_query[:3]))
    if matched_topics:
        reasons.append("Topic match: " + ", ".join(matched_topics[:3]))
    metadata = item.get("metadata", {})
    metric = metadata.get("stars") if item["platform"] == "github" else metadata.get("downloads")
    if isinstance(metric, int):
        score += min(20, round(5 * (metric + 1).bit_length()))
        reasons.append("Source popularity metadata is available" if metric > 0 else "Popularity count is currently zero")
    source_date = metadata.get("updated_at") or metadata.get("last_modified")
    try:
        updated = datetime.fromisoformat(source_date.replace("Z", "+00:00"))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        age_days = max(0, (now - updated).days)
        score += max(0, 10 - age_days // 30)
        reasons.append("Updated within the last 30 days" if age_days <= 30 else "Recent update metadata is available")
    except (AttributeError, ValueError):
        reasons.append("Update date not provided by source")
    if not reasons:
        reasons.append("Public source metadata matched the requested discovery query")
    return round(min(100.0, max(0.0, score)), 1), reasons


def run_discovery(principal: Principal, request: DiscoveryRunRequest, settings: Settings | None = None) -> DiscoveryResponse:
    settings = settings or get_settings()
    items, sources, _ = collect_discovery(request, settings)
    discovered_keys = {(item["platform"], item["source_identity"]) for item in items}
    stored_items = persist_items(principal, items, settings)
    current_items = [item for item in stored_items if (item["platform"], item["source_identity"]) in discovered_keys]
    return DiscoveryResponse(items=[DiscoveryItem(**item) for item in current_items], sources=sources, query=request.query)


def collect_discovery(request: DiscoveryRunRequest, settings: Settings | None = None) -> tuple[list[dict[str, Any]], list[SourceResult], str]:
    """Fetch and rank public provider data without persisting it.

    The scheduled worker calls this same adapter pipeline, then persists through
    its isolated cloud credential. Interactive requests use run_discovery.
    """
    settings = settings or get_settings()
    query = request.query or TOPIC_QUERY
    timeout = httpx.Timeout(settings.discovery_request_timeout_seconds)
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        jobs: list[tuple[str, Any]] = []
        if "github" in request.platforms:
            headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
            if settings.github_token:
                headers["Authorization"] = f"Bearer {settings.github_token}"
            jobs.append(("github", ("https://api.github.com/search/repositories", {"q": request.query or GITHUB_TOPIC_QUERY, "sort": "updated", "order": "desc", "per_page": min(request.limit, 30), "page": request.page}, headers)))
        if "huggingface" in request.platforms:
            for kind in request.hf_types:
                jobs.append((f"huggingface:{kind}", (f"https://huggingface.co/api/{kind}s", {"search": request.query or "computer vision", "limit": min(request.limit, 30), "offset": (request.page - 1) * min(request.limit, 30), "sort": "lastModified", "direction": "-1"}, {"Accept": "application/json"})))
        with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as executor:
            futures = [executor.submit(_fetch_json, client, url, params=params, headers=headers) for _, (url, params, headers) in jobs]
            outcomes = []
            for future in futures:
                try:
                    outcomes.append(future.result())
                except Exception as exc:
                    outcomes.append(exc)

    items: list[dict[str, Any]] = []
    sources: list[SourceResult] = []
    for (source, _), outcome in zip(jobs, outcomes):
        if isinstance(outcome, Exception):
            state = _source_status(outcome)
            sources.append(state.model_copy(update={"source": source}))
            continue
        if source == "github":
            raw = outcome.get("items") if isinstance(outcome, dict) else None
            if not isinstance(raw, list):
                sources.append(SourceResult(source=source, status="error", detail="Provider response did not match the expected schema.")); continue
            parsed = [item for item in (_github_item(entry) for entry in raw[:request.limit] if isinstance(entry, dict)) if item]
        else:
            kind = source.split(":", 1)[1]
            if not isinstance(outcome, list):
                sources.append(SourceResult(source=source, status="error", detail="Provider response did not match the expected schema.")); continue
            parsed = [item for item in (_hf_item(entry, kind) for entry in outcome[:request.limit] if isinstance(entry, dict)) if item]
        for item in parsed:
            item["ranking_score"], item["ranking_reasons"] = rank_item(item, query)
        items.extend(parsed)
        sources.append(SourceResult(source=source, status="ok", count=len(parsed)))
    items = _dedupe_sort(items)[:request.limit]
    return items, sources, query


def _dedupe_sort(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique = {(item["platform"], item["source_identity"]): item for item in items}
    return sorted(unique.values(), key=lambda x: (-x["ranking_score"], x["title"].lower()))


def persist_items(principal: Principal, items: list[dict[str, Any]], settings: Settings | None = None) -> list[dict[str, Any]]:
    settings = settings or get_settings()
    if settings.agent_state_storage == "supabase":
        if not principal.access_token:
            raise supabase_store.CloudStoreAuthError("A verified Supabase access token is required for discovery storage.")
        return supabase_store.upsert_discovery_items(principal.user_id, principal.access_token, items, settings)
    db.upsert_discovery_items(principal.user_id, items)
    return db.get_discovery_items(principal.user_id)


def list_items(principal: Principal, status: Status | None = None, settings: Settings | None = None) -> list[dict[str, Any]]:
    settings = settings or get_settings()
    if settings.agent_state_storage == "supabase":
        if not principal.access_token:
            raise supabase_store.CloudStoreAuthError("A verified Supabase access token is required for discovery storage.")
        return supabase_store.list_discovery_items(principal.user_id, principal.access_token, status, settings)
    return db.get_discovery_items(principal.user_id, status)


def set_item_status(principal: Principal, item_id: str, status: Literal["saved", "dismissed"], settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    if settings.agent_state_storage == "supabase":
        if not principal.access_token:
            raise supabase_store.CloudStoreAuthError("A verified Supabase access token is required for discovery storage.")
        return supabase_store.update_discovery_status(principal.user_id, principal.access_token, item_id, status, settings)
    return db.set_discovery_status(principal.user_id, item_id, status)
