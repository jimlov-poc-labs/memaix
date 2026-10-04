# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-project Nextcloud account provisioning. HTTP is injected; nothing
touches the network. The generated password must never leave the 0600 file."""

from __future__ import annotations

import stat

import pytest
import requests

from memaix_gateway import config, nextcloud_provision as np_
from memaix_gateway.nextcloud_provision import ProvisionError, provision_project_files

CFG = {"url": "https://nc.example/", "admin_user": "root", "admin_password_ref": "env:NC_ADMIN_PW"}
ADMIN_PW = "admin-secret-value"


class _Resp:
    status_code = 200

    def __init__(self, code=200, payload=None):
        self._payload = payload if payload is not None else {"ocs": {"meta": {"statuscode": code}}}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Http:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.calls = resp or _Resp(), exc, []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.exc:
            raise self.exc
        return self.resp


@pytest.fixture(autouse=True)
def cfg_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setenv("NC_ADMIN_PW", ADMIN_PW)
    return tmp_path / "cfg"


def _secret_file(cfg_dir, name="acme"):
    return cfg_dir / "secrets" / f"nc-{name}"


def test_success_creates_user_and_0600_secret(cfg_dir):
    http = _Http()
    out = provision_project_files("acme", cfg={**CFG, "quota": "5GB"}, http=http)

    path = _secret_file(cfg_dir)
    assert out == {
        "url": "https://nc.example/remote.php/dav/files/memaix-acme/",
        "user": "memaix-acme",
        "password_ref": f"file:{path}",
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    password = path.read_text()
    assert len(password) == 32 and password.isalnum()

    (url, kw), = http.calls
    assert url == "https://nc.example/ocs/v2.php/cloud/users"
    assert kw["headers"] == {"OCS-APIRequest": "true", "Accept": "application/json"}
    assert kw["auth"] == ("root", ADMIN_PW)
    assert kw["data"] == {
        "userid": "memaix-acme", "password": password, "displayName": "Memaix acme", "quota": "5GB",
    }


def test_default_quota_is_2gb():
    http = _Http()
    provision_project_files("acme", cfg=CFG, http=http)
    assert http.calls[0][1]["data"]["quota"] == "2GB"


def test_password_is_not_in_the_return_value():
    out = provision_project_files("acme", cfg=CFG, http=_Http())
    password = _secret_file(config.CONFIG_DIR.parent / "cfg").read_text()
    assert password not in repr(out)


def test_secret_in_returned_ref_resolves_through_config_secret():
    out = provision_project_files("acme", cfg=CFG, http=_Http())
    assert len(config.secret(out["password_ref"])) == 32


def test_existing_user_raises_and_leaves_no_file(cfg_dir):
    with pytest.raises(ProvisionError, match="already exists"):
        provision_project_files("acme", cfg=CFG, http=_Http(_Resp(102)))
    assert not _secret_file(cfg_dir).exists()


def test_existing_secret_file_is_never_overwritten(cfg_dir):
    path = _secret_file(cfg_dir)
    path.parent.mkdir(parents=True)
    path.write_text("keep-me")
    http = _Http()
    with pytest.raises(ProvisionError, match="already exists"):
        provision_project_files("acme", cfg=CFG, http=http)
    assert path.read_text() == "keep-me"
    assert http.calls == []


def test_unconfigured_returns_none(monkeypatch, cfg_dir):
    http = _Http()
    assert provision_project_files("acme", cfg={}, http=http) is None
    monkeypatch.setattr(config, "load", lambda: {"memaix": {}})
    assert provision_project_files("acme", http=http) is None
    assert http.calls == [] and not cfg_dir.exists()


def test_config_is_read_from_memaix_yaml_when_cfg_omitted(monkeypatch):
    monkeypatch.setattr(config, "load", lambda: {"memaix": {"nextcloud_provision": CFG}})
    http = _Http()
    assert provision_project_files("acme", http=http)["user"] == "memaix-acme"


@pytest.mark.parametrize("missing", ["url", "admin_user", "admin_password_ref"])
def test_incomplete_config_is_an_error(missing, cfg_dir):
    cfg = {k: v for k, v in CFG.items() if k != missing}
    with pytest.raises(ProvisionError, match=missing):
        provision_project_files("acme", cfg=cfg, http=_Http())
    assert not cfg_dir.exists()


@pytest.mark.parametrize("name", ["", "A", "../x", "a/b", "x" * 40])
def test_invalid_name_is_refused(name, cfg_dir):
    with pytest.raises(ProvisionError):
        provision_project_files(name, cfg=CFG, http=_Http())
    assert not cfg_dir.exists()


def test_ocs_failure_leaves_no_file_and_no_password_in_error(cfg_dir):
    with pytest.raises(ProvisionError) as err:
        provision_project_files("acme", cfg=CFG, http=_Http(_Resp(997)))
    assert "997" in str(err.value)
    assert not _secret_file(cfg_dir).exists()


def test_unreachable_nextcloud_leaves_no_file_and_hides_details(cfg_dir):
    boom = requests.ConnectionError(f"failed for auth root:{ADMIN_PW}")
    with pytest.raises(ProvisionError) as err:
        provision_project_files("acme", cfg=CFG, http=_Http(exc=boom))
    assert ADMIN_PW not in str(err.value) and "ConnectionError" in str(err.value)
    assert err.value.__cause__ is None and err.value.__suppress_context__
    assert not _secret_file(cfg_dir).exists()


def test_non_json_response_is_an_error(cfg_dir):
    with pytest.raises(ProvisionError, match="unexpected"):
        provision_project_files("acme", cfg=CFG, http=_Http(_Resp(payload=ValueError("not json"))))
    assert not _secret_file(cfg_dir).exists()


def test_missing_admin_secret_is_an_error_without_file(cfg_dir, monkeypatch):
    monkeypatch.delenv("NC_ADMIN_PW")
    with pytest.raises(ProvisionError, match="admin password"):
        provision_project_files("acme", cfg=CFG, http=_Http())
    assert not _secret_file(cfg_dir).exists()


def test_password_never_appears_in_exception_text(cfg_dir, monkeypatch):
    seen = []
    real = np_._write_secret

    def spy(path, password):
        seen.append(password)
        return real(path, password)

    monkeypatch.setattr(np_, "_write_secret", spy)
    with pytest.raises(ProvisionError) as err:
        provision_project_files("acme", cfg=CFG, http=_Http(_Resp(997)))
    assert seen and seen[0] not in str(err.value)
    assert seen[0] not in repr(err.value.args)


def test_unwritable_secret_dir_creates_no_account(cfg_dir):
    cfg_dir.parent.mkdir(parents=True, exist_ok=True)
    cfg_dir.write_text("a file where the directory should be")
    http = _Http()
    with pytest.raises(OSError):
        provision_project_files("acme", cfg=CFG, http=http)
    assert http.calls == []


def test_unset_admin_password_raises_without_creating_anything(cfg_dir, monkeypatch):
    monkeypatch.delenv("NC_ADMIN_PW_UNSET", raising=False)
    cfg = {**CFG, "admin_password_ref": "env:NC_ADMIN_PW_UNSET"}
    http = _Http()
    with pytest.raises(ProvisionError, match="admin password"):
        provision_project_files("acme", cfg=cfg, http=http)
    assert http.calls == [] and not _secret_file(cfg_dir).exists()
