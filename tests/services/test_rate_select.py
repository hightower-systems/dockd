"""select_rate(): cheapest service that is no slower than the one ordered.

The numbers in LIVE are a real /shipment/rateshopping response captured
2026-07-22, Aurora CO -> Bozeman MT, 4.82 lb, 12x9x6. They are used rather
than round invented figures because the interesting case is exactly the one
real data produced: the "cheap" service (FedEx Ground Economy) is neither
the cheapest nor fast, and a price-only ranking would have looked fine here
purely by accident.
"""

from app.services.rate_select import select_rate


def svc(code, total, days, name=None):
    return {
        'service_code': code,
        'name': name or code,
        'total': total,
        'carrier_rate': total,
        'transit_days': days,
        'one_rate': False,
        'quote_id': f'Q-{code}',
    }


LIVE = [
    svc('USPSGNDADV', 8.71, 2, 'USPS Ground Advantage'),
    svc('FSP', 10.50, 5, 'FedEx Ground Economy'),
    svc('U02', 11.54, 2, 'USPS Priority'),
    svc('F92', 13.14, 2, 'FedEx Ground'),
    svc('03', 14.36, 2, 'UPS Ground'),
    svc('F03', 20.06, 2, 'FedEx 2Day'),
]


class TestTheRule:

    def test_picks_cheapest_within_ordered_transit(self):
        out = select_rate(LIVE, ordered_code='03')       # UPS Ground, 2 d
        assert out['chosen']['service_code'] == 'USPSGNDADV'
        assert out['savings'] == 5.65                    # 14.36 - 8.71

    def test_never_picks_something_slower_to_save_money(self):
        """The whole point of the constraint. Ordered a 2-day service, so a
        5-day one is not a candidate no matter what it costs."""
        cheap_but_slow = [svc('03', 14.36, 2), svc('FSP', 4.00, 5)]
        out = select_rate(cheap_but_slow, ordered_code='03')
        assert out['chosen']['service_code'] == '03'
        assert out['savings'] == 0.0
        codes = [s['service_code'] for s, _ in out['rejected']]
        assert 'FSP' in codes

    def test_faster_and_cheaper_is_allowed(self):
        """No slower than ordered means faster is fine."""
        out = select_rate([svc('03', 14.36, 3), svc('USPSGNDADV', 8.71, 1)],
                          ordered_code='03')
        assert out['chosen']['service_code'] == 'USPSGNDADV'

    def test_keeps_ordered_when_it_is_already_cheapest(self):
        out = select_rate(LIVE, ordered_code='USPSGNDADV')
        assert out['chosen']['service_code'] == 'USPSGNDADV'
        assert out['savings'] == 0.0
        assert 'already cheapest' in out['reason']

    def test_ground_economy_loses_on_the_real_lane(self):
        """Regression guard on the finding that reshaped this design: FSP
        is both dearer and slower than USPS GA on a real lane."""
        out = select_rate(LIVE, ordered_code='USPSGNDADV')
        assert out['chosen']['service_code'] != 'FSP'
        rejected = {s['service_code']: why for s, why in out['rejected']}
        assert '5d vs 2d ordered' in rejected['FSP']


class TestTransitBudget:

    def test_uses_the_ordered_services_own_quoted_transit(self):
        """The budget comes from the same response, so no second lookup and
        no stale hardcoded transit table."""
        out = select_rate(LIVE, ordered_code='FSP')      # ordered 5 d
        # With a 5-day budget everything qualifies, so cheapest wins outright.
        assert out['chosen']['service_code'] == 'USPSGNDADV'
        assert out['rejected'] == []

    def test_falls_back_to_caller_supplied_transit(self):
        out = select_rate(LIVE, ordered_code='NOTQUOTED', ordered_transit_days=2)
        assert out['chosen']['service_code'] == 'USPSGNDADV'
        assert out['ordered'] is None

    def test_no_budget_at_all_accepts_any_transit(self):
        out = select_rate(LIVE)
        assert out['chosen']['service_code'] == 'USPSGNDADV'

    def test_unknown_transit_is_never_auto_selected(self):
        """Cannot prove it is not slower, so it must not win by being cheap."""
        mixed = [svc('03', 14.36, 2), svc('MYSTERY', 1.00, None)]
        out = select_rate(mixed, ordered_code='03')
        assert out['chosen']['service_code'] == '03'
        assert any(s['service_code'] == 'MYSTERY' for s, _ in out['rejected'])


class TestDegradesSafely:
    """chosen=None must always be survivable: the caller keeps the ordered
    carrier and the ship proceeds."""

    def test_empty_quote_list(self):
        out = select_rate([])
        assert out['chosen'] is None
        assert out['reason']

    def test_none_quote_list(self):
        assert select_rate(None)['chosen'] is None

    def test_services_without_a_price_are_ignored(self):
        out = select_rate([svc('03', 14.36, 2), {'service_code': 'X', 'total': None}],
                          ordered_code='03')
        assert out['chosen']['service_code'] == '03'

    def test_nothing_meets_transit_returns_no_choice(self):
        out = select_rate([svc('FSP', 4.00, 9)], ordered_code='03',
                          ordered_transit_days=1)
        assert out['chosen'] is None
        assert out['eligible'] == []

    def test_ordered_code_is_matched_case_insensitively(self):
        out = select_rate(LIVE, ordered_code='uspsgndadv')
        assert out['ordered']['service_code'] == 'USPSGNDADV'


class TestReasonIsStorable:
    """`reason` lands in the chosen_reason column, so it has to explain the
    decision on its own months later."""

    def test_switch_reason_names_both_services_and_the_delta(self):
        out = select_rate(LIVE, ordered_code='03')
        r = out['reason']
        assert 'USPSGNDADV' in r and '03' in r
        assert '5.65' in r
        assert 'transit' in r

    def test_reason_is_always_present(self):
        for args in ([], LIVE):
            assert select_rate(args)['reason']
