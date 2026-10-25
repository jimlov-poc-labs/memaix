# SPDX-License-Identifier: AGPL-3.0-or-later
import pytest


@pytest.fixture(autouse=True)
def _clear_free_window_cache():
    from memaix_gateway.booking import routes

    routes._free_cache.clear()
    yield
    routes._free_cache.clear()
