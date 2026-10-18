"""Pure pacing decisions. Only mature observations at an unchanged bid are used."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


@dataclass
class Decision:
    action: str
    reason: str
    cpm: float | None = None
    rate: float | None = None
    target_rate: float | None = None


def money(value):
    return float(Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def decide(ad, policy, snapshots, now, last_sync):
    if not policy.get('enabled'):
        return Decision('hold', 'Автоподбор выключен')
    if now - last_sync > 900:
        return Decision('hold', 'Статистика устарела')
    if ad.get('status') != 'active':
        return Decision('hold', 'Ожидаем активного объявления')
    cpm = float(ad['cpm'])
    if policy.get('last_cpm') is not None and abs(cpm - policy['last_cpm']) > 0.009:
        return Decision('external', 'Ставка изменена вне стратегии; автоподбор приостановлен')
    start = policy['started_at']
    end = policy['deadline']
    current_views = max(0, ad.get('views', 0) - policy['baseline_views'])
    spend = max(0, ad.get('spent_budget', 0) - policy['baseline_spent'])
    if now >= end:
        return Decision('pause', 'Завершён период размещения')
    if current_views >= policy['target_views']:
        return Decision('pause', 'Цель по показам достигнута')
    if spend >= policy['spend_limit']:
        return Decision('pause', 'Достигнут лимит расходов стратегии')
    if ad.get('remaining_budget', 0) <= 0:
        return Decision('hold', 'Закончился бюджет объявления')
    last_change = policy.get('last_change', start)
    if now - last_change < policy['cooldown_minutes'] * 60:
        return Decision('hold', 'Ожидаем применения ставки и накопления данных')
    # Exclude the latency after the last bid change; require enough data at one bid.
    mature_start = max(start, last_change + policy['settle_minutes'] * 60)
    window_start = max(mature_start, now - policy['window_minutes'] * 60)
    usable = [s for s in snapshots if s['ts'] >= window_start and abs(s['cpm'] - cpm) < 0.009 and s['status'] == 'active']
    if len(usable) < 2 or usable[-1]['ts'] - usable[0]['ts'] < policy['window_minutes'] * 60 * 0.8:
        return Decision('hold', 'Недостаточно наблюдений при текущей ставке')
    first, last = usable[0], usable[-1]
    if last['views'] < first['views']:
        return Decision('hold', 'Счётчик показов изменился; ожидаем новое окно')
    hours = (last['ts'] - first['ts']) / 3600
    rate = (last['views'] - first['views']) / hours
    target_rate = (policy['target_views'] - current_views) / max((end - now) / 3600, 0.01)
    if ad.get('placement') == 'search_result' and rate == 0:
        return Decision('hold', 'Нет показов в поиске: недостаточно данных о спросе; проверьте фразу и ставку вручную', rate=rate, target_rate=target_rate)
    tolerance = policy['tolerance_percent'] / 100
    step = policy['step_percent'] / 100
    if rate < target_rate * (1 - tolerance):
        new = money(min(policy['max_cpm'], max(policy['min_cpm'], cpm * (1 + step))))
        if new <= cpm:
            return Decision('hold', 'Достигнут потолок CPM; цель может быть недостижима', rate=rate, target_rate=target_rate)
        return Decision('bid', 'Темп показов ниже цели', new, rate, target_rate)
    if rate > target_rate * (1 + tolerance):
        new = money(max(policy['min_cpm'], min(policy['max_cpm'], cpm * (1 - step))))
        if new >= cpm:
            return Decision('hold', 'Достигнута минимальная ставка', rate=rate, target_rate=target_rate)
        return Decision('bid', 'Темп показов выше цели; пробуем снизить CPM', new, rate, target_rate)
    return Decision('hold', 'Темп показов в целевом диапазоне', rate=rate, target_rate=target_rate)
