"""Async GraphQL client behavior: throttling, retries, pagination, bulk (mocked transport)."""

import json
from collections import deque
from pathlib import Path

import httpx

from app.shopify.graphql import ShopifyGraphQLClient
from app.shopify.queries import PRODUCTS_PAGE_QUERY

FIXTURES = Path(__file__).parent / "fixtures" / "graphql"
PROBE = """
{"data": {"shop": {"name": "Timberline Supply"}, "productCount": {"count": 2}}}
"""


class Recorder:
    def __init__(self) -> None:
        self.sleeps: list[float] = []
        self.calls: list[str] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def _cost(actual: float) -> dict:
    return {
        "extensions": {
            "cost": {
                "requestedQueryCost": actual,
                "actualQueryCost": actual,
                "throttleStatus": {"maximumAvailable": 2000.0, "currentlyAvailable": 1990.0, "restoreRate": 100.0},
            }
        }
    }


def make_client(responses: list[httpx.Response], recorder: Recorder) -> ShopifyGraphQLClient:
    queue = deque(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.calls.append(request.content.decode())
        if not queue:
            raise AssertionError("unexpected GraphQL request")
        return queue.popleft()

    cl = ShopifyGraphQLClient("timberline.example.com", "shpat_test", sleep=recorder.sleep)
    cl._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return cl


def resp_200(body: str, actual_cost: float = 10.0) -> httpx.Response:
    data = json.loads(body)
    for ext, value in _cost(actual_cost).items():
        data.setdefault(ext, value)
    return httpx.Response(200, json=data)


async def test_request_consumes_throttle_budget():
    recorder = Recorder()
    cl = make_client([resp_200(PROBE, actual_cost=8.0)], recorder)
    async with cl:
        data = await cl.request("query { x }")
    assert data["data"]["shop"]["name"] == "Timberline Supply"
    assert recorder.sleeps == [], "budget must not sleep when affordable"
    assert cl._budget.currently_available == 1990.0 - 8.0


async def test_429_honors_retry_after():
    recorder = Recorder()
    cl = make_client([httpx.Response(429, headers={"Retry-After": "0.01"}), resp_200(PROBE)], recorder)
    async with cl:
        data = await cl.request("query { x }")
    assert data["data"]["shop"]["name"] == "Timberline Supply"
    assert 0.01 in (recorder.sleeps or [])


async def test_5xx_retries_with_backoff():
    recorder = Recorder()
    cl = make_client([httpx.Response(503), httpx.Response(503), resp_200(PROBE)], recorder)
    async with cl:
        data = await cl.request("query { x }")
    assert data["data"]["shop"]["name"] == "Timberline Supply"
    assert len(recorder.sleeps) >= 2


async def test_throttled_error_body_retries():
    recorder = Recorder()
    throttled = httpx.Response(
        200,
        json={
            "errors": [{"message": "throttled", "extensions": {"code": "THROTTLED"}}],
            **_cost(100.0),
        },
    )
    cl = make_client([throttled, resp_200(PROBE)], recorder)
    async with cl:
        data = await cl.request("query { x }")
    assert data["data"]["shop"]["name"] == "Timberline Supply"
    assert any(s >= 2.0 for s in recorder.sleeps), "THROTTLED must back off"


async def test_budget_wait_before_expensive_call():
    recorder = Recorder()
    cl = make_client([resp_200(PROBE)], recorder)
    cl._budget.currently_available = 0.0  # simulate depletion
    async with cl:
        await cl.request("query { x }")
    waits = [s for s in recorder.sleeps if 0 < s <= 5.0]
    assert waits, "depleted budget must wait for restore"


async def test_pagination_walks_cursor():
    recorder = Recorder()
    p1 = str(FIXTURES / "products_page1.json")
    body1 = json.loads(Path(p1).read_text(encoding="utf-8"))
    body2 = json.loads((FIXTURES / "products_page2.json").read_text(encoding="utf-8"))
    cl = make_client(
        [httpx.Response(200, json=body1), httpx.Response(200, json=body2)],
        recorder,
    )
    async with cl:
        nodes = await cl.paginate(PRODUCTS_PAGE_QUERY, {}, "products")
    assert [n["handle"] for n in nodes] == [
        "kestrel-alpine-shell-jacket",
        "kestrel-tapper-tee",
        "kestrel-down-parka",
    ]
    sent_vars = [json.loads(call)["variables"] for call in recorder.calls]
    assert sent_vars[0]["cursor"] is None
    assert sent_vars[1]["cursor"] == body1["data"]["products"]["pageInfo"]["endCursor"]


async def test_bulk_flow_downloads_jsonl():
    recorder = Recorder()
    run_resp = httpx.Response(200, json={
        "data": {"bulkOperationRunQuery": {"bulkOperation": {"id": "gid://shopify/BulkOperation/1", "status": "CREATED"}, "userErrors": []}},
        **_cost(5.0),
    })
    running = httpx.Response(200, json={
        "data": {"currentBulkOperation": {"id": "1", "status": "RUNNING", "objectCount": "2", "url": None}},
        **_cost(2.0),
    })
    completed = httpx.Response(200, json={
        "data": {"currentBulkOperation": {"id": "1", "status": "COMPLETED", "objectCount": "3",
                 "url": "https://bulk.timberline.example.myshopify.com/artifact.jsonl"}},
        **_cost(2.0),
    })
    artifact_lines = "\n".join([
        json.dumps({"id": "gid://shopify/Product/1", "__typename": "Product", "title": "A"}),
        json.dumps({"id": "gid://shopify/Product/2", "__typename": "Product", "title": "B"}),
        "",
    ])
    queue = deque([run_resp, running, completed, httpx.Response(200, text=artifact_lines)])

    def handler(request: httpx.Request) -> httpx.Response:
        if not queue:
            raise AssertionError("unexpected bulk request")
        return queue.popleft()

    cl = ShopifyGraphQLClient("timberline.example.com", "tok", sleep=recorder.sleep)
    cl._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    parsed = await cl.bulk_query("QUERY")
    assert len(parsed) == 2
    assert json.loads(parsed[0])["title"] == "A"
