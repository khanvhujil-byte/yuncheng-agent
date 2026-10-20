"""Derive partial boundary-day obligations from the user's actual time window."""
from datetime import date, timedelta

MEALS = ('breakfast', 'lunch', 'dinner')


def minutes(value):
    hours, minute = map(int, value.split(':'))
    return hours * 60 + minute


def policy(request, day_date):
    last = (date.fromisoformat(request.start_date) + timedelta(days=request.travel_days - 1)).isoformat()
    arrival = request.arrival_time if day_date == request.start_date else None
    departure = request.departure_time if day_date == last else None
    start = minutes(arrival) if arrival else 510
    end = minutes(departure) if departure else 1439
    # Departure-only mornings can begin before the ordinary 08:30 default.
    if departure and not arrival:
        start = min(start, max(0, end - 120))
    windows = (('breakfast', 7*60, 10*60, 30), ('lunch', 12*60, 14*60, 45), ('dinner', 17*60, 20*60+30, 60))
    kinds = [kind for kind, earliest, latest, length in windows
             if max(start, earliest) <= min(latest, end-length)] if arrival or departure else list(MEALS)
    return dict(start_minutes=start, end_minutes=end, meal_types=kinds,
                allow_empty_attractions=bool((arrival and start >= 18*60) or (departure and end <= 11*60)),
                constrained=bool(arrival or departure))
