# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-project Nextcloud account, created when a project is.

Each project gets its own Nextcloud user (`memaix-<name>`), so the files the
web UI and the nc_files_* tools reach are isolated by the account itself, not
just by path checks. The account's password is generated here, written to a
0600 file under the config dir and referenced from acl.yaml as `file:<path>`;
it is never returned, logged or put in an exception.

Enabled by an optional `nextcloud_provision` block in memaix.yaml
(docs/SECRETS.md, config/memaix.example.yaml). Without it nothing happens,
unless the top-level `require_files: true` flag is set: then a project can
only be created with a file area, and a missing block is an error.

Two ways to create the user: `method: ocs` (default; Nextcloud's provisioning
API, which Nextcloud 32 refuses for app-password logins) or `method: occ_ssh`
(ssh to the Nextcloud host, where a forced command runs `occ user:add`; see
ops/memaix-nc-provision.sh). The password goes over ssh stdin, never argv.
"""

from __future__ import annotations

import difflib
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
_TIMEOUT_S = 15  # ssh path's own timeout; used as _TIMEOUT_S * 2 there, unaffected by the OCS bump below
# A fresh Nextcloud install's first OCS call pays an opcache/cache warm-up
# cost (~11.5s measured, vs ~2.2s afterwards); 25s covers that with headroom
# without masking a genuinely unreachable host. OCS-only: the ssh path keeps
# its own, already generous, timeout above.
_OCS_TIMEOUT_S = 25


_MISSING_BLOCK = (
    "require_files är på men nextcloud_provision saknas i memaix.yaml; "
    "lägg till nextcloud_provision eller sätt require_files: false"
)


# Same grammar the forced-command script enforces (ops/memaix-nc-provision.sh): max 20GB.
# re.ASCII: \d must not match other Unicode digits (fullwidth, Arabic-Indic).
_SSH_QUOTA_RE = re.compile(r"(?:(?:[1-9]|1\d|20)GB|[1-9]\d{0,3}MB)", re.ASCII)


def _ssh_quota(raw: object) -> str:
    """Whitespace-free quota for the occ_ssh argv; anything off-grammar fails closed."""
    quota = "".join(str(raw or DEFAULT_QUOTA).split())
    if not _SSH_QUOTA_RE.fullmatch(quota):
        raise ProvisionError("nextcloud_provision quota must look like 2GB or 500MB (max 20GB)")
    return quota


class ProvisionError(Exception):
    """Provisioning failed. The message never contains the account password."""


class OutcomeUnknown(ProvisionError):
    """The remote command may have finished after we gave up; keep the secret file.

    Raised for the OCS path on every requests failure that isn't provably
    pre-send: a read/response timeout, ChunkedEncodingError,
    ContentDecodingError, and a requests.exceptions.ConnectionError unless
    it carries the MaxRetryError/NewConnectionError signal that proves a
    refused connection or DNS failure (see
    _is_proven_presend_connection_error). Those proven pre-send cases —
    ConnectTimeout, the refused/DNS-failure shape of ConnectionError, and
    the request-building errors MissingSchema/InvalidSchema/InvalidURL/
    InvalidHeader — get a plain ProvisionError instead, because they cannot
    have created the account. Everything else defaults to OutcomeUnknown
    rather than being guessed at: see the comments in _create_user. Also
    raised for the ssh path on a timeout, for the same "may have finished
    anyway" reason.
    """


def _http_post(url: str, *, timeout: float = _OCS_TIMEOUT_S, **kwargs):
    import requests

    return requests.post(url, timeout=timeout, **kwargs)


def _http_get(url: str, *, timeout: float = _OCS_TIMEOUT_S, **kwargs):
    import requests

    return requests.get(url, timeout=timeout, **kwargs)


def _run(argv: list[str], *, input: str, timeout: float):
    return subprocess.run(argv, input=input, capture_output=True, text=True, timeout=timeout, check=False)


def _method(section: dict) -> str:
    method = section.get("method") or "ocs"
    if method not in ("ocs", "occ_ssh"):
        raise ProvisionError(f"nextcloud_provision.method must be ocs or occ_ssh, not {method!r}")
    return method


def files_required(memaix_cfg: dict | None = None) -> bool:
    """Value of the top-level `require_files` flag in memaix.yaml (default False).

    Strict: only a real bool is accepted. Anything else ('false', 1, null, ...)
    raises, so a typo can never silently turn the guarantee on or off."""
    if memaix_cfg is None:
        memaix_cfg = config.load()["memaix"]
    if not isinstance(memaix_cfg, dict):
        raise ProvisionError("memaix.yaml must be a mapping at the top level")
    if "require_files" not in memaix_cfg:
        for key in memaix_cfg:
            if not isinstance(key, str):
                continue
            norm = key.strip().lower().replace("-", "_").replace(" ", "_")
            if norm == "require_files" or difflib.get_close_matches(norm, ["require_files"], n=1, cutoff=0.85):
                raise ProvisionError(
                    f"memaix.yaml has the key {key!r}, which looks like a misspelling of require_files; "
                    "fix the key name"
                )
        return False
    value = memaix_cfg["require_files"]
    if not isinstance(value, bool):
        raise ProvisionError("require_files in memaix.yaml must be true or false (a YAML boolean)")
    return value


def ensure_files_possible() -> None:
    """Preflight for project creation: nothing is created before this passes.

    No-op unless `require_files` is on; then `nextcloud_provision` must be a
    complete block, else ProvisionError. Invalid flag values also raise."""
    memaix_cfg = config.load()["memaix"]
    if not files_required(memaix_cfg):
        return
    section = memaix_cfg.get("nextcloud_provision")
    if not section or not isinstance(section, dict):
        raise ProvisionError(_MISSING_BLOCK)
    _settings(section)


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


def _is_proven_presend_connection_error(exc) -> bool:
    """True when a requests.exceptions.ConnectionError is provably "never
    reached the socket" rather than "reached it, then something broke".

    This is the same signal requests' own HTTPAdapter.send() uses
    internally to recognise a connect-phase failure (see the ConnectTimeout
    special-case there: `isinstance(e.reason, ConnectTimeoutError) and not
    isinstance(e.reason, NewConnectionError)`): a refused connection or a
    DNS failure surfaces as urllib3.exceptions.MaxRetryError wrapping a
    NewConnectionError (urllib3.exceptions.NameResolutionError is a
    NewConnectionError subclass, so DNS failures match too).

    Verified empirically (not just read from the source) against three
    real cases on this machine: a closed local TCP port (connection
    refused), a `.invalid` hostname (DNS failure) — both match this check —
    and a local server that accepts the connection, reads the request, then
    closes without replying — which raises a bare urllib3.ProtocolError
    with no MaxRetryError wrapper and correctly does NOT match. That last
    shape is the genuinely unknown "sent but the reply was lost" case and
    must stay OutcomeUnknown; see the caller.
    """
    from urllib3.exceptions import MaxRetryError, NewConnectionError

    cause = exc.args[0] if exc.args else None
    return isinstance(cause, MaxRetryError) and isinstance(cause.reason, NewConnectionError)


def _create_user(settings: dict, http, user: str, password: str, name: str) -> bool:
    """True when created, False when the user already exists."""
    import requests

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
            timeout=_OCS_TIMEOUT_S,
        )
    except requests.exceptions.ConnectTimeout as exc:
        # The TCP/TLS handshake itself never completed — no request body was
        # ever sent, so the account cannot have been created. This is the one
        # failure requests documents as connect-phase-only ("safe to retry"),
        # so it is proven to belong on the plain-ProvisionError side rather
        # than the unknown-outcome side below.
        raise ProvisionError(f"Nextcloud unreachable ({type(exc).__name__})") from None
    except (
        requests.exceptions.MissingSchema,
        requests.exceptions.InvalidSchema,
        requests.exceptions.InvalidURL,
        requests.exceptions.InvalidHeader,
    ) as exc:
        # Raised while requests builds/validates the PreparedRequest, before
        # any socket is ever opened — a malformed nextcloud_provision.url or
        # similar config problem, not a network failure. Provably pre-send,
        # same bucket as ConnectTimeout.
        raise ProvisionError(f"Nextcloud request malformed ({type(exc).__name__})") from None
    except requests.exceptions.ConnectionError as exc:
        # requests.adapters.HTTPAdapter.send() has a single except clause —
        # `except (ProtocolError, OSError) as err: raise ConnectionError(err,
        # request=request)` — that turns both "the socket never connected"
        # (e.g. connection refused, DNS failure) and "the connection broke
        # after the request was already written" (e.g. RemoteDisconnected)
        # into the same requests.exceptions.ConnectionError. But unlike the
        # generic case, refused/DNS failures carry a reliable signal: they
        # reach here via urllib3.exceptions.MaxRetryError wrapping a
        # NewConnectionError, the same shape requests' own ConnectTimeout
        # branch keys off internally — see _is_proven_presend_connection_error
        # for the check and how it was verified against real sockets, not
        # just read from source. A bare ProtocolError (no MaxRetryError
        # wrapper) is the genuinely ambiguous "sent, then lost the reply"
        # case and has no such signal, so it falls through to OutcomeUnknown.
        if _is_proven_presend_connection_error(exc):
            raise ProvisionError(f"Nextcloud unreachable ({type(exc).__name__})") from None
        raise OutcomeUnknown(
            f"Nextcloud account creation for {user} failed with {type(exc).__name__}, and the connection "
            "may have reached Nextcloud before it broke; the account may have been created anyway. "
            f"The secret file nc-{name} was kept; check the account, then remove whichever is stale "
            "before retrying"
        ) from None
    except OSError as exc:
        # Everything else requests/urllib3 can raise from this call —
        # ReadTimeout, ChunkedEncodingError, ContentDecodingError, and
        # whatever else inherits from OSError here but isn't one of the
        # provably pre-send cases handled above — is NOT provably a "never
        # reached Nextcloud" failure, so none of it gets the easy answer of
        # a plain ProvisionError (which deletes the secret file).
        #
        # Anything landing here defaults to OutcomeUnknown rather than being
        # sorted case by case: a false positive here costs one manual
        # account check before retrying; a false negative (treating an
        # orphaned account as "clearly failed, safe to delete the secret and
        # retry") costs a silently broken login the next person hits. The
        # cheaper mistake is the one this code makes.
        raise OutcomeUnknown(
            f"Nextcloud account creation for {user} failed with {type(exc).__name__}, and the connection "
            "may have reached Nextcloud before it broke; the account may have been created anyway. "
            f"The secret file nc-{name} was kept; check the account, then remove whichever is stale "
            "before retrying"
        ) from None
    status = _ocs_status(resp)
    if status == _ALREADY_EXISTS:
        return False
    if status not in _OK_CODES:
        raise ProvisionError(f"Nextcloud refused to create {user} (OCS status {status})")
    return True


def _probe_adopted_account(settings: dict, http, user: str, password: str) -> bool:
    """True only when `password` is this exact account's current working
    password, confirmed live against Nextcloud's own user-info endpoint
    (GET, never a write). Used to decide whether a secret file left behind by
    an OCS OutcomeUnknown (a timeout whose request may have gone through
    anyway) matches a real, usable account — i.e. a self-healing no-op rather
    than a stale conflict. Any failure (network, auth, unexpected response)
    returns False and the caller falls back to the existing fail-closed
    behaviour; this function never raises."""
    import requests

    try:
        resp = http(
            settings["url"].rstrip("/") + OCS_USERS + f"/{user}",
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
            auth=(user, password),
            timeout=_OCS_TIMEOUT_S,
        )
    except (requests.exceptions.RequestException, OSError):
        return False
    if getattr(resp, "status_code", None) != 200:
        return False
    try:
        if _ocs_status(resp) not in _OK_CODES:
            return False
        # Auth succeeding only proves *some* account named `user` accepted this
        # password; also check the identity Nextcloud reports back matches, so
        # a same-named-but-different account (however unlikely) is never adopted.
        return resp.json()["ocs"]["data"]["id"] == user
    except (ProvisionError, KeyError, TypeError):
        return False


def _create_user_ssh(settings: dict, run, user: str, password: str, name: str) -> bool:
    """True when created, False when the user already exists."""
    argv = [
        "ssh", "-i", str(settings["ssh_key_file"]),
        "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={settings['ssh_known_hosts']}",
        "-p", str(settings.get("ssh_port") or 22),
        f"{settings['ssh_user']}@{settings['ssh_host']}",
        "user-add", user, _ssh_quota(settings.get("quota")), f"Memaix {name}",
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
    Returns None only when provisioning is not configured and `require_files`
    is not on (when it is, that is a ProvisionError). Raises
    ProvisionError on any failure — including an account or secret file that
    already exists, which is never overwritten — leaving no new secret file
    behind.

    OCS exception: if a secret file already exists but a live check (GET
    against Nextcloud, with that file's own password) confirms the matching
    account already works, this is treated as an earlier, already-successful
    provisioning — not a conflict — and the same dict a fresh success would
    have returned is handed back, with no write of any kind. This only
    adopts an account whose password matches the local file exactly; it can
    never apply to anyone else's account, since our generated password is
    cryptographically unique per run."""
    required = files_required()
    settings = _settings(cfg)
    if settings is None:
        if required:
            raise ProvisionError(_MISSING_BLOCK)
        return None
    if not isinstance(name, str) or not _NAME.match(name):
        raise ProvisionError(f"invalid project name: {name!r}")
    user = f"memaix-{name}"
    secret_path = config.CONFIG_DIR / "secrets" / f"nc-{name}"
    password = "".join(secrets.choice(_ALPHABET) for _ in range(_PASSWORD_LEN))
    # File first: if it cannot be written there is no account whose password is lost.
    if not _write_secret(secret_path, password):
        # A leftover file from an earlier OutcomeUnknown (OCS path only) may be
        # legitimate: the matching account could already exist and work, left
        # by a request that actually succeeded before we gave up waiting on it.
        # Confirm that live, with the file's own password, before treating this
        # as adopted — never on the strength of the file's mere presence, and
        # never for occ_ssh, which has its own account-check pattern.
        if _method(settings) != "occ_ssh":
            # Resolve exactly how the password will later be resolved at use (file:
            # scheme, config.secret() -- which also strips), so "the probe passed"
            # and "the account actually works at runtime" can never disagree.
            try:
                existing_password = config.secret(f"file:{secret_path}") or ""
            except (KeyError, OSError):
                existing_password = ""  # nosec B105 -- empty sentinel meaning "unreadable", not a credential
            if existing_password and _probe_adopted_account(settings, http or _http_get, user, existing_password):
                return {
                    "url": f"{settings['url'].rstrip('/')}/remote.php/dav/files/{user}/",
                    "user": user,
                    "password_ref": f"file:{secret_path}",
                }
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
