import type { Attraction, DayPlan, Hotel, Meal, RouteLeg, TripPlan } from '../types/trip'

export type DayStop = {
  key: string
  kind: 'attraction' | 'meal'
  place: Attraction | Meal
  incoming?: RouteLeg
  returnToHotel?: RouteLeg
}

export function dayStops(day: DayPlan, hotel?: Hotel | null): DayStop[] {
  const spots = day.attractions ?? [], meals = day.meals ?? []
  const usedSpots = new Set<number>(), usedMeals = new Set<number>()
  const ordered: DayStop[] = []
  function add(kind: DayStop['kind'], index: number) {
    const pool = kind === 'attraction' ? spots : meals
    const used = kind === 'attraction' ? usedSpots : usedMeals
    if (index < 0 || used.has(index)) return
    used.add(index)
    ordered.push({ key: `${kind}-${index}`, kind, place: pool[index]! })
  }
  for (const entry of day.schedule ?? []) {
    if (entry.kind === 'attraction') {
      add('attraction', spots.findIndex((s, i) => !usedSpots.has(i) &&
        (entry.reference_id ? s.poi_id === entry.reference_id : s.name === entry.name)))
    } else if (entry.kind === 'meal') {
      add('meal', meals.findIndex((m, i) => !usedMeals.has(i) &&
        (!entry.meal_type || m.type === entry.meal_type) &&
        (entry.reference_id ? m.restaurant_id === entry.reference_id : m.name === entry.name)))
    }
  }
  // Legacy plans have no schedule: retain every entity without inventing times.
  if (!ordered.length) {
    meals.forEach((m, i) => { if (m.type === 'breakfast') add('meal', i) })
    spots.forEach((_, i) => {
      add('attraction', i)
      if (i === Math.max(0, Math.ceil(spots.length / 2) - 1)) {
        meals.forEach((m, j) => { if (m.type === 'lunch') add('meal', j) })
      }
    })
  }
  spots.forEach((_, i) => add('attraction', i))
  meals.forEach((_, i) => add('meal', i))
  let cursor = 0
  const legs = day.route_legs ?? []
  for (const stop of ordered) {
    const id = stop.kind === 'attraction' ? (stop.place as Attraction).poi_id : (stop.place as Meal).restaurant_id
    const index = legs.findIndex((leg, i) => i >= cursor &&
      (leg.to_id ? leg.to_id === id : leg.to_name === stop.place.name))
    if (index >= 0) { stop.incoming = legs[index]; cursor = index + 1 }
  }
  const end = day.dated_boundaries ? day.route_end : hotel
  const last = legs[legs.length - 1]
  if (ordered.length && end && last && legs.length - 1 >= cursor &&
      (last.to_id ? last.to_id === end.poi_id : last.to_name === end.name)) {
    ordered[ordered.length - 1]!.returnToHotel = last
  }
  return ordered
}

export function routeText(leg: RouteLeg): string {
  const mode = ({ walking: '步行', driving: '驾车', transit: '公共交通', bicycling: '骑行' } as Record<string, string>)[leg.mode] ?? leg.mode
  const duration = leg.status === 'unknown' ? '耗时待核实' : `${leg.status === 'estimated' ? '约 ' : ''}${leg.duration_minutes} 分钟`
  const distance = leg.distance_meters == null ? '' : leg.distance_meters < 1000 ?
    ` · ${Math.round(leg.distance_meters)} 米` : ` · ${(leg.distance_meters / 1000).toFixed(1)} 公里`
  return `${mode} · ${duration}${distance}`
}

/** Money and budget verdicts are owned by the backend, including old versions. */
export function preparePlanForDisplay(plan: TripPlan): TripPlan {
  return plan
}
