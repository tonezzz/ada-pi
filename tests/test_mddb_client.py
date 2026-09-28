import unittest
from unittest.mock import patch

import httpx

from backend.mddb_client import MddbClient


def _client(handler):
    client = MddbClient(base_url="http://mddb.test/v1")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def _ok(request):
    return httpx.Response(200, json={"results": [
        {"document": {"key": "k1", "contentMd": "c"}, "score": 0.9}]})


class VectorSearchRetryTest(unittest.IsolatedAsyncioTestCase):

    async def test_retries_503_then_succeeds(self):
        calls = []

        def handler(request):
            calls.append(request)
            if len(calls) < 3:
                return httpx.Response(503, json={"error": "index loading"})
            return _ok(request)

        with patch("backend.mddb_client.asyncio.sleep") as sleep:
            result = await _client(handler).vector_search("c", "q")
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep.await_count, 2)
        self.assertEqual(result[0]["key"], "k1")

    async def test_gives_up_after_retries(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(503, json={"error": "index loading"})

        with patch("backend.mddb_client.asyncio.sleep"):
            result = await _client(handler).vector_search("c", "q")
        self.assertIsNone(result)
        self.assertEqual(len(calls), 3)

    async def test_no_retry_on_4xx(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(400, json={"error": "bad request"})

        with patch("backend.mddb_client.asyncio.sleep") as sleep:
            result = await _client(handler).vector_search("c", "q")
        self.assertIsNone(result)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleep.await_count, 0)

    async def test_retries_transport_error(self):
        calls = []

        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                raise httpx.ConnectError("refused")
            return _ok(request)

        with patch("backend.mddb_client.asyncio.sleep"):
            result = await _client(handler).vector_search("c", "q")
        self.assertEqual(len(calls), 2)
        self.assertEqual(result[0]["key"], "k1")


if __name__ == "__main__":
    unittest.main()
