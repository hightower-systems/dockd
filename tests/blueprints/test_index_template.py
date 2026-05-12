"""Regression tests for the operator index template (v0.6.0).

The pre-v0.6.0 template gated the SETTINGS and EXIT sidebar buttons
on a server-side Jinja conditional
(`{% if current_user.role == 'admin' %}`). That conditional runs at
page-render time, BEFORE the JS-driven login attaches a session. The
HTML the browser had was missing both buttons, and only a full page
reload would surface them.

The fix renders both buttons unconditionally with `display:none` and
the `admin-only` class. A JS helper (`applyAdminVisibility`) toggles
them on after the login response lands.
"""


class TestAdminButtonMarkup:

    def test_unauthenticated_render_includes_admin_only_buttons(self, client):
        """Even with no session, the SETTINGS + EXIT buttons must be
        in the HTML so the JS-driven login can reveal them without a
        page reload."""
        resp = client.get('/')
        assert resp.status_code == 200
        body = resp.data.decode('utf-8')
        # Both admin-gated buttons appear in the markup.
        assert 'SETTINGS' in body
        assert 'EXIT' in body
        # Tagged with the admin-only class for the JS toggle.
        assert 'admin-only' in body
        # Default hidden so non-admin sessions never see them.
        assert 'display:none' in body or "display: none" in body

    def test_apply_admin_visibility_helper_present(self, client):
        """The JS helper that reveals .admin-only elements after login
        must be in the rendered page."""
        resp = client.get('/')
        body = resp.data.decode('utf-8')
        assert 'applyAdminVisibility' in body
        # Called from the auto-login bootstrap (server has session).
        assert 'applyAdminVisibility(currentUser)' in body

    def test_authenticated_render_still_includes_buttons(self, admin_client):
        """When the session IS attached at render time, the buttons
        still ship in the markup; the JS toggle runs after page load
        and reveals them based on currentUser.role."""
        resp = admin_client.get('/')
        body = resp.data.decode('utf-8')
        assert 'SETTINGS' in body
        assert 'EXIT' in body


class TestUpcScanMatching:
    """v0.6.1: verifyItem and verifyLinkItem must check i.upc.

    Pre-v0.6.1 both functions only compared the scan against
    `i.sku` + `i.item.refName` + the legacy `i.valid_scans` array,
    so every UPC scan failed with "INVALID ITEM" on the floor even
    though the upc field was populated on each item from Sentry.
    """

    def test_verify_item_matches_upc(self, client):
        resp = client.get('/')
        body = resp.data.decode('utf-8')
        # The verifyItem function body should compare i.upc.
        # Tolerant on whitespace + quote style.
        assert 'i.upc' in body
        # And in the ship-flow verify specifically (not just somewhere
        # else in the template).
        verify_block = body[body.find('function verifyItem('):]
        verify_block = verify_block[:verify_block.find('function ', 10)]
        assert 'i.upc' in verify_block

    def test_verify_link_item_matches_upc(self, client):
        resp = client.get('/')
        body = resp.data.decode('utf-8')
        link_block = body[body.find('function verifyLinkItem('):]
        link_block = link_block[:link_block.find('function ', 10)]
        assert 'i.upc' in link_block

    def test_render_items_shows_upc(self, client):
        """Operators want UPC visible on each line alongside the SKU."""
        resp = client.get('/')
        body = resp.data.decode('utf-8')
        # The UPC line variable name introduced in v0.6.1.
        assert 'upcLine' in body or 'i.upc' in body
        # Label text so operators can read the row at a glance.
        assert 'UPC' in body
