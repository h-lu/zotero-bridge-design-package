from __future__ import annotations

from typing import Any

from app.models import DiscoverySearchResponse, DiscoveryWork


class DiscoveryService:
    def __init__(self, bridge: Any) -> None:
        self._bridge = bridge

    async def search_discovery(
        self,
        *,
        q: str,
        start: int,
        limit: int,
        year_from: int | None,
        year_to: int | None,
        oa_only: bool,
        resolve_in_library: bool,
        exclude_existing: bool,
        sort: str,
    ) -> DiscoverySearchResponse:
        bridge = self._bridge
        page_size = 200
        page_number = (start // page_size) + 1
        local_start = start % page_size
        filters: list[str] = []
        if year_from is not None:
            filters.append(f"from_publication_date:{year_from}-01-01")
        if year_to is not None:
            filters.append(f"to_publication_date:{year_to}-12-31")
        if oa_only:
            filters.append("is_oa:true")
        params: dict[str, Any] = {
            "search": q,
            "per-page": page_size,
            "page": page_number,
            "select": ",".join(
                [
                    "id",
                    "doi",
                    "display_name",
                    "publication_year",
                    "publication_date",
                    "type",
                    "cited_by_count",
                    "authorships",
                    "primary_location",
                    "open_access",
                    "abstract_inverted_index",
                    "primary_topic",
                ]
            ),
        }
        if filters:
            params["filter"] = ",".join(filters)
        if sort == "cited_by":
            params["sort"] = "cited_by_count:desc"
        elif sort == "recent":
            params["sort"] = "publication_date:desc"
        if bridge._settings.openalex_api_key:
            params["api_key"] = bridge._settings.openalex_api_key
        doi_matches: dict[str, str] = {}
        title_matches: dict[str, str] = {}
        if resolve_in_library or exclude_existing:
            doi_matches, title_matches = await bridge._discovery_library_match_maps(
                require_ready=True
            )

        items: list[DiscoveryWork] = []
        total: int | None = None
        consumed_results = 0
        current_page = page_number
        current_local_start = local_start

        while len(items) < limit:
            payload = await bridge._openalex_get(
                "/works",
                params={**params, "page": current_page},
            )
            if total is None:
                meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
                total = bridge._coerce_optional_int(meta.get("count"))
            results = payload.get("results", []) if isinstance(payload, dict) else []
            if not isinstance(results, list):
                results = []
            if current_local_start >= len(results):
                if len(results) < page_size:
                    break
                current_page += 1
                current_local_start = 0
                continue

            for result in results[current_local_start:]:
                if not isinstance(result, dict):
                    continue
                consumed_results += 1
                item = bridge._normalize_openalex_work(result)
                matched_item_key: str | None = None
                match_strategy: str | None = None
                if resolve_in_library or exclude_existing:
                    matched_item_key, match_strategy = bridge._match_discovery_work_in_library(
                        item=item,
                        doi_matches=doi_matches,
                        title_matches=title_matches,
                    )
                    item = item.model_copy(
                        update={
                            "alreadyInLibrary": matched_item_key is not None,
                            "libraryItemKey": matched_item_key,
                            "libraryMatchStrategy": match_strategy,
                        }
                    )
                if exclude_existing and matched_item_key is not None:
                    continue
                items.append(item)
                if len(items) >= limit:
                    break

            if len(items) >= limit or len(results) < page_size:
                break
            current_page += 1
            current_local_start = 0

        resolved_total = total if total is not None else start + consumed_results
        return DiscoverySearchResponse(
            items=items,
            count=len(items),
            total=resolved_total,
            start=start,
            limit=limit,
            nextStart=bridge._next_start(
                start=start,
                returned_count=consumed_results,
                total=resolved_total,
            ),
        )
