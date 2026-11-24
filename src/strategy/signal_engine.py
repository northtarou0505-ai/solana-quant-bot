"""
凄腕トレーダーのエントリー・エグジット判断を行うシグナルエンジン（AIコパイロット＆判断材料提示対応版）。
シグナルを以下の3段階で判定する：
- "BUY" (スコア76点以上): 条件が完璧に合致した鉄板シグナル（ボットが即自動エントリー）
- "ASK" (スコア60〜75点): プラス材料はあるが懸念点も残る「微妙（ボーダーライン）」シグナル（判断材料を添えてTelegramでユーザーに判断を仰ぐ）
- "HOLD" (スコア59点以下): 優位性なし（見送り）
"""

from dataclasses import dataclass, field
from typing import List
from config.settings import SETTINGS
from src.data.market_data import MarketSnapshot
from src.strategy.indicators import TechnicalIndicators, compute_all_indicators


@dataclass
class TradeSignal:
    """エントリー判定結果・判断材料（プラス根拠／懸念点）・損切り利確目標。"""
    symbol: str
    mint_address: str
    action: str  # "BUY" (即自動購入), "ASK" (Telegramでユーザー判断を仰ぐ), "HOLD" (待機)
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
    pros: List[str] = field(default_factory=list)  # 買いを推すプラスの判断材料
    cons: List[str] = field(default_factory=list)  # 注意すべき懸念・マイナス材料


class QuantSignalEngine:
    """テクニカル指標と市場構造から優位性（エッジ）と判断材料を抽出するエンジン。"""

    def evaluate_snapshot(
        self, snapshot: MarketSnapshot, sol_1h_change_pct: float = 0.0
    ) -> TradeSignal:
        """
        市場スナップショットとSOL全体の地合いを分析し、
        鉄板の「BUY」、判断を仰ぐ微妙な「ASK」、見送りの「HOLD」を判断材料付きで返す。
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
                pros=[],
                cons=[f"基軸通貨SOLが直近1時間で {sol_1h_change_pct:+.2f}% 急落しており市場全体がリスクオフ"],
            )

        # =====================================================================
        # 戦略A: トレンドフォロー＆出来高ブレイクアウト（順張り）
        # =====================================================================
        is_uptrend = ind.ema_9 > ind.ema_21 and ind.ema_21 >= (ind.ema_50 * 0.997)
        has_healthy_momentum = 48.0 <= ind.rsi_14 <= 69.0
        has_volume_support = ind.volume_ratio >= 1.20
        is_above_fast_ema = price >= ind.ema_9 * 0.998
        max_allowable_extension = ind.ema_9 * (1.0 + max(ind.atr_pct * 2.5, 2.0) / 100.0)
        not_overextended = price <= max_allowable_extension and ind.close_position_in_range >= 0.45

        if (
            is_uptrend
            and has_healthy_momentum
            and has_volume_support
            and is_above_fast_ema
            and ind.is_bullish_candle
            and not_overextended
            and net_rr >= SETTINGS.min_risk_reward_ratio
        ):
            score = 76
            pros = [
                f"短期・中期・長期EMAが上昇配列 (EMA9=${ind.ema_9:.4f} > EMA21=${ind.ema_21:.4f})",
                f"RSI(14)が {ind.rsi_14:.1f} と過熱感のない健全な上昇モメンタム帯",
                f"出来高が直近平均の {ind.volume_ratio:.2f}倍 に増加し買いの裏付けあり",
                f"手数料控除後の実質リスクリワード比が 1 : {net_rr:.2f} (目標+{take_profit_pct:.1f}% / 損切-{stop_loss_pct:.1f}%)",
            ]
            cons = []
            if ind.volume_ratio >= 1.8:
                score += 10
            if ind.ema_21 > ind.ema_50:
                score += 8
            if ind.close_position_in_range >= 0.70:
                score += 4
            score = min(score, 98)

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
                pros=pros,
                cons=cons,
            )

        # =====================================================================
        # 戦略B: 高流動性銘柄の急落リバウンド（売られすぎからの平均回帰）
        # =====================================================================
        is_oversold = ind.rsi_14 <= 34.0
        is_near_lower_bb = price <= ind.bb_lower * 1.005
        not_crashing = snapshot.price_change_24h_pct > -18.0 and snapshot.price_change_1h_pct > -6.0
        has_strong_bounce_wick = ind.is_bullish_candle and ind.close_position_in_range >= 0.50

        if (
            (is_oversold or (ind.rsi_14 <= 38.0 and is_near_lower_bb))
            and has_strong_bounce_wick
            and not_crashing
            and net_rr >= SETTINGS.min_risk_reward_ratio
        ):
            score = 76
            pros = [
                f"RSI(14)が {ind.rsi_14:.1f} まで売られすぎ水準に到達し反発陽線が出現",
                f"ローソク足の下ヒゲから買い支えが入りレンジ上位({ind.close_position_in_range*100:.0f}%位置)で引け",
                f"流動性 ${snapshot.liquidity_usd:,.0f} の主要銘柄であり自律反発の期待値が高い",
                f"実質リスクリワード比 1 : {net_rr:.2f} (目標+{take_profit_pct:.1f}% / 損切-{stop_loss_pct:.1f}%)",
            ]
            if is_oversold and is_near_lower_bb:
                score += 12
                pros.append(f"ボリンジャーバンド下限(${ind.bb_lower:.4f})タッチからの強い平均回帰シグナル")
            if ind.volume_ratio >= 1.5:
                score += 8
                pros.append(f"出来高倍率 {ind.volume_ratio:.2f}x のセリングクライマックス吸収を確認")
            score = min(score, 96)

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
                pros=pros,
                cons=[],
            )

        # =====================================================================
        # ボーダーライン（微妙な局面：スコア60〜75点）の検出 -> ユーザーに判断を仰ぐ (ASK)
        # =====================================================================
        # ケース1: 順張りトレンドだが、出来高がやや足りない or RSIが少し高め(69〜74) or 押し目待機中
        if is_uptrend and 46.0 <= ind.rsi_14 <= 74.0 and price >= ind.ema_21:
            pros = [
                f"トレンド自体は上昇基調 (EMA9=${ind.ema_9:.4f} > EMA21=${ind.ema_21:.4f})",
                f"現在価格(${price:.4f})は中期サポートEMA21(${ind.ema_21:.4f})の上方をキープ",
                f"実質リスクリワード比 1 : {net_rr:.2f} (目標+{take_profit_pct:.1f}% / 損切-{stop_loss_pct:.1f}%)",
            ]
            cons = []
            score = 66
            if ind.volume_ratio < 1.20:
                cons.append(f"出来高倍率が {ind.volume_ratio:.2f}x とブレイク基準(1.20x)よりやや薄く、騙しの懸念あり")
            else:
                pros.append(f"出来高倍率は {ind.volume_ratio:.2f}x と十分")
                score += 4
            if ind.rsi_14 > 69.0:
                cons.append(f"RSI(14)が {ind.rsi_14:.1f} とやや過熱圏に近く、短期的な高値掴みリスクあり")
            if not ind.is_bullish_candle:
                cons.append("直近5分足が陰線（押し目形成中）であり、反発陽線の確定前")
            if not not_overextended:
                cons.append(f"EMA9からの上方乖離または上ヒゲがあり、短期反落に注意が必要")

            if cons and net_rr >= SETTINGS.min_risk_reward_ratio:
                return TradeSignal(
                    symbol=snapshot.symbol,
                    mint_address=snapshot.mint_address,
                    action="ASK",
                    strategy_name="Borderline_Trend_Setup",
                    confidence_score=min(score, 74),
                    entry_price=price,
                    stop_loss_price=stop_loss_price,
                    take_profit_price=take_profit_price,
                    stop_loss_pct=stop_loss_pct,
                    take_profit_pct=take_profit_pct,
                    net_risk_reward_ratio=net_rr,
                    reason=(
                        f"🤔 判断伺い: 上昇トレンド中だが一部懸念あり "
                        f"(RSI={ind.rsi_14:.1f}, Vol={ind.volume_ratio:.2f}x)"
                    ),
                    indicators=ind,
                    pros=pros,
                    cons=cons,
                )

        # ケース2: 売られすぎ圏（RSI <= 41 または BB下限付近）だが、まだ強い反発陽線が確定していない微妙な局面
        if (ind.rsi_14 <= 41.0 or price <= ind.bb_lower * 1.012) and not_crashing:
            pros = [
                f"短期的に売られすぎ水準に接近 (RSI={ind.rsi_14:.1f}, BB下限=${ind.bb_lower:.4f})",
                f"1時間変動率 {snapshot.price_change_1h_pct:+.2f}% の押し目であり、反発すれば+{take_profit_pct:.1f}%を狙える位置",
                f"実質リスクリワード比 1 : {net_rr:.2f}",
            ]
            cons = []
            score = 64
            if not ind.is_bullish_candle:
                cons.append("直近足がまだ陰線であり、底打ち反発（陽線転換）が未確定（もう一段下げるリスク）")
            elif ind.close_position_in_range < 0.50:
                cons.append(f"陽線だが上ヒゲが重く（終値位置 {ind.close_position_in_range*100:.0f}%）、戻り売り圧力が残存")
            if ind.rsi_14 > 34.0:
                cons.append(f"RSIが {ind.rsi_14:.1f} であり、極端な売られすぎ基準(34以下)まではあと一歩余裕あり")
            if ind.ema_9 < ind.ema_21:
                cons.append(f"短期トレンド自体は下向き (EMA9=${ind.ema_9:.4f} < EMA21=${ind.ema_21:.4f}) の逆張りエントリー")

            if cons and net_rr >= SETTINGS.min_risk_reward_ratio:
                return TradeSignal(
                    symbol=snapshot.symbol,
                    mint_address=snapshot.mint_address,
                    action="ASK",
                    strategy_name="Borderline_Dip_Rebound",
                    confidence_score=min(score, 73),
                    entry_price=price,
                    stop_loss_price=stop_loss_price,
                    take_profit_price=take_profit_price,
                    stop_loss_pct=stop_loss_pct,
                    take_profit_pct=take_profit_pct,
                    net_risk_reward_ratio=net_rr,
                    reason=(
                        f"🤔 判断伺い: 押し目・安値圏だが反発強度の見極め局面 "
                        f"(RSI={ind.rsi_14:.1f}, Vol={ind.volume_ratio:.2f}x)"
                    ),
                    indicators=ind,
                    pros=pros,
                    cons=cons,
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
            pros=[],
            cons=["トレンド順張り・急落逆張りのいずれの基準にも達していない中立レンジ帯"],
        )
