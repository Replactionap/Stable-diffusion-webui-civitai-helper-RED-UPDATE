"""Meilisearch (search-new.civitai.com) integration for Civitai Helper

This module performs searches against the Meilisearch endpoint used by
Civitai's frontend and converts results into the parsed model format
used by the browser UI so we can merge them with the REST API results.
"""
from __future__ import annotations
import requests
from . import util
from . import civitai

SEARCH_URL = "https://search-new.civitai.com/multi-search"
# Token from user-provided snippet. If you prefer, you can store this in
# options and read via util.get_opts("ch_meili_token").
SEARCH_TOKEN = "8c46eb2508e21db1e9828a97968d91ab1ca1caa5f70a00e88a2ba1e286603b61"

HEADERS = {
    "Authorization": f"Bearer {SEARCH_TOKEN}",
    "Content-Type": "application/json",
    "Origin": "https://civitai.com",
    "Referer": "https://civitai.com/",
}


def _convert_hit_to_parsed_model(hit: dict) -> dict:
    """Convert a Meili hit into the parsed-model dict used by browser.make_cards.

    We keep the fields the UI expects: id, name, preview, url, versions,
    description, type, download, base_models.
    """
    model_id = hit.get("id")
    name = hit.get("name", "")
    description = hit.get("description", "") or hit.get("summary", "") or ""

    # Build url to the model page
    url = f"{civitai.URLS['modelPage']}{model_id}"

    # Collect versions info
    versions = {}
    base_models = []
    download = ""

    # Meili may provide a single `version` or a `versions` list
    version_list = []
    if "version" in hit and isinstance(hit.get("version"), dict):
        version_list = [hit.get("version")]
    else:
        version_list = hit.get("versions", []) or []

    previews = []
    for v in version_list:
        ver_id = v.get("id")
        base = v.get("baseModel") or v.get("base_model")
        if base and base not in base_models:
            base_models.append(base)

        if ver_id:
            versions[ver_id] = base

        images = v.get("images") or []
        if images:
            previews.extend(images)

        # downloadUrl may be present on version
        if not download:
            download = v.get("downloadUrl") or v.get("download_url") or ""

    # Pick first image preview (if any)
    preview = {"type": None, "url": None}
    for img in previews:
        if not isinstance(img, dict):
            continue
        if img.get("type") != "image":
            continue
        preview["url"] = img.get("url") or img.get("src") or None
        preview["type"] = "image"
        break

    model_type = hit.get("type") or hit.get("modelType") or ""

    return {
        "id": model_id,
        "name": name,
        "preview": preview,
        "url": url,
        "versions": versions,
        "description": description,
        "type": model_type,
        "download": download,
        "base_models": base_models,
    }


def search(query: str, base_models: list | None = None, types: list | None = None, limit: int = 20) -> list:
    """Perform a Meilisearch query and return a list of parsed-model dicts.

    This is a best-effort mapping; Meili results may have slightly different
    fields than the REST API, but we provide enough for the UI to render cards.
    """
    if not query:
        return []

    filters = []
    if types:
        for t in types:
            filters.append(f"type = {t}")
    if base_models:
        for bm in base_models:
            # meili indexes base model under version.baseModel
            filters.append(f"version.baseModel = {bm}")

    payload = {
        "queries": [
            {
                "indexUid": "models_v9",
                "q": query,
                "limit": limit,
                "offset": 0,
                "filter": filters,
                "facets": ["category.name", "type", "version.baseModel", "tags.name"],
            }
        ]
    }

    try:
        util.printD(f"Meili search: {query} filters={filters}")
        resp = requests.post(SEARCH_URL, headers=HEADERS, json=payload, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        util.printD(f"Meili search failed: {e}")
        return []

    data = resp.json()
    if not data:
        return []

    try:
        result = data["results"][0]
    except Exception:
        return []

    hits = result.get("hits", []) or []

    parsed = []
    for hit in hits:
        try:
            parsed.append(_convert_hit_to_parsed_model(hit))
        except Exception as e:
            util.printD(f"Failed to convert meili hit: {e}")
            continue

    # For parsed hits that lack preview images, try to fetch model info from
    # the Civitai REST API to obtain images/versions. This gives better
    # thumbnails similar to what parse_model() does for REST results.
    nsfw_preview_threshold = util.get_opts("ch_nsfw_threshold")
    max_size_preview = util.get_opts("ch_max_size_preview")

    for m in parsed:
        try:
            if m.get("preview", {}).get("url"):
                continue

            model_id = m.get("id")
            if not model_id:
                continue

            util.printD(f"Meili: fetching model info for id {model_id} to get preview")
            model_info = civitai.get_model_info_by_id(str(model_id))
            if not model_info:
                continue

            # Extract previews from modelVersions
            model_versions = model_info.get("modelVersions", [])
            previews = []
            for version in model_versions:
                images = version.get("images", [])
                if images:
                    previews.extend(images)

                base = version.get("baseModel", None)
                if base and (base not in m.get("base_models", [])):
                    m.setdefault("base_models", []).append(base)

                vid = version.get("id")
                if vid:
                    m.setdefault("versions", {})[vid] = version.get("baseModel")

            # Pick first allowed preview respecting NSFW threshold
            for img in previews:
                if img.get("type") != "image":
                    continue

                img_nsfw = img.get("nsfwLevel", 32)
                try:
                    if civitai.NSFW_LEVELS.get(nsfw_preview_threshold, 1) < img_nsfw:
                        util.printD("Meili: skipping NSFW preview due to threshold")
                        continue
                except Exception:
                    pass

                # use civitai helper to construct sized url
                url = civitai.get_image_url(img, max_size_preview)
                if url:
                    m["preview"] = {"type": "image", "url": url}
                    break

        except Exception as e:
            util.printD(f"Failed to enrich meili model {m.get('id')}: {e}")
            continue

    return parsed
