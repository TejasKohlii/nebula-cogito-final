"""Task-independent experiment classification and measured prediction verdicts."""


def classify(parent_rtl, candidate_rtl, parent_options, options, allow_implementation_bundle=False):
    rtl_changed = parent_rtl != candidate_rtl
    knobs = sorted(k for k in set(parent_options) | set(options)
                   if parent_options.get(k) != options.get(k))
    if rtl_changed and knobs:
        raise ValueError('Mixed experiment rejected: RTL and implementation settings changed. '
                         'Submit an RTL-only experiment with parent settings, or a settings-only experiment with parent RTL.')
    if len(knobs) > 1 and not allow_implementation_bundle:
        raise ValueError('Change one implementation setting per experiment: '+', '.join(knobs))
    if not rtl_changed and not knobs:
        raise ValueError('No experimental change: only metadata changed or candidate repeats its parent')
    return dict(category='rtl' if rtl_changed else 'implementation', changed_settings=knobs)


def verdict(prediction, before, after, same_endpoints=True):
    result = dict(prediction=prediction, before=before, after=after,
                  measured_delta=None, verdict='inconclusive')
    if before is None or after is None:
        result['reason'] = 'Comparable measurements unavailable'
    elif not same_endpoints:
        result['reason'] = 'Worst path changed RTL endpoints; cannot judge a same-path prediction'
    else:
        delta = after-before
        supported = delta < -1e-6 if prediction['direction']=='decrease' else delta > 1e-6
        result.update(measured_delta=round(delta, 6),
                      verdict='supported' if supported else 'not_supported',
                      reason='Measured prediction direction; this does not prove causal attribution')
    return result
