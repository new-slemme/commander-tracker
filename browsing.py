"""HTML listing controls. No model writes or external metadata lookups."""
from datetime import datetime, timedelta
from math import isfinite
from urllib.parse import urlencode, urlsplit

from flask import request
from sqlalchemy import false


def listing_return_url(value):
    """Only local listing destinations may be carried through detail/edit pages."""
    if not value or any(c in value for c in ('\\', '\r', '\n')):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc or parsed.fragment or parsed.path not in ('/decks', '/players', '/games'):
        return None
    return parsed.path + ('?' + parsed.query if parsed.query else '')


class Browse:
    def __init__(self, kind, sorts, default_sort='name', default_direction='asc'):
        self.kind = kind
        self.args = request.args.copy()
        self.errors = {}
        self.fields = []
        self.values = {}
        self.sorts = sorts
        self.q = self.args.get('q', '').strip()
        self.values['q'] = self.q
        self.sort = self.choice('sort', sorts, default_sort)
        self.direction = self.choice('direction', [('asc', 'Ascending'), ('desc', 'Descending')], default_direction)
        self.per_page = int(self.choice('per_page', [(str(n), str(n)) for n in (10, 25, 50, 100)], '25'))
        try:
            self.page = max(1, int(self.args.get('page', '1')))
            if self.page > 1000000:
                raise ValueError
        except ValueError:
            self.page = 1
            self.errors['page'] = 'Page must be a whole number.'
        self.start = self.end = None
        self.period = 'lifetime'
        self.base_total = 0

    def choice(self, name, options, default='', label=None, multiple=False):
        options = [(str(k), v) for k, v in options]
        valid = {k for k, _ in options}
        raw = self.args.getlist(name) if multiple else self.args.get(name, default)
        if multiple:
            raw = list(dict.fromkeys(v for v in raw if v))
            if any(v not in valid for v in raw):
                self.errors[name] = 'Choose one of the available options.'
        elif raw not in valid:
            self.errors[name] = 'Choose one of the available options.'
            raw = default
        self.values[name] = raw
        if label:
            self.fields.append(dict(name=name, label=label, type='multi' if multiple else 'select', options=options))
        return raw

    def dates(self, with_period=True):
        if with_period:
            self.period = self.choice('period', [('lifetime', 'Lifetime'), ('30', 'Last 30 days'), ('90', 'Last 90 days'), ('custom', 'Custom dates')], 'lifetime', 'Statistics period')
        for name, label in [('date_from', 'From (UTC)'), ('date_to', 'Through (UTC)')]:
            raw = self.args.get(name, '').strip()
            self.values[name] = raw
            self.fields.append(dict(name=name, label=label, type='date'))
            try:
                parsed = datetime.strptime(raw, '%Y-%m-%d') if raw else None
                if name == 'date_from':
                    self.start = parsed
                else:
                    self.end = parsed + timedelta(days=1) if parsed else None
            except (ValueError, OverflowError):
                self.errors[name] = 'Enter a valid calendar date (YYYY-MM-DD).'
        if self.start and self.end and self.start >= self.end:
            self.errors['date_to'] = 'End date must be on or after the start date.'
        if with_period:
            if self.period != 'custom' and (self.args.get('date_from') or self.args.get('date_to')):
                self.errors['date_from'] = 'Choose Custom dates to use date boundaries, or clear the dates.'
            if self.period == 'custom' and not (self.start or self.end):
                self.errors['date_from'] = 'Choose at least one date for a custom period.'
            elif self.period in ('30', '90'):
                today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
                self.start, self.end = today - timedelta(days=int(self.period) - 1), today + timedelta(days=1)
            elif self.period == 'lifetime':
                self.start = self.end = None

    def dated(self, query, column):
        if self.start:
            query = query.filter(column >= self.start)
        if self.end:
            query = query.filter(column < self.end)
        return query

    def ranges(self, query, metrics):
        for name, label, column, integer in metrics:
            bounds = []
            for prefix, text in [('min_', 'Minimum'), ('max_', 'Maximum')]:
                key = prefix + name
                raw = self.args.get(key, '').strip()
                self.values[key] = raw
                self.fields.append(dict(name=key, label=f'{text} {label}', type='number', step='1' if integer else 'any'))
                value = None
                if raw:
                    try:
                        value = float(raw)
                        if not isfinite(value) or value < 0 or (integer and not value.is_integer()) or (name == 'winrate' and value > 100):
                            raise ValueError
                    except ValueError:
                        self.errors[key] = 'Enter a nonnegative whole number.' if integer else 'Enter a valid nonnegative number (0–100 for win rate).'
                        value = None
                bounds.append(value)
                if value is not None:
                    query = query.filter(column >= value if prefix == 'min_' else column <= value)
            if all(v is not None for v in bounds) and bounds[0] > bounds[1]:
                self.errors['max_' + name] = 'Maximum must be greater than or equal to minimum.'
        return query

    def ordered(self, query, columns, id_column):
        if self.errors:
            query = query.filter(false())
        column = columns[self.sort]
        order = column.desc() if self.direction == 'desc' else column.asc()
        return query.order_by(None).order_by(column.is_(None), order, id_column.asc())

    def url(self, **changes):
        args = self.args.copy()
        args.pop('page', None)
        for key, value in changes.items():
            args.pop(key, None)
            if value is not None:
                args.setlist(key, value if isinstance(value, list) else [str(value)])
        return request.path + ('?' + urlencode(list(args.items(multi=True))) if args else '')

    @property
    def clear_url(self):
        scope = self.args.get('scope')
        return request.path + ('?' + urlencode({'scope': scope}) if scope in ('all', 'pod') else '')

    @property
    def chips(self):
        chips = []
        fields = {f['name']: f for f in self.fields}
        fields['q'] = dict(label='Search')
        for key, field in fields.items():
            values = self.args.getlist(key)
            # Default selectors are useful state, but not active filters.
            for value in dict.fromkeys(v for v in values if v):
                if (key == 'period' and value == 'lifetime') or (key == 'status' and value == 'active') or key == 'color_mode':
                    continue
                options = dict(field.get('options', []))
                if options and value not in options:
                    continue
                label = options.get(value, value)
                remaining = [v for v in values if v != value]
                changes = {key: remaining or None}
                if key == 'period':
                    changes.update(date_from=None, date_to=None)
                if key == 'status':
                    changes['show_retired'] = None
                chips.append(dict(label=f"{field['label']}: {label}", url=self.url(**changes)))
        return chips
