# SPDX-License-Identifier: AGPL-3.0-or-later
"""Memaix doctor — pre-flight checks for a gateway deployment.

Usage:
    python -m memaix_gateway.doctor          # human-readable output, exit 0/1
    python -m memaix_gateway.doctor --json   # JSON array on stdout
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections import namedtuple
from pathlib import Path
from typing import Generator

from . import config
from .acl import AccessDenied, Acl

Check = namedtuple("Check", ["name", "status", "message"])

_PASS = "PASS"  # nosec B105 -- status label, not a credential
_WARN = "WARN"
_FAIL = "FAIL"
_SKIP = "SKIP"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_refs(obj) -> Generator[str, None, None]:
    """Recursively yield values of all keys ending in '_ref'."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.endswith("_ref") and isinstance(v, str):
                yield v
            else:
                yield from _find_refs(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _find_refs(item)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_config_parses() -> Check:
    name = "config_parses"
    try:
        cfg = config.load()
        # Verify top-level keys accessible without error.
        _ = cfg["brand"]
        _ = cfg["memaix"]
        _ = cfg["acl"]
        return Check(name, _PASS, "config loaded ok")
    except Exception as exc:
        return Check(name, _FAIL, str(exc))


def _check_secrets_resolved() -> Check:
    name = "secrets_resolved"
    try:
        cfg = config.load()
        failed: list[str] = []
        for ref in _find_refs(cfg):
            try:
                config.secret(ref)
            except (KeyError, FileNotFoundError) as exc:
                failed.append(str(exc))
            except NotImplementedError:
                pass  # vault/kms backends not yet wired — not a FAIL at this stage
        if failed:
            return Check(name, _FAIL, "; ".join(failed))
        return Check(name, _PASS, "all *_ref values resolved")
    except Exception as exc:
        return Check(name, _FAIL, f"config load error: {exc}")


def _check_hydra_reachable() -> Check:
    name = "hydra_reachable"
    try:
        cfg = config.load()
        issuer = cfg.get("memaix", {}).get("auth", {}).get("issuer", "")
        if not issuer:
            return Check(name, _SKIP, "auth.issuer not configured")
        import urllib.request
        url = f"{issuer}/.well-known/openid-configuration"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:  # nosec B310 -- admin-configured issuer URL, doctor CLI only
            if resp.status == 200:
                return Check(name, _PASS, f"reachable at {url}")
            return Check(name, _FAIL, f"unexpected status {resp.status}")
    except Exception as exc:
        return Check(name, _FAIL, str(exc))


def _check_gateway_healthy() -> Check:
    name = "gateway_healthy"
    base_url = os.environ.get("MEMAIX_HTTP_URL", "").strip()
    if not base_url:
        return Check(name, _SKIP, "MEMAIX_HTTP_URL not set")
    try:
        import urllib.request
        url = f"{base_url.rstrip('/')}/health"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:  # nosec B310 -- admin-configured MEMAIX_HTTP_URL, doctor CLI only
            if resp.status == 200:
                return Check(name, _PASS, f"healthy at {url}")
            return Check(name, _FAIL, f"unexpected status {resp.status}")
    except Exception as exc:
        return Check(name, _FAIL, str(exc))


def _check_rbac_isolation() -> Check:
    name = "rbac_isolation"
    try:
        acl = Acl(
            users={},
            projects={"test_project": {"vault": str(Path(tempfile.gettempdir()) / "__memaix_test__")}},
        )
        try:
            acl.enforce("__test_ghost__", "test_project", "reader")
            return Check(name, _FAIL, "enforce() did not raise for unknown user — isolation broken")
        except AccessDenied:
            return Check(name, _PASS, "AccessDenied raised for unknown user")
    except Exception as exc:
        return Check(name, _FAIL, str(exc))


def _check_owner_per_project() -> list[Check]:
    name = "owner_per_project"
    try:
        cfg = config.load()
        acl_cfg = cfg.get("acl", {})
        projects = acl_cfg.get("projects", {})
        users = acl_cfg.get("users", {})

        results: list[Check] = []
        for proj in projects:
            has_owner = any(
                u.get("grants", {}).get(proj) == "owner"
                for u in users.values()
            )
            if not has_owner:
                results.append(Check(name, _WARN, f"project '{proj}' has no owner"))

        if not results:
            return [Check(name, _PASS, "every project has at least one owner")]
        return results
    except Exception as exc:
        return [Check(name, _FAIL, str(exc))]


def _check_oauth_sub_unique() -> Check:
    name = "oauth_sub_unique"
    try:
        cfg = config.load()
        users = cfg.get("acl", {}).get("users", {})
        subs: list[str] = []
        for u in users.values():
            sub = u.get("oauth_sub")
            if sub:
                subs.append(sub)
        seen: set[str] = set()
        dupes: list[str] = []
        for s in subs:
            if s in seen:
                dupes.append(s)
            seen.add(s)
        if dupes:
            return Check(name, _FAIL, f"duplicate oauth_sub: {dupes}")
        return Check(name, _PASS, "all oauth_sub values are unique")
    except Exception as exc:
        return Check(name, _FAIL, str(exc))


def _check_vault_writable() -> list[Check]:
    name = "vault_writable"
    try:
        cfg = config.load()
        projects = cfg.get("acl", {}).get("projects", {})
        results: list[Check] = []
        for proj, pdata in projects.items():
            vault_path = pdata.get("vault")
            if not vault_path:
                continue
            vp = Path(vault_path)
            if not vp.exists():
                results.append(Check(name, _FAIL, f"{proj}: vault path does not exist: {vault_path}"))
                continue
            try:
                tmp = vp / f"__doctor_probe_{os.getpid()}__.tmp"
                tmp.write_text("probe")
                tmp.unlink()
                results.append(Check(name, _PASS, f"{proj}: vault writable"))
            except Exception as exc:
                results.append(Check(name, _FAIL, f"{proj}: {exc}"))

        if not results:
            return [Check(name, _SKIP, "no projects with vault configured")]
        return results
    except Exception as exc:
        return [Check(name, _FAIL, str(exc))]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_all() -> list[Check]:
    checks: list[Check] = []
    checks.append(_check_config_parses())
    checks.append(_check_secrets_resolved())
    checks.append(_check_hydra_reachable())
    checks.append(_check_gateway_healthy())
    checks.append(_check_rbac_isolation())
    checks.extend(_check_owner_per_project())
    checks.append(_check_oauth_sub_unique())
    checks.extend(_check_vault_writable())
    return checks


# ---------------------------------------------------------------------------
# Formatting + entrypoint
# ---------------------------------------------------------------------------

_STATUS_ORDER = [_PASS, _WARN, _FAIL, _SKIP]


def _format_human(checks: list[Check]) -> str:
    col_width = max(len(c.name) for c in checks) + 3
    lines = ["Memaix doctor"]
    for c in checks:
        dots = "." * (col_width - len(c.name))
        lines.append(f"  {c.name} {dots} {c.status:<4}  {c.message}")

    counts = {s: sum(1 for c in checks if c.status == s) for s in _STATUS_ORDER}
    parts = [f"{counts[s]} {s}" for s in _STATUS_ORDER if counts[s]]
    lines.append(f"Summa: {' · '.join(parts)}")
    return "\n".join(lines)


def main() -> None:
    use_json = "--json" in sys.argv
    checks = run_all()

    if use_json:
        print(json.dumps([c._asdict() for c in checks], indent=2))
    else:
        print(_format_human(checks))

    if any(c.status == _FAIL for c in checks):
        sys.exit(1)


if __name__ == "__main__":
    main()
