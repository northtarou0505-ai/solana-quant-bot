"""
凄腕トレーダーのエントリー・エグジット判断を行うシグナルエンジン（100点完成版）。
1. トレンド順張り（押し目・出来高ブレイクアウト ＋ 高値掴み防止フィルター）
2. 高流動性銘柄の急落リバウンド（売られすぎからの平均回帰 ＋ 下ヒゲ反発・落ちるナイフ防止フィルター）
3. SOL基軸マーケット・レジームフィルター（親玉のSOLが急落崩壊している時はアルトコインの買いを停止）
を統合し、手数料控除後の実質リスクリワード比を厳格に審査する。
"""

from dataclasses import dataclass
from config.settings import SETTINGS
from src.data.market_data import MarketSnapshot
from src.strategy.indicators import TechnicalIndicators, compute_all_indicators


@dataclass
class TradeSignal:
    """エントリー判定結果と動的に算出された損切り・利確目標。"""
    symbol: str
    mint_address: str
    action: str  # "BUY" または "HOLD"
    strategy_name: str
    confidence_score: int  # 0〜100の確信度スコア
    entry_price: float
    stop_loss_price: float
    take_profit_price: float
    stop_loss_pct: float
    take_profit_pct: float
    net_risk_reward_ratio: float
    reason: str
    indicators: TechnicalIndicators


class QuantSignalEngine:
    """テクニカル指標と市場構造から優位性（エッジ）のあるシグナルのみを抽出するエンジン。"""

    def evaluate_snapshot(
        self, snapshot: MarketSnapshot, sol_1h_change_pct: float = 0.0
    ) -> TradeSignal:
        """
        市場スナップショットとSOL全体の地合いを分析し、期待値がプラスかつリスクリワード条件を満たす場合のみBUYを返す。
        """
        ind = compute_all_indicators(snapshot.candles_5m, latest_spot_price=snapshot.price_usd)
        price = ind.current_price

        # ATR（ボラティリティ）に基づいた動的な損切り幅・利確幅の算出
        raw_sl_pct = max(ind.atr_pct * 2.2, 1.2)
        stop_loss_pct = min(raw_sl_pct, SETTINGS.max_stop_loss_pct)
        take_profit_pct = max(stop_loss_pct * 2.1, SETTINGS.min_take_profit_pct)

        # 往復手数料・スリッページ（約0.25%）を差し引いた「実質リスクリワード比」
        fee_pct = SETTINGS.estimated_roundtrip_fee_pct
        net_profit_pct = max(take_profit_pct - fee_pct, 0.01)
        net_loss_pct = stop_loss_pct + fee_pct
        net_rr = net_profit_pct / net_loss_pct

        stop_loss_price = price * (1.0 - stop_loss_pct / 100.0)
        take_profit_price = price * (1.0 + take_profit_pct / 100.0)

        # --- 全体地合い（マーケットレジーム）フィルター ---
        # 基軸通貨のSOLが直近1時間で -4.5% 以上暴落している最中は、連鎖下落を防ぐため新規買いを見送る
        if sol_1h_change_pct <= -4.5:
            return TradeSignal(
                symbol=snapshot.symbol,
                mint_address=snapshot.mint_address,
                action="HOLD",
                strategy_name="Market_Regime_Guard",
                confidence_score=20,
                entry_price=price,
                stop_loss_price=stop_loss_price,
                take_profit_price=take_profit_price,
                stop_loss_pct=stop_loss_pct,
                take_profit_pct=take_profit_pct,
                net_risk_reward_ratio=net_rr,
                reason=f"地合い警戒待機 (SOL 1h変動={sol_1h_change_pct:+.2f}% の急落局面のため新規買い保護)",
                indicators=ind,
            )

        # --- 戦略A: トレンドフォロー＆出来高ブレイクアウト（順張り） ---
        is_uptrend = ind.ema_9 > ind.ema_21 and ind.ema_21 >= (ind.ema_50 * 0.997)
        has_healthy_momentum = 48.0 <= ind.rsi_14 <= 69.0
        has_volume_support = ind.volume_ratio >= 1.20
        is_above_fast_ema = price >= ind.ema_9 * 0.998
        # 高値掴み防止: EMA9から上方に乖離しすぎていないこと＆長い上ヒゲ陰線でないこと
        max_allowable_extension = ind.ema_9 * (1.0 + max(ind.atr_pct * 2.5, 2.0) / 100.0)
        not_overextended = price <= max_allowable_extension and ind.close_position_in_range >= 0.45

        if (
            is_uptrend
            and has_healthy_momentum
            and has_volume_support
            and is_above_fast_ema
            and ind.is_bullish_candle
            and not_overextended
        ):
            score = 76
            if ind.volume_ratio >= 1.8:
                score += 10
            if ind.ema_21 > ind.ema_50:
                score += 8
            if ind.close_position_in_range >= 0.70:
                score += 4
            score = min(score, 98)

            if net_rr >= SETTINGS.min_risk_reward_ratio:
                return TradeSignal(
                    symbol=snapshot.symbol,
                    mint_address=snapshot.mint_address,
                    action="BUY",
                    strategy_name="Trend_Breakout_Follow",
                    confidence_score=score,
                    entry_price=price,
                    stop_loss_price=stop_loss_price,
                    take_profit_price=take_profit_price,
                    stop_loss_pct=stop_loss_pct,
                    take_profit_pct=take_profit_pct,
                    net_risk_reward_ratio=net_rr,
                    reason=(
                        f"上昇トレンド順張り合致 (EMA9>21, RSI={ind.rsi_14:.1f}, "
                        f"出来高倍率={ind.volume_ratio:.2f}x, 実質RR=1:{net_rr:.2f})"
                    ),
                    indicators=ind,
                )

        # --- 戦略B: 高流動性銘柄の急落リバウンド（売られすぎからの平均回帰） ---
        is_oversold = ind.rsi_14 <= 34.0
        is_near_lower_bb = price <= ind.bb_lower * 1.005
        not_crashing = snapshot.price_change_24h_pct > -18.0 and snapshot.price_change_1h_pct > -6.0
        # 落ちるナイフ防止: 必ず陽線かつローソク足レンジの上半分以上で引けて買い支えが確認できること
        has_strong_bounce_wick = ind.is_bullish_candle and ind.close_position_in_range >= 0.50

        if (
            (is_oversold or (ind.rsi_14 <= 38.0 and is_near_lower_bb))
            and has_strong_bounce_wick
            and not_crashing
        ):
            score = 74
            if is_oversold and is_near_lower_bb:
                score += 14
            if ind.volume_ratio >= 1.5:
                score += 8
            score = min(score, 96)

            if net_rr >= SETTINGS.min_risk_reward_ratio:
                return TradeSignal(
                    symbol=snapshot.symbol,
                    mint_address=snapshot.mint_address,
                    action="BUY",
                    strategy_name="Oversold_Mean_Reversion",
                    confidence_score=score,
                    entry_price=price,
                    stop_loss_price=stop_loss_price,
                    take_profit_price=take_profit_price,
                    stop_loss_pct=stop_loss_pct,
                    take_profit_pct=take_profit_pct,
                    net_risk_reward_ratio=net_rr,
                    reason=(
                        f"急落リバウンド反発検知 (RSI={ind.rsi_14:.1f}, BB下限反発陽線, "
                        f"出来高倍率={ind.volume_ratio:.2f}x, 実質RR=1:{net_rr:.2f})"
                    ),
                    indicators=ind,
                )

        # 条件未達の場合は見送り（HOLD）
        hold_reason = (
            f"優位性待機中 (Price=${price:.4f}, RSI={ind.rsi_14:.1f}, "
            f"EMA9/21={ind.ema_9:.4f}/{ind.ema_21:.4f}, Vol={ind.volume_ratio:.2f}x)"
        )
        return TradeSignal(
            symbol=snapshot.symbol,
            mint_address=snapshot.mint_address,
            action="HOLD",
            strategy_name="None",
            confidence_score=40,
            entry_price=price,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            net_risk_reward_ratio=net_rr,
            reason=hold_reason,
            indicators=ind,
        )
