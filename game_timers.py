"""Host-controlled clocks. Pure transitions accept epoch milliseconds for deterministic tests."""
from copy import deepcopy


def validate_config(config):
    if not isinstance(config, dict) or config.get('mode', 'off') not in ('off', 'chess_clock', 'turn_timer'):
        raise ValueError('Invalid timer mode')
    mode = config.get('mode', 'off')
    result = {'mode': mode}
    fields = [('minutes_per_player', 1, 180), ('increment_seconds', 0, 300)] if mode == 'chess_clock' else [('seconds_per_turn', 5, 3600)] if mode == 'turn_timer' else []
    for key, low, high in fields:
        value = config.get(key, 0)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f'{key} must be a whole number between {low} and {high}')
        result[key] = value
    return result


def new_timer(config, players, active, now):
    config = validate_config(config)
    return dict(config=config, remaining_ms={str(pid): config.get('minutes_per_player', 0) * 60000 for pid in players},
                turn_remaining_ms=config.get('seconds_per_turn', 0) * 1000, active_player_id=active,
                updated_at_ms=now, paused_reasons=[], timed_out_player_id=None, timeout_handled=False)


def tick(timer, now):
    result = deepcopy(timer)
    elapsed = max(0, now - timer['updated_at_ms'])
    result['updated_at_ms'] = max(now, timer['updated_at_ms'])
    if timer['config']['mode'] == 'off' or timer['paused_reasons'] or timer['timed_out_player_id'] is not None:
        return result
    active = timer['active_player_id']
    if active is None:
        return result
    if timer['config']['mode'] == 'chess_clock':
        remaining = max(0, timer['remaining_ms'].get(str(active), 0) - elapsed)
        result['remaining_ms'][str(active)] = remaining
    else:
        remaining = max(0, timer['turn_remaining_ms'] - elapsed)
        result['turn_remaining_ms'] = remaining
    if remaining == 0:
        result['timed_out_player_id'] = active
    return result


def advance(timer, active, now):
    result = tick(timer, now)
    if result['timed_out_player_id'] is not None or result['paused_reasons']:
        return result
    if active != result['active_player_id']:
        if result['config']['mode'] == 'chess_clock':
            key = str(result['active_player_id'])
            result['remaining_ms'][key] += result['config'].get('increment_seconds', 0) * 1000
        result['turn_remaining_ms'] = result['config'].get('seconds_per_turn', 0) * 1000
        result['active_player_id'] = active
    return result


def validate_snapshot(raw, player_ids):
    if not isinstance(raw, dict):
        raise ValueError('timer_snapshot must be an object')
    config = validate_config(raw.get('config'))
    result = new_timer(config, player_ids, None, 0)
    for key in ('active_player_id', 'timed_out_player_id'):
        value = raw.get(key)
        if value is not None and (type(value) is not int or value not in player_ids):
            raise ValueError(key + ' must reference a participant')
        result[key] = value
    clocks = raw.get('remaining_ms', {})
    if not isinstance(clocks, dict) or any(key not in {str(pid) for pid in player_ids} for key in clocks):
        raise ValueError('Invalid remaining clocks')
    for key, value in clocks.items():
        if type(value) is not int or not 0 <= value <= 604800000:
            raise ValueError('Invalid remaining clock value')
    result['remaining_ms'] = clocks
    for key, limit in [('turn_remaining_ms', 3600000), ('updated_at_ms', 253402300799999)]:
        value = raw.get(key, 0)
        if type(value) is not int or not 0 <= value <= limit:
            raise ValueError('Invalid ' + key)
        result[key] = value
    reasons = raw.get('paused_reasons', [])
    if not isinstance(reasons, list) or any(reason not in ('modal', 'manual', 'end_game') for reason in reasons):
        raise ValueError('Invalid pause reasons')
    result['paused_reasons'] = sorted(set(reasons))
    handled = raw.get('timeout_handled', False)
    if type(handled) is not bool:
        raise ValueError('timeout_handled must be boolean')
    result['timeout_handled'] = handled
    return result
