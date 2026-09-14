"""Local enterprise auth smoke tests, without external OAuth or DB injection.

Run cases(ctx) AFTER restarting App with these config keys:
  auth.enable_anonymous_usage: false
  auth.enable_self_auth: true
  auth.jwt_key: "smoke-local-only-not-a-secret"
Keep app.build_buddy_url equal to ctx.http_url (loopback only).

IMPORTANT v2.303.0: disabling anonymous usage alone is insufficient without
providers. enterprise/server/oidc/oidc.go AnonymousUsageEnabled() returns
flag || (no configured providers && !selfauth.Enabled()). RegisterNullAuth
passes that effective value to NullAuthenticator, so it remains anonymous.
Self-auth uses the app's own local OAuth endpoints and needs no credentials.
It grants admin access to anyone who can reach it: NEVER enable in production.

Only UNAUTHENTICATED/PERMISSION_DENIED count as denial. Unsupported RPCs,
validation errors, missing blobs, transport errors and timeouts FAIL tests.
Not covered: external providers, multi-user/group isolation, role boundaries,
key revocation/expiry, session expiry, TLS, or production identity security.
"""

import hashlib
from http.cookiejar import CookieJar
import uuid
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import (Request, build_opener, HTTPCookieProcessor,
                            HTTPRedirectHandler, ProxyHandler)

import grpc
from google.bytestream import bytestream_pb2 as bs, bytestream_pb2_grpc as bs_grpc
from proto import api_key_pb2 as keys, capability_pb2 as cap, context_pb2
from proto import grp_pb2 as groups, user_pb2 as users
from proto import remote_execution_pb2 as re, remote_execution_pb2_grpc as re_grpc
from proto import publish_build_event_pb2_grpc as publish_grpc

from smoke import bes


_DENIED = (grpc.StatusCode.UNAUTHENTICATED, grpc.StatusCode.PERMISSION_DENIED)


def _denied(operation):
    try:
        operation()
    except grpc.RpcError as exc:
        assert exc.code() in _DENIED, (
            f"Expected auth denial, got {exc.code().name}: {exc.details()}"
        )
        return
    raise AssertionError("Anonymous/invalid credentials unexpectedly accepted")


def _digest(data):
    return re.Digest(hash=hashlib.sha256(data).hexdigest(), size_bytes=len(data))


def _rpc(ctx, method, request, response_type, **kwargs):
    return ctx.channel.unary_unary(
        f"/buildbuddy.service.BuildBuddyService/{method}",
        request_serializer=type(request).SerializeToString,
        response_deserializer=response_type.FromString,
    )(request, timeout=min(ctx.timeout, 5), **kwargs)


def _http(ctx, opener, method, request, response_type):
    with opener.open(Request(
        ctx.http_url + "/rpc/BuildBuddyService/" + method,
        data=request.SerializeToString(),
        headers={"Content-Type": "application/protobuf"},
    ), timeout=min(ctx.timeout, 5)) as response:
        assert response.status == 200, response.status
        return response_type.FromString(response.read())


class _LocalRedirect(HTTPRedirectHandler):
    """Do not let even a misconfigured login redirect to a remote provider."""
    def __init__(self, origin):
        self.origin = urlsplit(origin).netloc

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        assert target.scheme == "http" and target.netloc == self.origin, newurl
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _local_authenticated_roundtrip(ctx):
    """Real local OAuth -> CreateUser -> API key -> authenticated CAS and AC."""
    origin = urlsplit(ctx.http_url)
    assert origin.scheme == "http" and origin.hostname in ("127.0.0.1", "localhost", "::1")
    opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()),
                          _LocalRedirect(ctx.http_url))
    login = ctx.http_url + "/login/?" + urlencode(
        {"issuer_url": ctx.http_url, "redirect_url": "/"})
    with opener.open(login, timeout=min(ctx.timeout, 5)) as response:
        assert response.status == 200
        response.read()
    # This is normal public API onboarding, not a database fixture/injection.
    created = _http(ctx, opener, "CreateUser", users.CreateUserRequest(), users.CreateUserResponse)
    user = _http(ctx, opener, "GetUser", users.GetUserRequest(), users.GetUserResponse)
    assert created.display_user.user_id.id == user.display_user.user_id.id
    assert user.display_user.user_id.id and user.display_user.email == "buildbuddy@example.com"
    assert user.selected_group.access == users.SelectedGroup.ALLOWED
    group_id = user.selected_group.group_id
    assert group_id and any(g.id == group_id for g in user.user_group)
    request_context = context_pb2.RequestContext(group_id=group_id)
    created_key = _http(ctx, opener, "CreateApiKey", keys.CreateApiKeyRequest(
        request_context=request_context, label="smoke-local-auth-" + uuid.uuid4().hex,
        capability=[cap.CACHE_WRITE, cap.CAS_WRITE]), keys.CreateApiKeyResponse)
    assert created_key.api_key.id and created_key.api_key.value
    metadata = (("x-buildbuddy-api-key", created_key.api_key.value),)
    options = {"timeout": min(ctx.timeout, 5), "metadata": metadata}
    cas = re_grpc.ContentAddressableStorageStub(ctx.channel)
    ac = re_grpc.ActionCacheStub(ctx.channel)
    data = ("authenticated-smoke:" + uuid.uuid4().hex).encode()
    digest = _digest(data)
    instance = "smoke/auth/" + uuid.uuid4().hex
    update = cas.BatchUpdateBlobs(re.BatchUpdateBlobsRequest(
        instance_name=instance, requests=[re.BatchUpdateBlobsRequest.Request(
            digest=digest, data=data)]), **options)
    assert len(update.responses) == 1 and update.responses[0].status.code == 0, update
    read_request = re.BatchReadBlobsRequest(instance_name=instance, digests=[digest])
    read = cas.BatchReadBlobs(read_request, **options)
    assert len(read.responses) == 1 and read.responses[0].status.code == 0, read
    assert read.responses[0].data == data and read.responses[0].digest == digest
    action_digest = _digest(b"action:" + data)
    result = re.ActionResult(exit_code=0, stdout_raw=b"authenticated smoke output")
    stored = ac.UpdateActionResult(re.UpdateActionResultRequest(
        instance_name=instance, action_digest=action_digest, action_result=result), **options)
    get_request = re.GetActionResultRequest(instance_name=instance, action_digest=action_digest)
    assert ac.GetActionResult(get_request, **options) == stored
    assert stored.stdout_raw == result.stdout_raw and stored.exit_code == 0
    # Repeat denial against KNOWN existing entries, not just absent digests.
    _denied(lambda: cas.BatchReadBlobs(read_request, timeout=min(ctx.timeout, 5)))
    _denied(lambda: ac.GetActionResult(get_request, timeout=min(ctx.timeout, 5)))


def cases(ctx):
    """Return independent anonymous denial cases, followed by local login proof."""
    timeout = min(ctx.timeout, 5)
    cas = re_grpc.ContentAddressableStorageStub(ctx.channel)
    ac = re_grpc.ActionCacheStub(ctx.channel)
    stream = bs_grpc.ByteStreamStub(ctx.channel)
    publish = publish_grpc.PublishBuildEventStub(ctx.channel)
    data = ("anonymous-auth-smoke:" + uuid.uuid4().hex).encode()
    digest = _digest(data)
    resource = f"blobs/{digest.hash}/{digest.size_bytes}"
    upload = f"uploads/{uuid.uuid4()}/{resource}"
    bad_key = (("x-buildbuddy-api-key", "invalid-smoke-" + uuid.uuid4().hex),)
    group_id = "GR0000000000000000000"
    context = context_pb2.RequestContext(group_id=group_id)

    def http_denial():
        # Fresh opener: no cookies from the positive-login case or host proxy.
        opener = build_opener(ProxyHandler({}), _LocalRedirect(ctx.http_url))
        try:
            _http(ctx, opener, "GetUser", users.GetUserRequest(), users.GetUserResponse)
        except HTTPError as exc:
            body = exc.read().decode()
            # v2.303.0 protolet maps ALL method errors to HTTP 500; the
            # embedded gRPC status, not generic HTTP failure, proves denial.
            assert exc.code == 500, (exc.code, body)
            assert body.startswith(("rpc error: code = Unauthenticated desc = ",
                                    "rpc error: code = PermissionDenied desc = ")), body
            return
        raise AssertionError("Anonymous HTTP GetUser unexpectedly accepted")

    calls = [
        ("cas.find_missing", lambda: cas.FindMissingBlobs(re.FindMissingBlobsRequest(
            blob_digests=[digest]), timeout=timeout)),
        ("cas.batch_write", lambda: cas.BatchUpdateBlobs(re.BatchUpdateBlobsRequest(
            requests=[re.BatchUpdateBlobsRequest.Request(digest=digest, data=data)]), timeout=timeout)),
        ("cas.batch_read", lambda: cas.BatchReadBlobs(re.BatchReadBlobsRequest(
            digests=[digest]), timeout=timeout)),
        ("cas.get_tree", lambda: list(cas.GetTree(re.GetTreeRequest(root_digest=digest), timeout=timeout))),
        ("bytestream.write", lambda: stream.Write(iter([bs.WriteRequest(
            resource_name=upload, data=data, finish_write=True)]), timeout=timeout)),
        ("bytestream.read", lambda: list(stream.Read(bs.ReadRequest(resource_name=resource), timeout=timeout))),
        ("ac.write", lambda: ac.UpdateActionResult(re.UpdateActionResultRequest(
            action_digest=digest, action_result=re.ActionResult(exit_code=0)), timeout=timeout)),
        ("ac.read", lambda: ac.GetActionResult(re.GetActionResultRequest(action_digest=digest), timeout=timeout)),
        ("bes.publish", lambda: list(publish.PublishBuildToolEventStream(iter(
            bes._requests(bes._fixture(str(uuid.uuid4()), True))), timeout=timeout))),
        ("service.get_user", lambda: _rpc(ctx, "GetUser", users.GetUserRequest(), users.GetUserResponse)),
        ("service.create_group", lambda: _rpc(ctx, "CreateGroup", groups.CreateGroupRequest(
            name="anonymous-smoke-" + uuid.uuid4().hex), groups.CreateGroupResponse)),
        ("service.get_api_keys", lambda: _rpc(ctx, "GetApiKeys", keys.GetApiKeysRequest(
            request_context=context, group_id=group_id), keys.GetApiKeysResponse)),
        ("service.create_api_key", lambda: _rpc(ctx, "CreateApiKey", keys.CreateApiKeyRequest(
            request_context=context, label="anonymous-smoke", capability=[cap.CACHE_WRITE]), keys.CreateApiKeyResponse)),
        ("invalid_api_key.cas", lambda: cas.FindMissingBlobs(re.FindMissingBlobsRequest(
            blob_digests=[digest]), timeout=timeout, metadata=bad_key)),
        ("invalid_api_key.service", lambda: _rpc(ctx, "GetUser", users.GetUserRequest(),
            users.GetUserResponse, metadata=bad_key)),
    ]
    return [("auth.denied." + name, lambda operation=operation: _denied(operation))
            for name, operation in calls] + [
        ("auth.denied.http_get_user", http_denial),
        ("auth.local_oauth_api_key_cas_ac_roundtrip", lambda: _local_authenticated_roundtrip(ctx)),
    ]
