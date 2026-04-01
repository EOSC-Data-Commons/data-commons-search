import json
from typing import Any

import httpx
import pytest
from ag_ui.core import RunStartedEvent

from data_commons_search.config import settings
from data_commons_search.models import RankedSearchResponse
from tests.benchmark import TestItem, test_items

# When running the tests, ensure the server is running on port 8000
server_port = 8000
llm_models = [
    "einfracz/qwen3-coder",
    # "einfracz/gpt-oss-120b",
    # "einfracz/kimi-k2.5",
    # "einfracz/deepseek-v3.2",
]

# "Datasets about emoji" search should match "ACCOMOJI" in https://www.swissubase.ch/en/catalogue/search?q=emojis&p=0&ps=10&sn=ref-number&sd=desc
# But it does not seems to be in search index


def opensearch_is_available() -> bool:
    """Lightweight check whether an OpenSearch URL is reachable."""
    try:
        r = httpx.get(settings.opensearch_url.rstrip("/") + "/_cluster/health", timeout=1.0)
        return 200 <= r.status_code < 400
    except Exception:
        return False


@pytest.mark.parametrize("test_item", test_items)
@pytest.mark.parametrize("llm_model", llm_models)
@pytest.mark.skipif(
    not opensearch_is_available(),
    reason=f"OpenSearch unreachable at {settings.opensearch_url}",
)
def test_app(test_item: TestItem, llm_model: str) -> None:
    for attempt in range(3):
        try:
            print(f"☑️ Testing `{test_item['input']}`")
            response = httpx.get(f"http://127.0.0.1:{server_port}/", follow_redirects=True, timeout=20)
            assert response.status_code == 200
            # Test chat call streaming endpoint
            payload = {
                "items": [{"type": "message", "role": "user", "content": [{"text": test_item["input"]}]}],
                "model": llm_model,
            }
            headers = {"Content-Type": "application/json", "Authorization": "Bearer SECRET_KEY"}

            with httpx.stream(
                "POST",
                f"http://127.0.0.1:{server_port}/chat",
                headers=headers,
                json=payload,
                follow_redirects=True,
                timeout=120,
            ) as resp:
                assert resp.status_code == 200
                assert resp.headers.get("content-type", "").startswith("text/event-stream")
                events = process_stream(resp)
            # print(json.dumps(events, indent=2))

            assert len(events) >= 1
            # Ensure a `RunStartedEvent` is received first
            assert RunStartedEvent.model_validate(events[0])

            # Find the rerank results event (may be absent when the model answers without search)
            rerank_event = None
            for event in events:
                if event.get("type") == "TOOL_CALL_RESULT" and event.get("tool_call_id") == "rerank_results":
                    rerank_event = event
                    break

            if rerank_event is not None:
                # Model went through the full search → rerank pipeline
                ranked_response = RankedSearchResponse.model_validate_json(rerank_event["content"])
                assert len(ranked_response.hits) > 0, "Rerank returned no hits"
                for hit in ranked_response.hits:
                    assert hit.id, "Every ranked hit must have a non-empty id"
            else:
                # Model answered via the fallback (text summary) or no-results path —
                # verify the run still completed with at least one text or tool-result event.
                has_response = any(
                    event.get("type") in ("TEXT_MESSAGE_CHUNK", "TOOL_CALL_RESULT", "RUN_FINISHED") for event in events
                )
                assert has_response, f"No meaningful response events received for model {llm_model!r}"

            break
        except Exception as e:
            if attempt == 2:
                raise
            print(f"⚠️ Attempt {attempt + 1} failed: {e}. Retrying...")


# async def test_get_relevant_tools() -> None:
#     """Test reranking dummy search results."""
#     dummy_search_hits = [
#         SearchHit.model_validate(
#             {
#                 "id": "https://doi.org/10.17026/DANS-XTQ-AJZS",
#                 "source": {
#                     "_repo": "DANS",
#                     "_harvestUrl": "https://example.org/harvest",
#                     "doi": "10.17026/DANS-XTQ-AJZS",
#                     "url": None,
#                     "titles": [
#                         {
#                             "title": "Replication Data for: Cognitive load in cyclists while navigating in traffic: Effects of static and dynamic route events on neural activity of cyclists measured by fNIRS"
#                         }
#                     ],
#                     "descriptions": [
#                         {
#                             "description": "Neural activity data collected during a real-life field experiment by a non-invasive portable method, namely Functional Near-Infrared Spectroscopy (fNIRS), sensitive to neural activity in the prefrontal cortex region."
#                         },
#                     ],
#                     "publicationYear": "2025",
#                     "publicationDate": None,
#                     "subjects": [{"subject": "Engineering"}],
#                     "creators": [{"creatorName": "Nidegger, Christian"}],
#                     "resourceType": "dataset",
#                 },
#                 "opensearch_score": 0.91528225,
#                 "score": None,
#                 # "file_extensions": [],
#             }
#         )
#     ]
#     # search_res = RankedSearchResponse.model_validate(dummy_search_res)
#     await get_relevant_tools(dummy_search_hits)
#     print(dummy_search_hits)
#     assert len(dummy_search_hits) >= 1
#     for hit in dummy_search_hits:
#         print(f"Hit {hit.id} has file extensions: {hit.file_extensions}")
#         assert hit.file_extensions and len(hit.file_extensions) >= 1


def process_stream(resp: httpx.Response) -> list[Any]:
    events = []
    for raw_line in resp.iter_lines():
        if raw_line:
            stripped_line = raw_line.strip()
            if stripped_line.startswith("data: "):
                data = stripped_line[6:]  # Remove 'data: '
                try:
                    event = json.loads(data)
                    events.append(event)
                    # print(f"Event: {event.get('type', 'unknown')}")
                except json.JSONDecodeError:
                    continue
    return events
