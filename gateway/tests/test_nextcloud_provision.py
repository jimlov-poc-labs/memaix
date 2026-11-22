# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-project Nextcloud account provisioning. HTTP is injected; nothing
touches the network. The generated password must never leave the 0600 file."""

from __future__ import annotations

import stat
import subprocess

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


def test_ocs_timeout_raises_outcome_unknown_and_keeps_secret(cfg_dir):
    from memaix_gateway.nextcloud_provision import OutcomeUnknown

    boom = requests.exceptions.ReadTimeout("read timed out")
    with pytest.raises(OutcomeUnknown, match="may have been created anyway") as ei:
        provision_project_files("acme", cfg=CFG, http=_Http(exc=boom))
    path = _secret_file(cfg_dir)
    assert path.exists()
    password = path.read_text()
    assert password not in str(ei.value)
    assert ei.value.__cause__ is None and ei.value.__suppress_context__


def test_ocs_connection_error_still_raises_plain_provision_error_and_removes_secret(cfg_dir):
    from memaix_gateway.nextcloud_provision import OutcomeUnknown

    boom = requests.exceptions.ConnectionError("connection refused")
    with pytest.raises(ProvisionError) as err:
        provision_project_files("acme", cfg=CFG, http=_Http(exc=boom))
    assert not isinstance(err.value, OutcomeUnknown)
    assert not _secret_file(cfg_dir).exists()


def test_ocs_connect_timeout_is_treated_as_outcome_unknown_too(cfg_dir):
    """requests.exceptions.ConnectTimeout is both a ConnectionError and a Timeout.
    This task deliberately treats it as a Timeout (OutcomeUnknown, secret kept)
    even though the TCP handshake never completing arguably means the account
    cannot exist; see report ("Behöver beslut") for the tradeoff. Pins the
    current, intentional behaviour so a future change is a visible decision,
    not a silent side effect of reordering the except clauses."""
    boom = requests.exceptions.ConnectTimeout("connect timed out")
    with pytest.raises(ProvisionError, match="may have been created anyway"):
        provision_project_files("acme", cfg=CFG, http=_Http(exc=boom))
    assert _secret_file(cfg_dir).exists()


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


# --- method: occ_ssh -------------------------------------------------------

SSH_CFG = {
    "method": "occ_ssh",
    "url": "https://nc.example/",
    "ssh_host": "nc-host",
    "ssh_user": "provision",
    "ssh_key_file": "/keys/prov",
    "ssh_known_hosts": "/keys/known_hosts",
}


class _Proc:
    def __init__(self, returncode=0):
        self.returncode = returncode


class _Run:
    def __init__(self, returncode=0, exc=None):
        self.returncode, self.exc, self.calls = returncode, exc, []

    def __call__(self, argv, *, input, timeout):
        self.calls.append((argv, input))
        if self.exc:
            raise self.exc
        return _Proc(self.returncode)


def test_ssh_success_sends_password_on_stdin_never_argv(cfg_dir):
    run = _Run()
    out = provision_project_files("acme", cfg={**SSH_CFG, "quota": "5GB"}, run=run)

    path = _secret_file(cfg_dir)
    assert out["user"] == "memaix-acme"
    assert out["url"] == "https://nc.example/remote.php/dav/files/memaix-acme/"
    password = path.read_text()
    (argv, stdin), = run.calls
    assert stdin == password + "\n"
    assert all(password not in a for a in argv)
    assert argv[-4:] == ["user-add", "memaix-acme", "5GB", "Memaix acme"]
    assert "StrictHostKeyChecking=yes" in argv and "BatchMode=yes" in argv
    assert "UserKnownHostsFile=/keys/known_hosts" in argv


def test_ssh_default_quota_and_no_ocs_call():
    run, http = _Run(), _Http()
    provision_project_files("acme", cfg=SSH_CFG, run=run, http=http)
    assert run.calls[0][0][-2] == "2GB"
    assert http.calls == []


def test_ssh_existing_user_raises_and_leaves_no_file(cfg_dir):
    with pytest.raises(ProvisionError, match="already exists"):
        provision_project_files("acme", cfg=SSH_CFG, run=_Run(10))
    assert not _secret_file(cfg_dir).exists()


def test_ssh_failure_exit_is_reported_without_password(cfg_dir):
    with pytest.raises(ProvisionError, match=r"exit 255"):
        provision_project_files("acme", cfg=SSH_CFG, run=_Run(255))
    assert not _secret_file(cfg_dir).exists()


def test_ssh_unreachable_removes_secret(cfg_dir):
    with pytest.raises(ProvisionError, match="unreachable"):
        provision_project_files("acme", cfg=SSH_CFG, run=_Run(exc=FileNotFoundError()))
    assert not _secret_file(cfg_dir).exists()


@pytest.mark.parametrize("missing", ["url", "ssh_host", "ssh_user", "ssh_key_file", "ssh_known_hosts"])
def test_ssh_missing_setting_raises(missing):
    cfg = {k: v for k, v in SSH_CFG.items() if k != missing}
    with pytest.raises(ProvisionError, match=missing):
        provision_project_files("acme", cfg=cfg, run=_Run())


def test_unknown_method_raises():
    with pytest.raises(ProvisionError, match="method"):
        provision_project_files("acme", cfg={**SSH_CFG, "method": "magic"}, run=_Run())


def test_ssh_timeout_keeps_secret_file_because_the_account_may_exist(cfg_dir):
    run = _Run(exc=subprocess.TimeoutExpired(cmd="ssh", timeout=30))
    with pytest.raises(ProvisionError, match="may exist") as ei:
        provision_project_files("acme", cfg=SSH_CFG, run=run)
    assert _secret_file(cfg_dir).exists()
    assert _secret_file(cfg_dir).read_text() not in str(ei.value)


# ------------------------------------------------------------ require_files


def _mx(monkeypatch, memaix):
    monkeypatch.setattr(config, "load", lambda: {"memaix": memaix})


def test_require_files_on_without_block_is_an_error(monkeypatch, cfg_dir):
    _mx(monkeypatch, {"require_files": True})
    http = _Http()
    with pytest.raises(ProvisionError, match="require_files.*nextcloud_provision saknas"):
        provision_project_files("acme", http=http)
    assert http.calls == [] and not cfg_dir.exists()


def test_require_files_on_with_block_provisions(monkeypatch, cfg_dir):
    _mx(monkeypatch, {"require_files": True, "nextcloud_provision": CFG})
    assert provision_project_files("acme", http=_Http())["user"] == "memaix-acme"


@pytest.mark.parametrize("memaix", [{}, {"require_files": False}])
def test_require_files_off_or_undefined_keeps_silent_skip(monkeypatch, memaix):
    _mx(monkeypatch, memaix)
    assert provision_project_files("acme", http=_Http()) is None


@pytest.mark.parametrize("bad", ["false", "true", "False", 1, 0, None, [], {}, ""])
def test_require_files_must_be_a_real_bool(monkeypatch, cfg_dir, bad):
    _mx(monkeypatch, {"require_files": bad, "nextcloud_provision": CFG})
    http = _Http()
    with pytest.raises(ProvisionError, match="require_files.*true or false"):
        provision_project_files("acme", http=http)
    assert http.calls == [] and not cfg_dir.exists()


@pytest.mark.parametrize("key", ["require_file", "Require_Files", "require-files", "require_files ", "requires_files"])
def test_misspelled_require_files_key_fails_closed(monkeypatch, key):
    _mx(monkeypatch, {key: True, "nextcloud_provision": CFG})
    with pytest.raises(ProvisionError, match="misspelling of require_files"):
        provision_project_files("acme", http=_Http())
    with pytest.raises(ProvisionError, match="misspelling"):
        np_.ensure_files_possible()


@pytest.mark.parametrize("key", ["brand", "nextcloud_provision", "auth", "onboarding", "model", "files"])
def test_unrelated_keys_do_not_trigger_misspelling_check(key):
    assert np_.files_required({key: {}}) is False


@pytest.mark.parametrize("bad", [None, [], "x", 3])
def test_non_mapping_memaix_config_fails_closed(bad):
    with pytest.raises(ProvisionError, match="mapping"):
        np_.files_required(bad if bad is not None else _NONE)


_NONE = object()


@pytest.mark.parametrize("block", [None, {}, "x", []])
def test_preflight_refuses_missing_or_malformed_block(monkeypatch, block):
    _mx(monkeypatch, {"require_files": True, "nextcloud_provision": block})
    with pytest.raises(ProvisionError, match="nextcloud_provision saknas"):
        np_.ensure_files_possible()


def test_preflight_is_noop_when_off_and_passes_with_block(monkeypatch):
    _mx(monkeypatch, {})
    np_.ensure_files_possible()
    _mx(monkeypatch, {"require_files": True, "nextcloud_provision": CFG})
    np_.ensure_files_possible()


def test_ssh_quota_with_a_space_is_sent_as_one_word():
    run = _Run()
    provision_project_files("acme", cfg={**SSH_CFG, "quota": "2 GB"}, run=run)
    assert run.calls[0][0][-2] == "2GB"


@pytest.mark.parametrize("raw,sent", [("2 GB", "2GB"), (" 500 MB ", "500MB"), ("20GB", "20GB"), ("2\tGB", "2GB")])
def test_ssh_quota_is_normalised_to_one_word(raw, sent):
    run = _Run()
    provision_project_files("acme", cfg={**SSH_CFG, "quota": raw}, run=run)
    assert run.calls[0][0][-2] == sent


@pytest.mark.parametrize(
    "bad",
    ["2GB; rm -rf /", "2GB$(id)", "`id`", "2GB\nfoo", "2 G B x", "21GB", "0GB", "2TB", "none", "unlimited",
     "-1GB", "2gb", "GB", "2.5GB", "99999MB", "$IFS", "2GB|id", "2GB&&id"],
)
def test_ssh_quota_invalid_fails_closed_before_ssh(bad, cfg_dir):
    run = _Run()
    with pytest.raises(ProvisionError):
        provision_project_files("acme", cfg={**SSH_CFG, "quota": bad}, run=run)
    assert run.calls == []
