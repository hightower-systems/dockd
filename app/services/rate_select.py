"""Pick a service from a rate-shop quote. Pure logic, no I/O, no Flask.

The rule, decided 2026-07-22: **cheapest service that is no slower than the
one the customer ordered.**

That constraint is doing real work. Sorting on price alone looks correct in
a spreadsheet and wrong at the pack bench. A live quote on 2026-07-22
(Aurora CO -> Bozeman MT, 4.82 lb, 12x9x6) returned:

    USPS Ground Advantage   USPSGNDADV    $8.71   2 d
    FedEx Ground Economy    FSP          $10.50   5 d
    UPS Ground              03           $14.36   2 d

Ground Economy is the service most often pitched as the cheap one, and on
that lane it is both more expensive *and* three days slower. A price-only
ranking would have been right there by luck; on a lane where FSP does win
on price it would silently have downgraded the customer's delivery date to
save a dollar.

A useful consequence of the rule: within "no slower than ordered" there is
no trade-off left to weigh, so a switch is pure upside and needs no
operator prompt. The priced prompts stay attached to the decisions that
DO involve a judgement call -- rural surcharge, high value, Amazon -- and
rate shopping just supplies them with real numbers.
"""

import logging

logger = logging.getLogger('dockd.rate_select')


# Transit that came back empty cannot be proven no-slower-than-ordered, so
# it is never auto-selected. It is still returned in `rejected` so the
# operator-facing list can show it.
UNKNOWN_TRANSIT = None

# FedEx Ground Economy (FSP) is DIM-priced, so it wins on small parcels and
# loses badly on large light ones. The 2026-07-20 backtest re-priced 543 real
# UPS shipments through /rateshopping and found the crossover at 20 billable
# pounds: GE was cheaper on 92-100% of parcels below it and LOST on 91% above
# it. Worst observed case was a 1lb 36x16x5 rod tube at $71.25 UPS versus
# $112.49 GE. Rod tubes and oversized stay on UPS.
DIM_CAPPED_SERVICES = {'FSP': 20.0}


def select_rate(services, ordered_code=None, ordered_transit_days=None,
                billable_lb=None):
    """Choose a service from `services` (as returned by rate_shop()).

    Returns a dict:
        chosen           the winning service, or None
        ordered          the service matching ordered_code, if quoted
        savings          ordered['total'] - chosen['total'], or None
        eligible         services that met the transit constraint
        rejected         [(service, why)] for everything that did not
        reason           short human string, stored on the ship row

    Never raises on odd input. A caller that gets chosen=None must keep the
    carrier the order asked for.
    """
    services = [s for s in (services or []) if s.get('total') is not None]
    if not services:
        return _empty('no quotes returned')

    ordered = _find_ordered(services, ordered_code)

    # Transit budget: prefer the ordered service's own quoted transit, since
    # it arrives in the same response and needs no separate lookup. Fall
    # back to a caller-supplied figure, and if neither exists accept any
    # transit rather than refusing to choose at all.
    budget = None
    if ordered and ordered.get('transit_days') is not None:
        budget = ordered['transit_days']
    elif ordered_transit_days is not None:
        budget = ordered_transit_days

    eligible, rejected = [], []
    for s in services:
        # Dim-weight ceiling before the transit test: above it the service is
        # not merely slower, it is more expensive than what it is replacing.
        cap = DIM_CAPPED_SERVICES.get(str(s.get('service_code', '')).upper())
        if cap is not None and billable_lb is not None and billable_lb > cap:
            rejected.append((s, f'{billable_lb:.1f} lb over the {cap:.0f} lb dim ceiling'))
            continue

        days = s.get('transit_days')
        if budget is None:
            eligible.append(s)
        elif days is UNKNOWN_TRANSIT:
            rejected.append((s, 'no transit estimate'))
        elif days > budget:
            rejected.append((s, f'{days}d vs {budget}d ordered'))
        else:
            eligible.append(s)

    if not eligible:
        return _empty('no service matched the ordered transit', ordered=ordered,
                      rejected=rejected)

    chosen = min(eligible, key=lambda s: s['total'])

    savings = None
    if ordered and ordered.get('total') is not None:
        savings = round(ordered['total'] - chosen['total'], 2)

    if ordered and chosen['service_code'] == ordered['service_code']:
        reason = 'ordered service was already cheapest within transit'
    elif savings is not None:
        reason = (
            f"{chosen['service_code']} at {chosen['total']:.2f} beat ordered "
            f"{ordered['service_code']} at {ordered['total']:.2f} "
            f"by {savings:.2f}, same or faster transit"
        )
    else:
        reason = (
            f"ordered service {ordered_code or 'unknown'} was not quoted; "
            f"took cheapest at {chosen['total']:.2f}"
        )

    logger.info("Rate select: %s", reason)

    return {
        'chosen': chosen,
        'ordered': ordered,
        'savings': savings,
        'eligible': eligible,
        'rejected': rejected,
        'reason': reason,
    }


def _find_ordered(services, ordered_code):
    if not ordered_code:
        return None
    want = str(ordered_code).strip().upper()
    for s in services:
        if str(s.get('service_code', '')).strip().upper() == want:
            return s
    return None


def _empty(reason, ordered=None, rejected=None):
    return {
        'chosen': None,
        'ordered': ordered,
        'savings': None,
        'eligible': [],
        'rejected': rejected or [],
        'reason': reason,
    }
