"""
凄腕トレーダーのエントリー・エグジット判断を行う V3 レジーム適応型シグナルエンジン（100点完成版）。

【実トレード7戦の敗因解析から導入された3大進化】
1. 相場レジーム（BULL上昇期 / RANGE揉み合い期 / BEAR下落調整期）の自動識別：
   - 下落トレンド期(BEAR_DOWNTREND)での安易な逆張り早仕掛け（落ちるナイフ掴み）を自動ブロックし、
     「二番底（安値切り上げ）＋セリングクライマックス出来高＋極限売られすぎ(RSI<=29)」が揃った大底のみ厳選エントリー。
2. 対SOL 相対強度（Relative Strength）フィルター：
   - 基軸通貨SOLよりモメンタムが弱い出遅れアルトコイン（RAY等）のダマシ・ブレイクアウト飛び乗りを防止。
3. 3段階シグナル判定（"BUY": 76点以上即エントリー / "ASK": 60〜75点判断材料付きTelegram伺い / "HOLD": 見送り）。
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
    pros: List[str] = field(default_factory=list)
    cons: List[str] = field(default_factory=list)


class QuantSignalEngine:
    """相場環境（レジーム）に応じて順張り・逆張りの基準を動的に切り替えるV3クオンツエンジン。"""

    def evaluate_snapshot(
        self, snapshot: MarketSnapshot, sol_1h_change_pct: float = 0.0
    ) -> TradeSignal:
        """
        市場スナップショットとSOL全体の地合い（相対強度）を分析し、
        鉄板の「BUY」、判断を仰ぐ微妙な「ASK」、見送りの「HOLD」を判断材料付きで返す。
        """
        ind = compute_all_indicators(snapshot.candles_5m, latest_spot_price=snapshot.price_usd)
        price = ind.current_price
        regime = ind.market_regime

        # ATR（ボラティリティ）に基づいた動的な損切り幅・利確幅の算出
        raw_sl_pct = max(ind.atr_pct * 2.2, 1.25)
        stop_loss_pct = min(raw_sl_pct, SETTINGS.max_stop_loss_pct)
        take_profit_pct = max(stop_loss_pct * 2.1, SETTINGS.min_take_profit_pct)

        # 往復手数料・スリッページ（約0.25%）を差し引いた「実質リスクリワード比」
        fee_pct = SETTINGS.estimated_roundtrip_fee_pct
        net_profit_pct = max(take_profit_pct - fee_pct, 0.01)
        net_loss_pct = stop_loss_pct + fee_pct
        net_rr = net_profit_pct / net_loss_pct

        stop_loss_price = price * (1.0 - stop_loss_pct / 100.0)
        take_profit_price = price * (1.0 + take_profit_pct / 100.0)

        # アルトコインの対SOL相対強度（Relative Strength）
        is_altcoin = snapshot.symbol != "SOL"
        relative_strength_1h = snapshot.price_change_1h_pct - sol_1h_change_pct if is_altcoin else 0.0

        # --- 全体地合い（マーケットレジーム）緊急防衛フィルター ---
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
        is_uptrend = (
            ind.ema_9 > ind.ema_21
            and ind.ema_21 >= (ind.ema_50 * 0.998)
            and ind.ema_50_slope_pct >= -0.10
        )
        has_healthy_momentum = 48.0 <= ind.rsi_14 <= 68.5
        has_volume_support = ind.volume_ratio >= 1.25
        is_above_fast_ema = price >= ind.ema_9 * 0.998
        max_allowable_extension = ind.ema_9 * (1.0 + max(ind.atr_pct * 2.4, 1.8) / 100.0)
        not_overextended = price <= max_allowable_extension and ind.close_position_in_range >= 0.50
        # アルトコインの場合はSOLに対して大きく劣後していないこと（出遅れダマシ防止）
        has_relative_strength = (not is_altcoin) or (relative_strength_1h >= -0.8)

        if (
            is_uptrend
            and has_healthy_momentum
            and has_volume_support
            and is_above_fast_ema
            and ind.is_bullish_candle
            and not_overextended
            and has_relative_strength
            and net_rr >= SETTINGS.min_risk_reward_ratio
        ):
            score = 78
            pros = [
                f"相場レジーム【{regime}】: EMA9>21>50の上昇トレンドかつ長期EMA傾き({ind.ema_50_slope_pct:+.2f}%)が堅調",
                f"RSI(14)が {ind.rsi_14:.1f} と過熱感のない健全なモメンタム推進帯",
                f"出来高が直近平均の {ind.volume_ratio:.2f}倍 に急増し大口買いの裏付けあり",
                f"実質リスクリワード比 1 : {net_rr:.2f} (目標+{take_profit_pct:.1f}% / 損切-{stop_loss_pct:.1f}%)",
            ]
            if is_altcoin:
                pros.append(f"対SOL相対強度(1h)が {relative_strength_1h:+.2f}% と強い主役銘柄")
            if ind.volume_ratio >= 1.8:
                score += 10
            if ind.ema_50_slope_pct >= 0.20:
                score += 6
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
                    f"上昇トレンド順張り合致 (Regime={regime}, RSI={ind.rsi_14:.1f}, "
                    f"出来高={ind.volume_ratio:.2f}x, 実質RR=1:{net_rr:.2f})"
                ),
                indicators=ind,
                pros=pros,
                cons=[],
            )

        # =====================================================================
        # 戦略B: レジーム適応型・急落リバウンド（売られすぎからの平均回帰）
        # ※実トレードの敗因だった「下落トレンド途中の早仕掛け」を防ぐため、
        #   BEAR_DOWNTREND時は基準を大幅に厳格化（RSI<=29 + 出来高1.35x以上 + 二番底確認必須）
        # =====================================================================
        not_crashing = snapshot.price_change_24h_pct > -18.0 and snapshot.price_change_1h_pct > -6.0
        has_strong_bounce_wick = (
            ind.is_bullish_candle
            and ind.close_position_in_range >= 0.55
            and ind.is_higher_low_or_reversal
        )

        if regime == "BEAR_DOWNTREND":
            # 下落トレンド期は「大底セリングクライマックス反転（RSI<=31 + BB下限タッチ + 出来高1.35x以上 + 二番底陽線）」のみ自動BUY許可
            qualifies_auto_reversion = (
                ind.rsi_14 <= 31.0
                and ind.touched_bb_lower
                and ind.volume_ratio >= 1.35
                and has_strong_bounce_wick
                and not_crashing
            )
        else:
            # 上昇期・レンジ期の押し目は標準の売られすぎ反発＋二番底確認で自動BUY許可
            qualifies_auto_reversion = (
                (ind.rsi_14 <= 34.0 or (ind.rsi_14 <= 37.5 and ind.touched_bb_lower))
                and ind.volume_ratio >= 1.15
                and has_strong_bounce_wick
                and not_crashing
            )

        if qualifies_auto_reversion and net_rr >= SETTINGS.min_risk_reward_ratio:
            score = 78
            pros = [
                f"相場レジーム【{regime}】に応じた厳格な底打ち条件をクリア (RSI={ind.rsi_14:.1f})",
                f"二番底（安値切り上げ）または包み陽線を確認し、ローソク足上位({ind.close_position_in_range*100:.0f}%)で強く引け",
                f"反発出来高倍率 {ind.volume_ratio:.2f}x により投げ売りの吸収を確認",
                f"実質リスクリワード比 1 : {net_rr:.2f} (目標+{take_profit_pct:.1f}% / 損切-{stop_loss_pct:.1f}%)",
            ]
            if price <= ind.bb_lower * 1.002:
                score += 8
                pros.append(f"ボリンジャーバンド下限(${ind.bb_lower:.4f})到達からの平均回帰優位性あり")
            if ind.volume_ratio >= 1.6:
                score += 6
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
                    f"厳選リバウンド反発合致 (Regime={regime}, RSI={ind.rsi_14:.1f}, "
                    f"二番底陽線, 出来高={ind.volume_ratio:.2f}x)"
                ),
                indicators=ind,
                pros=pros,
                cons=[],
            )

        # =====================================================================
        # ボーダーライン（微妙な局面：スコア60〜75点）の検出 -> ユーザーに判断を仰ぐ (ASK)
        # =====================================================================
        # ケース1: 順張りトレンドだが、出来高不足 or 出遅れアルト or 押し目陰線
        if (ind.ema_9 > ind.ema_21 and price >= ind.ema_21 and 46.0 <= ind.rsi_14 <= 74.0):
            pros = [
                f"短期トレンドは上昇配列 (EMA9=${ind.ema_9:.4f} > EMA21=${ind.ema_21:.4f})",
                f"現在価格(${price:.4f})は中期サポートEMA21(${ind.ema_21:.4f})の上方を維持",
                f"実質リスクリワード比 1 : {net_rr:.2f} (目標+{take_profit_pct:.1f}% / 損切-{stop_loss_pct:.1f}%)",
            ]
            cons = []
            score = 66
            if ind.volume_ratio < 1.25:
                cons.append(f"出来高倍率が {ind.volume_ratio:.2f}x とブレイク基準(1.25x)より薄く、失速・ダマシの懸念あり")
            else:
                pros.append(f"出来高倍率は {ind.volume_ratio:.2f}x と十分")
                score += 4
            if is_altcoin and not has_relative_strength:
                cons.append(f"基軸SOLに対して1時間モメンタムが劣後({relative_strength_1h:+.2f}%)しており出遅れ感あり")
            if ind.ema_50_slope_pct < -0.10:
                cons.append(f"上位足EMA50の傾きがまだ下向き({ind.ema_50_slope_pct:+.2f}%)で戻り売りに押される懸念")
            if ind.rsi_14 > 68.5:
                cons.append(f"RSI(14)が {ind.rsi_14:.1f} とやや過熱圏に近く、高値掴みリスクあり")
            if not ind.is_bullish_candle or ind.close_position_in_range < 0.50:
                cons.append("直近足が陰線または上ヒゲが長く、買いの勢いがまだ未確定")

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
                        f"🤔 判断伺い: 上昇基調だが一部懸念あり "
                        f"(Regime={regime}, RSI={ind.rsi_14:.1f}, Vol={ind.volume_ratio:.2f}x)"
                    ),
                    indicators=ind,
                    pros=pros,
                    cons=cons,
                )

        # ケース2: 売られすぎ圏（RSI <= 39 または BB下限付近）だが、下落トレンド中または二番底未確定の微妙な局面
        if (ind.rsi_14 <= 39.0 or price <= ind.bb_lower * 1.010) and not_crashing:
            pros = [
                f"短期オシレーターは売られすぎ水準に接近 (RSI={ind.rsi_14:.1f}, BB下限=${ind.bb_lower:.4f})",
                f"反発すれば+{take_profit_pct:.1f}%を狙える安値エリア (実質RR 1 : {net_rr:.2f})",
            ]
            cons = []
            score = 64
            if regime == "BEAR_DOWNTREND":
                cons.append(
                    f"現在【下落トレンド期(EMA50傾き {ind.ema_50_slope_pct:+.2f}%)】のため、"
                    f"早仕掛けするとさらに一段掘る（落ちるナイフ）リスクが高い時期"
                )
            if not ind.is_higher_low_or_reversal:
                cons.append("まだ安値を切り下げている最中で、二番底（底固め）が確認できていない")
            if not ind.is_bullish_candle or ind.close_position_in_range < 0.55:
                cons.append("力強い下ヒゲ反発陽線がまだ確定しておらず、売り圧力が残存")
            if ind.volume_ratio < 1.25:
                cons.append(f"出来高倍率が {ind.volume_ratio:.2f}x と少なく、大口の買い支え（セリクラ）が未確認")

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
                        f"🤔 判断伺い: 安値圏だが{regime}期のため底打ち見極め局面 "
                        f"(RSI={ind.rsi_14:.1f}, Vol={ind.volume_ratio:.2f}x)"
                    ),
                    indicators=ind,
                    pros=pros,
                    cons=cons,
                )

        # 条件未達の場合は見送り（HOLD）
        hold_reason = (
            f"優位性待機中 [{regime}] (Price=${price:.4f}, RSI={ind.rsi_14:.1f}, "
            f"Slope={ind.ema_50_slope_pct:+.2f}%, Vol={ind.volume_ratio:.2f}x)"
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
