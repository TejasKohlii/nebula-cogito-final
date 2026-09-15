"""Timing-first PPA fitness and restart-safe continuation for a single archive."""
import json
import math

DEFAULT = dict(mode='timing_then_ppa', area_reference_um2=100000., power_reference_w=1.,
               patience_generations=6, min_relative_improvement=0.001, preclosure_ppa_budget=0.,
               closing_policy='stop_at_closure', closing_bonus_improvement=0.005)


def enabled(task):
    return task.get('optimization', {}).get('mode') == 'timing_then_ppa'


def config(task):
    supplied = task.get('optimization', {})
    if not supplied:
        return None
    if set(supplied) - set(DEFAULT) or supplied.get('mode') != DEFAULT['mode']:
        raise ValueError('Unsupported optimization configuration')
    result = {**DEFAULT, **supplied}
    for key in ('area_reference_um2', 'power_reference_w', 'min_relative_improvement'):
        if not positive(result[key]):
            raise ValueError(f'{key} must be finite and positive')
    if not finite(result['preclosure_ppa_budget']) or not 0 <= result['preclosure_ppa_budget'] <= .1:
        raise ValueError('preclosure_ppa_budget must be finite and in 0..0.1')
    if result['min_relative_improvement'] >= 1:
        raise ValueError('min_relative_improvement must be below 1')
    if type(result['patience_generations']) is not int or result['patience_generations'] < 1:
        raise ValueError('patience_generations must be a positive integer')
    if result['closing_policy'] not in ('patience', 'two_plus_one', 'stop_at_closure'):
        raise ValueError('Unsupported closing_policy')
    if not positive(result['closing_bonus_improvement']) or result['closing_bonus_improvement'] >= 1:
        raise ValueError('closing_bonus_improvement must be finite and in (0,1)')
    if result['closing_policy'] in ('two_plus_one', 'stop_at_closure'):
        # Avoid exposing inactive legacy patience knobs in the model prompt.
        result.pop('patience_generations')
        result.pop('min_relative_improvement')
    if result['closing_policy'] == 'stop_at_closure':
        result.pop('closing_bonus_improvement')
    return result


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def positive(value):
    return finite(value) and value > 0


def closed(public):
    return (public.get('formal_equivalence') is True
            and public.get('timing_closed') is True
            and finite(public.get('wns_ns')) and public['wns_ns'] >= 0
            and finite(public.get('tns_ns')) and public['tns_ns'] == 0
            and public.get('violating_endpoints') == 0
            and public.get('constraint_warnings') == 0)


def assess(public, task, power):
    """Invalid=0, open<1, closed without power=1, closed with PPA in (2,3).

    Before closure, optionally add a bounded PPA penalty to timing burden.
    Within the closed PPA band, minimize the geometric mean of normalized area and
    power. Timing margin earns no bonus: any setup violation loses this band.
    References are fixed scales, not measured baseline claims.
    """
    cfg = config(task)
    watts = (power.get('watts') or {}).get('total')
    valid = power.get('valid') is True and positive(watts)
    public.update(power_valid=valid, power_w=watts if valid else None, ppa_quality=None)
    if (public.get('formal_equivalence') is not True
            or public.get('constraint_warnings') != 0
            or not finite(public.get('wns_ns')) or not finite(public.get('tns_ns'))
            or not positive(public.get('cell_area_um2'))):
        return 0.
    quality = None
    if valid:
        quality = ppa_quality(public['cell_area_um2'], watts, cfg)
    if not closed(public):
        cost = timing_cost(public)
        budget = cfg['preclosure_ppa_budget']
        if budget:
            # Bounded secondary objective: no PPA can offset a timing-cost
            # regression larger than budget. Missing power gets full penalty.
            penalty = budget * (1. - quality if quality is not None else 1.)
            public.update(ppa_quality=quality, timing_cost=cost,
                          preclosure_ppa_penalty=penalty)
        else:
            penalty = 0.
        return .99 / (1 + cost + penalty)
    if not valid:
        return 1.
    public['ppa_quality'] = quality
    if cfg['preclosure_ppa_budget']:
        public.update(timing_cost=0., preclosure_ppa_penalty=0.)
    return 2. + quality


def timing_cost(public):
    """Existing timing burden: negative WNS in ns + negative TNS / 100."""
    return max(0., -public['wns_ns']) + max(0., -public['tns_ns']) / 100.


def ppa_quality(area, watts, cfg):
    log_cost = .5 * (math.log(area) - math.log(cfg['area_reference_um2'])
                     + math.log(watts) - math.log(cfg['power_reference_w']))
    # Stable logistic; fixed scales are not measured baseline claims.
    return (1 / (1 + math.exp(min(log_cost, 700))) if log_cost >= 0
            else 1 - 1 / (1 + math.exp(min(-log_cost, 700))))


def tradeoff_candidates(task, rows, limit=6):
    """Surface nondominated measured alternatives near the best timing burden.

    Read-only evidence, not a replacement for official Shinka selection. All
    admitted candidates remain in its search pool; this is a compact shortlist.
    """
    cfg = config(task)
    if not cfg or not cfg['preclosure_ppa_budget']:
        return []
    valid = []
    for row in rows:
        p = json.loads(row['public_metrics'] or '{}')
        if (row['correct'] and p.get('formal_equivalence') is True
                and p.get('constraint_warnings') == 0
                and finite(p.get('wns_ns')) and finite(p.get('tns_ns'))
                and positive(p.get('cell_area_um2'))):
            valid.append((row, p))
    if not valid:
        return []
    best = min(timing_cost(p) for _, p in valid)
    near = []
    seen = set()
    for r, p in sorted(valid, key=lambda rp: (rp[0]['generation'], rp[0]['id'])):
        if (timing_cost(p) > best + cfg['preclosure_ppa_budget']
                or p.get('power_valid') is not True or not positive(p.get('power_w'))):
            continue
        point = (max(0., -p['wns_ns']), max(0., -p['tns_ns']), p['cell_area_um2'], p['power_w'])
        if point not in seen:
            near.append((r, p, point))
            seen.add(point)
    frontier = [item for item in near if not any(
        all(a <= b for a, b in zip(other[2], item[2])) and any(a < b for a, b in zip(other[2], item[2]))
        for other in near)]
    # Preserve timing/area/power extremes in the bounded evidence packet.
    selected = []
    for key in (lambda x: timing_cost(x[1]), lambda x: x[2][0], lambda x: x[2][1],
                lambda x: x[2][2], lambda x: x[2][3]):
        if frontier:
            item = min(frontier, key=key)
            if item not in selected:
                selected.append(item)
    for item in sorted(frontier, key=lambda x: timing_cost(x[1])):
        if item not in selected:
            selected.append(item)
    return [dict(id=r['id'], generation=r['generation'], metrics=p,
                 timing_cost=timing_cost(p)) for r, p, _ in selected[:limit]]


def continuation(task, winner, rows):
    """Reconstruct the configured stop policy from evaluated archive history.

    Default: stop at the first admissible timing closure. Explicit legacy
    policies retain their bounded-attempt or PPA-patience behavior.
    """
    cfg = config(task)
    if cfg and cfg['closing_policy'] == 'stop_at_closure':
        return stop_at_closure(rows)
    if cfg and cfg['closing_policy'] == 'two_plus_one':
        return bounded_closing(cfg, rows)
    result = dict(phase='timing_closure', stop=False, stop_reason=None,
                  first_closure_generation=None, generations_without_improvement=0)
    if cfg is None:
        return result
    last = max((r['generation'] for r in rows), default=-1)
    anchor = None
    improved_at = None
    for row in sorted(rows, key=lambda r: (r['generation'], r.get('timestamp', 0))):
        public = json.loads(row['public_metrics'] or '{}')
        if not row['correct'] or not closed(public):
            continue
        if result['first_closure_generation'] is None:
            result['first_closure_generation'] = row['generation']
            result['phase'] = 'ppa_refinement'
        if public.get('power_valid') is not True or not positive(public.get('power_w')) or not positive(public.get('cell_area_um2')):
            continue
        cost = .5 * (math.log(public['cell_area_um2']) + math.log(public['power_w']))
        if anchor is None or cost <= anchor + math.log1p(-cfg['min_relative_improvement']):
            anchor, improved_at = cost, row['generation']
    if improved_at is not None:
        result['generations_without_improvement'] = last - improved_at
        wp = json.loads(winner['public_metrics'] or '{}') if winner else {}
        if (winner and winner['correct'] and closed(wp) and wp.get('power_valid') is True
                and last - improved_at >= cfg['patience_generations']):
            result.update(stop=True, stop_reason='ppa_plateau')
    return result


def stop_at_closure(rows):
    """Stop after the first admissible closure, even when power is unavailable."""
    state = dict(phase='timing_closure', stop=False, stop_reason=None,
                 closing_policy='stop_at_closure', first_closure_generation=None,
                 first_closure_id=None, postclosure_attempts_allowed=0,
                 postclosure_attempts_remaining=0)
    for row in sorted(rows, key=lambda r: (r['generation'], r.get('timestamp', 0), str(r.get('id', '')))):
        public = json.loads(row['public_metrics'] or '{}')
        if row['correct'] and closed(public):
            state.update(phase='timing_closed', stop=True, stop_reason='timing_closure_reached',
                         first_closure_generation=row['generation'], first_closure_id=row.get('id'))
            break
    return state


def ppa_log_cost(public):
    if (public.get('power_valid') is True and positive(public.get('power_w'))
            and positive(public.get('cell_area_um2'))):
        return .5 * (math.log(public['cell_area_um2']) + math.log(public['power_w']))
    return None


def bounded_closing(cfg, rows):
    """Two generations after first closure; one bonus for >=0.5% PPA gain.

    Reconstruct from immutable archive history, counting distinct evaluated
    generations (failed evaluations count; island copies do not). Only gains
    during the first two attempts can earn the bonus. Never restart this budget.
    """
    state = dict(phase='timing_closure', stop=False, stop_reason=None,
                 closing_policy='two_plus_one', first_closure_generation=None,
                 first_closure_id=None, first_closure_metrics=None,
                 postclosure_attempts_used=0, postclosure_attempts_allowed=2,
                 postclosure_attempts_remaining=2, closing_bonus_earned=False,
                 closing_bonus_threshold=cfg['closing_bonus_improvement'],
                 closing_qualifying_gain=None, postclosure_relative_gain=None)
    ordered = sorted(rows, key=lambda r: (r['generation'], r.get('timestamp', 0), str(r.get('id', ''))))
    measured = [(r, json.loads(r['public_metrics'] or '{}')) for r in ordered]
    closed_rows = [(r, p) for r, p in measured if r['correct'] and closed(p)
                   and positive(p.get('cell_area_um2'))]
    if not closed_rows:
        return state
    first, baseline = closed_rows[0]
    state.update(phase='ppa_refinement', first_closure_generation=first['generation'],
                 first_closure_id=first.get('id'), first_closure_metrics=baseline)
    attempts = sorted({r['generation'] for r in ordered if r['generation'] > first['generation']})
    first_two = set(attempts[:2])
    anchor = ppa_log_cost(baseline)
    if anchor is not None:
        qualifying, best = anchor, anchor
        for r, p in closed_rows:
            if r['generation'] <= first['generation']:
                continue
            cost = ppa_log_cost(p)
            if cost is not None:
                best = min(best, cost)
                if r['generation'] in first_two:
                    qualifying = min(qualifying, cost)
        gain = -math.expm1(qualifying - anchor)
        state.update(closing_qualifying_gain=gain,
                     postclosure_relative_gain=-math.expm1(best - anchor),
                     closing_bonus_earned=gain + 1e-12 >= cfg['closing_bonus_improvement'])
    allowance = 3 if state['closing_bonus_earned'] else 2
    used = len(attempts)
    state.update(postclosure_attempts_used=used, postclosure_attempts_allowed=allowance,
                 postclosure_attempts_remaining=max(0, allowance-used))
    if used >= allowance:
        state.update(stop=True, stop_reason='postclosure_three_attempt_cap' if allowance == 3
                     else 'postclosure_two_attempts_complete')
    return state
