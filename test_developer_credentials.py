"""Developer access fails closed when credentials are not configured."""
import ast
import base64
import hashlib
import hmac
import secrets
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials


def functions(password="", secret=""):
    tree = ast.parse(Path("main.py").read_text())
    selected = {"authenticate", "developer_request_is_authorized", "create_developer_session_token",
                "developer_session_access_level"}
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in selected]
    namespace = dict(PASSWORD=password, USERNAME="developer@example.test", DEVELOPER_SESSION_SECRET=secret,
                     DEVELOPER_SESSION_COOKIE="developer", DEVELOPER_SESSION_TTL_SECONDS=3600,
                     DEVELOPER_ACCESS_LEVELS={"full_access", "full_review"},
                     time=SimpleNamespace(time=lambda:10000), secrets=secrets, base64=base64,
                     hashlib=hashlib, hmac=hmac, HTTPException=HTTPException, Request=Request,
                     HTTPBasicCredentials=HTTPBasicCredentials, Depends=Depends, security=HTTPBasic())
    exec(compile(ast.Module(body=nodes,type_ignores=[]),"credential_routes","exec"), namespace)
    return namespace


def request(password, cookie=""):
    token=base64.b64encode(("developer@example.test:"+password).encode()).decode()
    return Request({"type":"http","headers":[(b"authorization",("Basic "+token).encode()),
                                               (b"cookie",("developer="+cookie).encode())]})


@pytest.mark.parametrize("password,secret",[("",""),("","test-session-secret"),("test-configured-password","")])
def test_missing_credentials_cannot_create_or_accept_sessions(password,secret):
    ns=functions(password,secret)
    with pytest.raises(HTTPException) as result:ns["create_developer_session_token"]()
    assert result.value.status_code==503
    assert ns["developer_session_access_level"](request(password,"10000.full_access.fake"))==""
    if not password:
        assert not ns["developer_request_is_authorized"](request(password))
        with pytest.raises(HTTPException):ns["authenticate"](HTTPBasicCredentials(username="developer@example.test",password=password))


def test_configured_credentials_and_signed_sessions_still_work():
    ns=functions("test-configured-password","test-session-secret")
    ns["authenticate"](HTTPBasicCredentials(username="developer@example.test",password="test-configured-password"))
    assert ns["developer_request_is_authorized"](request("test-configured-password"))
    assert not ns["developer_request_is_authorized"](request("incorrect"))
    token=ns["create_developer_session_token"]("full_review")
    assert ns["developer_session_access_level"](request("test-configured-password",token))=="full_review"
    assert ns["developer_session_access_level"](request("test-configured-password",token+"tampered"))==""


def test_unset_environment_does_not_supply_a_developer_password(monkeypatch):
    import os
    monkeypatch.delenv("DEVELOPER_PASSWORD",raising=False)
    tree=ast.parse(Path("main.py").read_text())
    nodes=[node for node in tree.body if isinstance(node,ast.Assign)
           and any(isinstance(target,ast.Name) and target.id=="PASSWORD" for target in node.targets)]
    ns={"os":os}
    exec(compile(ast.Module(body=nodes,type_ignores=[]),"credentials","exec"),ns)
    assert ns["PASSWORD"]==""
