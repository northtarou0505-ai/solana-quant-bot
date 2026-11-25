"""
クオンツ取引に必要なテクニカル指標（EMA, RSI, ボリンジャーバンド, ATR, 出来高倍率, 相場レジーム判定, 二番底・プライスアクション）を計算するモジュール。
外部C拡張ライブラリに依存せず、標準ライブラリのみで高速・正確に算出する。
"""

import math
from dataclasses import dataclass
from typing import List
from src.data.market_data import Candle


@dataclass
class TechnicalIndicators:
    """算出された最新のテクニカル指標・相場環境（レジーム）・プライスアクション指標セット。"""
    current_price: float
    ema_9: float
    ema_21: float
    ema_50: float
    ema_50_slope_pct: float  # 直近1時間(12本)での長期トレンド傾き(%)
    market_regime: str       # "BULL_TREND" (上昇期), "RANGE_CHOP" (レンジ期), "BEAR_DOWNTREND" (下落調整期)
    rsi_14: float
    bb_upper: float
    bb_middle: float
    bb_lower: float
    bb_width_pct: float
    atr_14: float
    atr_pct: float
    volume_ratio: float
    is_bullish_candle: bool
    close_position_in_range: float  # ローソク足の安値〜高値に対する終値の位置 (0.0=最安値引け, 1.0=最高値引け)
    is_higher_low_or_reversal: bool  # 直近足が安値切り上げ（二番底）または前足実体を包む強い反発陽線か
    touched_bb_lower: bool           # 直近足の安値(Low)または現在価格がボリンジャーバンド下限に到達したか


def calculate_ema(values: List[float], period: int) -> float:
    """指数平滑移動平均（EMA）を計算する。"""
    if not values:
        return 0.0
    if len(values) < period:
        return sum(values) / len(values)

    multiplier = 2.0 / (period + 1.0)
    ema = sum(values[:period]) / period
    for price in values[period:]:
        ema = (price - ema) * multiplier + ema
    return ema


def calculate_rsi(closes: List[float], period: int = 14) -> float:
    """Wilder法によるRSI（相対力指数）を計算する。"""
    if len(closes) <= period:
        return 50.0

    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def calculate_bollinger_bands(
    closes: List[float], period: int = 20, std_dev_mult: float = 2.0
) -> tuple[float, float, float, float]:
    """ボリンジャーバンド（上限、中央SMA、下限、バンド幅%）を計算する。"""
    window = closes[-period:] if len(closes) >= period else closes
    if not window:
        return 0.0, 0.0, 0.0, 0.0

    middle = sum(window) / len(window)
    variance = sum((x - middle) ** 2 for x in window) / len(window)
    std_dev = math.sqrt(variance)

    upper = middle + std_dev_mult * std_dev
    lower = middle - std_dev_mult * std_dev
    width_pct = ((upper - lower) / middle * 100.0) if middle > 0 else 0.0
    return upper, middle, lower, width_pct


def calculate_atr(candles: List[Candle], period: int = 14) -> float:
    """ATR（Average True Range：平均真の値幅）を計算する。"""
    if len(candles) < 2:
        return 0.0

    true_ranges: List[float] = []
    for i in range(1, len(candles)):
        curr = candles[i]
        prev_close = candles[i - 1].close
        tr = max(
            curr.high - curr.low,
            abs(curr.high - prev_close),
            abs(curr.low - prev_close),
        )
        true_ranges.append(tr)

    if len(true_ranges) < period:
        return sum(true_ranges) / len(true_ranges)

    atr = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def compute_all_indicators(candles: List[Candle], latest_spot_price: float = 0.0) -> TechnicalIndicators:
    """ローソク足リストから全テクニカル指標・相場レジーム・プライスアクション強度を一括算出する。"""
    closes = [c.close for c in candles]
    volumes = [c.volume for c in candles]
    current_price = latest_spot_price if latest_spot_price > 0 else closes[-1]

    ema_9 = calculate_ema(closes, 9)
    ema_21 = calculate_ema(closes, 21)
    ema_50 = calculate_ema(closes, 50)

    # 1時間前（12本前）のEMA50と比較して上位足のトレンド傾き(%)を算出
    if len(closes) > 15:
        prev_ema_50 = calculate_ema(closes[:-12], min(50, len(closes) - 12))
        ema_50_slope_pct = ((ema_50 - prev_ema_50) / prev_ema_50 * 100.0) if prev_ema_50 > 0 else 0.0
    else:
        ema_50_slope_pct = 0.0

    # 相場レジーム（時期・地合い）の3分類
    if ema_9 > ema_21 and ema_21 >= ema_50 * 0.998 and ema_50_slope_pct >= -0.15:
        market_regime = "BULL_TREND"
    elif ema_21 < ema_50 * 0.996 and ema_50_slope_pct <= -0.35:
        market_regime = "BEAR_DOWNTREND"
    else:
        market_regime = "RANGE_CHOP"

    rsi_14 = calculate_rsi(closes, 14)
    bb_upper, bb_middle, bb_lower, bb_width_pct = calculate_bollinger_bands(closes, 20, 2.0)
    atr_14 = calculate_atr(candles, 14)
    atr_pct = (atr_14 / current_price * 100.0) if current_price > 0 else 1.0

    vol_window = volumes[-21:-1] if len(volumes) > 21 else volumes[:-1]
    avg_vol = (sum(vol_window) / len(vol_window)) if vol_window else 1.0
    latest_vol = volumes[-1] if volumes else 0.0
    if len(volumes) >= 2 and latest_vol < avg_vol * 0.5:
        latest_vol = max(latest_vol, volumes[-2])
    volume_ratio = (latest_vol / avg_vol) if avg_vol > 0 else 1.0

    last_candle = candles[-1]
    is_bullish_candle = last_candle.close >= last_candle.open
    candle_range = last_candle.high - last_candle.low
    if candle_range > 0:
        close_position_in_range = max(0.0, min(1.0, (last_candle.close - last_candle.low) / candle_range))
    else:
        close_position_in_range = 0.5

    # 安値切り上げ（二番底）または前足高値を上抜く強い反転足かどうかの確認（下落途中の1本目ナイフ掴み防止）
    if len(candles) >= 3:
        prev_c = candles[-2]
        prev2_c = candles[-3]
        higher_low = last_candle.low >= min(prev_c.low, prev2_c.low) * 0.999
        bullish_engulf = is_bullish_candle and (last_candle.close >= prev_c.open)
        is_higher_low_or_reversal = higher_low or bullish_engulf
    else:
        is_higher_low_or_reversal = is_bullish_candle

    touched_bb_lower = (last_candle.low <= bb_lower * 1.005) or (current_price <= bb_lower * 1.005)

    return TechnicalIndicators(
        current_price=current_price,
        ema_9=ema_9,
        ema_21=ema_21,
        ema_50=ema_50,
        ema_50_slope_pct=ema_50_slope_pct,
        market_regime=market_regime,
        rsi_14=rsi_14,
        bb_upper=bb_upper,
        bb_middle=bb_middle,
        bb_lower=bb_lower,
        bb_width_pct=bb_width_pct,
        atr_14=atr_14,
        atr_pct=atr_pct,
        volume_ratio=volume_ratio,
        is_bullish_candle=is_bullish_candle,
        close_position_in_range=close_position_in_range,
        is_higher_low_or_reversal=is_higher_low_or_reversal,
        touched_bb_lower=touched_bb_lower,
    )
