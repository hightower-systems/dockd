"""The shared stylesheet has to be linked AND actually served (v2).

Dockd's CSS used to live in a <style> block inside each template, so it
could not be wrong: if the template rendered, the styles were there. Moving
it to app/static/dockd.css introduces a failure mode that renders a
perfectly valid, completely unstyled page -- the link resolves, the file
404s, and nothing in the existing suite notices.

That risk is not hypothetical here. `create_app` builds Flask with an
explicit `static_folder=resource_path('static')` rather than the default,
because under a PyInstaller bundle the app runs out of sys._MEIPASS and an
app-root-relative 'static' would miss. These tests pin both halves: the
templates ask for the file, and the file comes back.
"""


class TestStylesheetIsServed:

    def test_stylesheet_route_returns_css(self, client):
        """/static/dockd.css must 200 with CSS, not 404."""
        resp = client.get('/static/dockd.css')
        assert resp.status_code == 200, (
            'dockd.css did not serve; static_folder wiring is broken'
        )
        assert 'css' in resp.headers.get('Content-Type', '').lower()

    def test_stylesheet_has_the_design_tokens(self, client):
        """A 200 is not enough -- an empty or truncated file also 200s."""
        body = client.get('/static/dockd.css').data.decode('utf-8')
        # The palette custom properties everything else derives from.
        assert '--paper:' in body
        assert '--copper:' in body
        assert '--red:' in body
        # Curvature + lift tokens: v2 started razor-square, then took a
        # 6px radius to sit alongside Sentry (--radius: 6px in App.css).
        assert '--radius:' in body
        assert '--shadow:' in body

    def test_index_links_the_stylesheet(self, client):
        body = client.get('/').data.decode('utf-8')
        assert '/static/dockd.css' in body

    def test_settings_links_the_stylesheet(self, admin_client):
        body = admin_client.get('/settings').data.decode('utf-8')
        assert '/static/dockd.css' in body


class TestNoOrphanedInlineStyleBlock:
    """Both templates should be fully migrated, not half-migrated.

    A leftover <style> block would silently win over the stylesheet for any
    rule it repeats, which is the confusing half-state this guards against.
    """

    def test_index_has_no_style_block(self, client):
        body = client.get('/').data.decode('utf-8')
        assert '<style>' not in body

    def test_settings_has_no_style_block(self, admin_client):
        body = admin_client.get('/settings').data.decode('utf-8')
        assert '<style>' not in body


class TestFontMigration:
    """Type is shared with Sentry-WMS admin (Instrument Sans + JetBrains
    Mono, see admin/src/App.css). Matching it is most of what makes the two
    apps read as one product rather than two.

    The negative assertions matter as much as the positive ones: this is a
    pack station on a warehouse LAN, and every extra family is another
    blocking request against fonts.googleapis.com before text renders.
    """

    SUPERSEDED = ('DM+Sans', 'IBM+Plex')

    def test_index_requests_sentrys_families(self, client):
        body = client.get('/').data.decode('utf-8')
        assert 'Instrument+Sans' in body
        assert 'JetBrains+Mono' in body

    def test_settings_requests_sentrys_families(self, admin_client):
        body = admin_client.get('/settings').data.decode('utf-8')
        assert 'Instrument+Sans' in body
        assert 'JetBrains+Mono' in body

    def test_index_drops_superseded_families(self, client):
        body = client.get('/').data.decode('utf-8')
        for fam in self.SUPERSEDED:
            assert fam not in body, f'{fam} still requested'

    def test_settings_drops_superseded_families(self, admin_client):
        body = admin_client.get('/settings').data.decode('utf-8')
        for fam in self.SUPERSEDED:
            assert fam not in body, f'{fam} still requested'

    def test_stylesheet_declares_the_same_stack(self, client):
        css = client.get('/static/dockd.css').data.decode('utf-8')
        assert "'Instrument Sans'" in css
        assert "'JetBrains Mono'" in css


class TestSharedPaletteWithSentry:
    """Structure tokens are lifted from Sentry-WMS admin/src/App.css so the
    two apps sit on the same paper. If someone re-themes dockd in isolation,
    these fail and prompt the question rather than letting the two silently
    drift apart again."""

    def _css(self, client):
        return client.get('/static/dockd.css').data.decode('utf-8')

    def test_ground_is_cream_not_stark_white(self, client):
        """Deliberate divergence from Sentry's #FAFAF7 ground.

        Panels on a near-white page had nothing to separate them and read
        as flat. dockd sits on cream (#EDE7DC) with panels a shade lighter
        (#FBF9F4), so they lift. The AvidMax email cream (#f1ece3) is the
        inset/header surface."""
        css = self._css(client)
        assert '#EDE7DC' in css
        assert '#FBF9F4' in css
        assert '#f1ece3' in css
        assert '--card:      #FFFFFF' not in css, 'panels must not be stark white'

    def test_radius_matches_sentry(self, client):
        assert '--radius:    6px' in self._css(client)

    def test_signal_colours_match_sentry(self, client):
        """Success/warning/danger/info are lifted whole so a status chip
        means the same thing in both apps."""
        css = self._css(client)
        for token in ('#2d7a3a', '#e8f5ea', '#8a6d1b', '#fef9e7', '#fce8e5', '#1e5f8a'):
            assert token in css, f'Sentry signal {token} missing'

    def test_ink_matches_sentry(self, client):
        css = self._css(client)
        for token in ('#1A1714', '#6b5f50', '#a89a88'):
            assert token in css, f'Sentry ink {token} missing'

    def test_accent_is_avidmax_green(self, client):
        """#014F38 is declared as --green in the Klaviyo campaign
        templates. It is the brand value, not a shade someone picked."""
        assert '#014F38' in self._css(client)

    def test_red_is_reserved_for_stop(self, client):
        """dockd uses red for an armed gate 3, voids and cancellations, so
        it cannot also be the structural accent the way it is in Sentry."""
        css = self._css(client)
        assert '#8e2715' in css
        assert '--green:      #014F38' in css


class TestJsDependentStylesSurvived:
    """Rules the inline JS relies on but never sets itself.

    Extracting the stylesheet out of the template introduced a failure mode
    that renders without erroring. Two real examples, both shipped and both
    caught by eye rather than by tests:

      * showBin()/showLabels() set only `display:flex`. The extraction
        dropped `flex-direction: column`, so the heading, subtitle and card
        laid out in a ROW across the top of an otherwise empty page.
      * renderBackendHealthDot() sets only `.style.background`. The dot's
        width and border-radius were inline and got stripped, leaving a
        zero-width element and a status readable only as text.

    Both are the same shape: JS supplies one property and assumes CSS
    supplies the rest.
    """

    def _css(self, client):
        return client.get('/static/dockd.css').data.decode('utf-8')

    def test_js_toggled_panels_declare_their_direction(self, client):
        """display:flex defaults to row; these panels must say column."""
        css = self._css(client)
        block = css[css.index('#bin-panel, #labels-panel {'):]
        block = block[:block.index('}')]
        assert 'flex-direction: column' in block

    def test_health_dot_has_shape_not_just_colour(self, client):
        css = self._css(client)
        block = css[css.index('#backend-health-dot'):]
        block = block[:block.index('}')]
        assert 'width:' in block
        assert 'border-radius: 50%' in block

    def test_modal_overlay_centres_when_js_flips_it_to_flex(self, client):
        """Every modal is opened with style.display='flex'; the alignment
        that centres it lives only in CSS."""
        css = self._css(client)
        block = css[css.index('.modal-overlay {'):]
        block = block[:block.index('}')]
        assert 'align-items: center' in block
        assert 'justify-content: center' in block
