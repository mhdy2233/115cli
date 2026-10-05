import math

import pytest

from tests.client.conftest import make_client, make_dir


class TestFileListingPageSize:
    @pytest.mark.parametrize("page_size,server_cap", [
        (None, 1150), (1150, 1150), (5000, 1150), (1150, 300),
    ])
    def test_complete_listing_respects_server_page_size(self, page_size, server_cap):
        client = make_client()
        total = 1300
        requests = []

        def get(url, *, params):
            assert url.endswith("/files")
            requests.append(params.copy())
            offset = params["offset"]
            limit = min(params["limit"], server_cap)
            client.file._api.get.return_value.json.return_value = {
                "count": total, "offset": offset, "limit": limit,
                "data": [
                    {"fid": str(i), "cid": "0", "n": f"file-{i}", "s": 1}
                    for i in range(offset, min(offset + limit, total))
                ],
            }
            return client.file._api.get.return_value

        client.file._api.get.side_effect = get
        options = {} if page_size is None else {"page_size": page_size}
        entries = list(client.file.list(make_dir(id="0", path="/"), **options))

        requested = 200 if page_size is None else min(page_size, 1150)
        effective = min(requested, server_cap)
        assert [entry.id for entry in entries] == [str(i) for i in range(total)]
        assert requests[0]["limit"] == requested
        assert [request["offset"] for request in requests] == list(range(0, total, effective))
        assert len(requests) == math.ceil(total / effective)
        assert client.file._api.post.call_count == len(requests)

    @pytest.mark.parametrize("page_size", [0, -1])
    def test_invalid_page_size_does_not_request_api(self, page_size):
        client = make_client()
        with pytest.raises(ValueError, match="page size"):
            client.file.list("/folder", page_size=page_size)
        client.file._api.get.assert_not_called()
        client.file._api.post.assert_not_called()
