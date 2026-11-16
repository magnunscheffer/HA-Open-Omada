"""Client tests against a fake Omada controller (real aiohttp server)."""

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from api import OmadaApiError, OmadaAuthError, OmadaClient
from api.client import ERR_TOKEN_EXPIRED

OID = "cid123"


class FakeController:
    def __init__(self) -> None:
        self.tokens_issued = 0
        self.expire_next = False
        self.bad_client = False
        self.auth_headers: list[str] = []
        self.app = web.Application()
        self.app.router.add_post("/openapi/authorize/token", self.token)
        self.app.router.add_get(f"/openapi/v1/{OID}/sites", self.sites)
        self.app.router.add_post(f"/openapi/v1/{OID}/only-get", self.sites)  # GET -> 405
        self.app.router.add_get(f"/openapi/v1/{OID}/html", self.html)
        self.app.router.add_get(f"/openapi/v1/{OID}/redirect", self.redirect)
        self.port_calls: list[dict] = []
        self.app.router.add_patch(f"/openapi/v1/{OID}/sites/s1/switches/AA-BB-CC-00-00-01/ports/2", self.port_patch)

    async def token(self, req: web.Request) -> web.Response:
        if self.bad_client:
            return web.json_response({"errorCode": -44106, "msg": "invalid client"})
        self.tokens_issued += 1
        n = self.tokens_issued
        return web.json_response({"errorCode": 0, "result": {
            "accessToken": f"AT-{n}", "refreshToken": f"RT-{n}", "expiresIn": 7200,
            "grant": req.query["grant_type"]}})

    async def html(self, req: web.Request) -> web.Response:
        return web.Response(text="<html>login</html>")

    async def redirect(self, req: web.Request) -> web.Response:
        raise web.HTTPFound("https://evil.example/")

    async def port_patch(self, req: web.Request) -> web.Response:
        self.port_calls.append(await req.json())
        return web.json_response({"errorCode": 0})

    async def sites(self, req: web.Request) -> web.Response:
        self.auth_headers.append(req.headers.get("Authorization", ""))
        if self.expire_next:
            self.expire_next = False
            return web.json_response({"errorCode": ERR_TOKEN_EXPIRED, "msg": "expired"})
        page = int(req.query["page"])
        data = [{"siteId": "a"}] if page == 1 else [{"siteId": "b"}]
        return web.json_response({"errorCode": 0, "result": {"totalRows": 2, "data": data}})


@pytest.fixture
async def env():
    fake = FakeController()
    server = TestServer(fake.app)
    await server.start_server()
    async with aiohttp.ClientSession() as s:
        client = OmadaClient(s, str(server.make_url("")), "id", "secret", omadac_id=OID)
        yield fake, client
    await server.close()


async def test_auth_header_and_paging(env):
    fake, client = env
    sites = await client.get_sites()
    assert [s["siteId"] for s in sites] == ["a", "b"]
    assert fake.auth_headers == ["AccessToken=AT-1", "AccessToken=AT-1"]


async def test_expired_token_is_refreshed_and_retried(env):
    fake, client = env
    fake.expire_next = True
    assert len(await client.get_sites()) == 2
    assert fake.auth_headers[0] == "AccessToken=AT-1"
    assert fake.auth_headers[1] == "AccessToken=AT-2"


async def test_bad_credentials(env):
    fake, client = env
    fake.bad_client = True
    with pytest.raises(OmadaAuthError):
        await client.get_sites()


async def test_http_errors_never_leak_the_access_token(env):
    fake, client = env
    with pytest.raises(OmadaApiError) as exc:
        await client.request("GET", "/openapi/v1/{omadacId}/only-get")
    assert exc.value.error_code == 405
    text = f"{exc.value} {exc.value!r} {exc.value.__cause__!r}"
    assert "AT-1" not in text and "AccessToken" not in text



async def test_port_profile_uses_patch_with_name_and_override(env):
    fake, client = env
    await client.set_switch_port_profile(
        "s1", "AA:BB:CC:00:00:01", 2, "p-dis", port_name="Port2", profile_override=False
    )
    assert fake.port_calls == [{"name": "Port2", "profileId": "p-dis", "profileOverrideEnable": False}]


async def test_non_json_and_redirect_responses_are_api_errors(env):
    fake, client = env
    for path in ("/openapi/v1/{omadacId}/html", "/openapi/v1/{omadacId}/redirect"):
        with pytest.raises(OmadaApiError):
            await client.request("GET", path)


async def test_concurrent_expired_token_refreshes_once(env):
    fake, client = env
    await client.get_sites()  # AT-1
    fake.expire_next = True
    issued = fake.tokens_issued
    # The request that used the old token triggers one refresh; a second request that
    # reports the same (already replaced) token must not refresh again.
    await client.get_sites()
    await client._ensure_token(force=True, rejected="AT-1")
    assert fake.tokens_issued == issued + 1
