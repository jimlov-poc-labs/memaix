# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-project Nextcloud account, created when a project is.

Each project gets its own Nextcloud user (`memaix-<name>`), so the files the
web UI and the nc_files_* tools reach are isolated by the account itself, not
just by path checks. The account's password is generated here, written to a
0600 file under the config dir and referenced from acl.yaml as `file:<path>`;
it is never returned, logged or put in an exception.

Enabled by an optional `nextcloud_provision` block in memaix.yaml
(docs/SECRETS.md, config/memaix.example.yaml). Without it nothing happens.

Two ways to create the user: `method: ocs` (default; Nextcloud's provisioning
API, which Nextcloud 32 refuses for app-password logins) or `method: occ_ssh`
(ssh to the Nextcloud host, where a forced command runs `occ user:add`; see
ops/memaix-nc-provision.sh). The password goes over ssh stdin, never argv.
"""

from __future__ import annotations

import os
import re
import secrets
import string
import subprocess
from pathlib import Path

from . import config

OCS_USERS = "/ocs/v2.php/cloud/users"
DEFAULT_QUOTA = "2GB"
_OK_CODES = {100, 200}
_ALREADY_EXISTS = 102
_REQUIRED_OCS = ("url", "admin_user", "admin_password_ref")
_REQUIRED_SSH = ("url", "ssh_host", "ssh_user", "ssh_key_file", "ssh_known_hosts")
_EXISTS_EXIT = 10
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
_ALPHABET = string.ascii_letters + string.digits
_PASSWORD_LEN = 32
_TIMEOUT_S = 15


class ProvisionError(Exception):
    """Provisioning failed. The message never contains the account password."""


class OutcomeUnknown(ProvisionError):
    """The remote command may have finished after we gave up; keep the secret file."""


def _http_post(url: str, *, timeout: float = 15, **kwargs):
    import requests

    return requests.post(url, timeout=timeout, **kwargs)


def _run(argv: list[str], *, input: str, timeout: float):
    return subprocess.run(argv, input=input, capture_output=True, text=True, timeout=timeout, check=False)


def _method(section: dict) -> str:
    method = section.get("method") or "ocs"
    if method not in ("ocs", "occ_ssh"):
        raise ProvisionError(f"nextcloud_provision.method must be ocs or occ_ssh, not {method!r}")
    return method


def _settings(cfg: dict | None) -> dict | None:
    section = cfg if cfg is not None else config.load()["memaix"].get("nextcloud_provision")
    if not section:
        return None
    required = _REQUIRED_SSH if _method(section) == "occ_ssh" else _REQUIRED_OCS
    missing = [k for k in required if not section.get(k)]
    if missing:
        raise ProvisionError(f"nextcloud_provision is missing: {', '.join(missing)}")
    return section


def _admin_password(settings: dict) -> str:
    try:
        password = config.secret(settings["admin_password_ref"])
    except (KeyError, ValueError, NotImplementedError) as exc:
        raise ProvisionError(f"admin password not available ({type(exc).__name__})") from None
    if not password:
        raise ProvisionError("admin password not available (empty)")
    return password


def _write_secret(path: Path, password: str) -> bool:
    """Create the 0600 secret file; False when one already exists (never overwritten)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as fh:
        fh.write(password)
    return True


def _ocs_status(resp) -> int:
    try:
        return int(resp.json()["ocs"]["meta"]["statuscode"])
    except (ValueError, KeyError, TypeError):
        raise ProvisionError(f"unexpected Nextcloud response (HTTP {resp.status_code})") from None


def _create_user(settings: dict, http, user: str, password: str, name: str) -> bool:
    """True when created, False when the user already exists."""
    try:
        resp = http(
            settings["url"].rstrip("/") + OCS_USERS,
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
            auth=(settings["admin_user"], _admin_password(settings)),
            data={
                "userid": user,
                "password": password,
                "displayName": f"Memaix {name}",
                "quota": settings.get("quota") or DEFAULT_QUOTA,
            },
            timeout=_TIMEOUT_S,
        )
    except OSError as exc:  # requests' exceptions derive from OSError
        raise ProvisionError(f"Nextcloud unreachable ({type(exc).__name__})") from None
    status = _ocs_status(resp)
    if status == _ALREADY_EXISTS:
        return False
    if status not in _OK_CODES:
        raise ProvisionError(f"Nextcloud refused to create {user} (OCS status {status})")
    return True


def _create_user_ssh(settings: dict, run, user: str, password: str, name: str) -> bool:
    """True when created, False when the user already exists."""
    argv = [
        "ssh", "-i", str(settings["ssh_key_file"]),
        "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={settings['ssh_known_hosts']}",
        "-p", str(settings.get("ssh_port") or 22),
        f"{settings['ssh_user']}@{settings['ssh_host']}",
        "user-add", user, str(settings.get("quota") or DEFAULT_QUOTA), f"Memaix {name}",
    ]
    try:
        proc = run(argv, input=password + "\n", timeout=_TIMEOUT_S * 2)
    except subprocess.TimeoutExpired:
        raise OutcomeUnknown(
            f"ssh to the Nextcloud host timed out; {user} may exist. The secret file nc-{name} was kept; "
            "check the account, then remove whichever is stale before retrying"
        ) from None
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProvisionError(f"Nextcloud host unreachable over ssh ({type(exc).__name__})") from None
    if proc.returncode == _EXISTS_EXIT:
        return False
    if proc.returncode != 0:
        raise ProvisionError(f"occ over ssh refused to create {user} (exit {proc.returncode})")
    return True


def provision_project_files(
    name: str, *, cfg: dict | None = None, http=None, run=None
) -> dict | None:
    """Create the project's Nextcloud account and return its `files:` resource.

    `cfg` is the `nextcloud_provision` block (read from memaix.yaml when None).
    Returns None only when provisioning is not configured. Raises
    ProvisionError on any failure — including an account or secret file that
    already exists, which is never overwritten — leaving no new secret file
    behind."""
    settings = _settings(cfg)
    if settings is None:
        return None
    if not isinstance(name, str) or not _NAME.match(name):
        raise ProvisionError(f"invalid project name: {name!r}")
    user = f"memaix-{name}"
    secret_path = config.CONFIG_DIR / "secrets" / f"nc-{name}"
    password = "".join(secrets.choice(_ALPHABET) for _ in range(_PASSWORD_LEN))
    # File first: if it cannot be written there is no account whose password is lost.
    if not _write_secret(secret_path, password):
        raise ProvisionError(f"a secret file for {name} already exists; remove it or the stale account first")
    try:
        if _method(settings) == "occ_ssh":
            created = _create_user_ssh(settings, run or _run, user, password, name)
        else:
            created = _create_user(settings, http or _http_post, user, password, name)
    except OutcomeUnknown:
        raise
    except BaseException:
        secret_path.unlink(missing_ok=True)
        raise
    if not created:
        secret_path.unlink(missing_ok=True)
        raise ProvisionError(f"the Nextcloud account {user} already exists; remove it first")
    return {
        "url": f"{settings['url'].rstrip('/')}/remote.php/dav/files/{user}/",
        "user": user,
        "password_ref": f"file:{secret_path}",
    }
