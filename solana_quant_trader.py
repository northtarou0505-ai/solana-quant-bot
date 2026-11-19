"""
Solana 凄腕クオンツ自動売買ボット（1,000円少額スタート ＆ 高流動性主要銘柄特化・100点完成版）
対象銘柄: SOL, JUP, RAY, BONK (対USDC決済)

【使い方】
1. 現在の資産・ポジション状況の確認:
   python3 solana_quant_trader.py --status

2. リアルタイム市場データを取得して1回だけシグナル判定・仮想売買テストを実行:
   python3 solana_quant_trader.py --once

3. 24時間自動監視ループの起動（デフォルトは安全なPAPER仮想売買モード）:
   python3 solana_quant_trader.py

4. Phantomサブウォレットを使った本番稼働（十分にPAPERで勝率確認後）:
   python3 solana_quant_trader.py --mode live
"""

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict
import requests

# どのディレクトリから実行しても確実にパッケージを解決できるようにパスを追加
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import SETTINGS
from src.data.market_data import MarketSnapshot, SolanaMarketDataClient
from src.execution.jupiter_executor import JupiterSwapExecutor
from src.risk.risk_manager import RiskAndPortfolioManager
from src.strategy.signal_engine import QuantSignalEngine, TradeSignal


def send_telegram_alert(message: str) -> None:
    """環境変数にTelegram設定がある場合のみ、エントリー・決済通知を送信する。"""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not bot_token or not chat_id or bot_token == "your_telegram_bot_token_here":
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        requests.post(
            url,
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=8,
        )
    except Exception as exc:
        print(f"【Telegram通知警告】送信失敗: {exc}")


class SolanaQuantBot:
    """1,000円予算から主要銘柄を手堅く回すSolanaクオンツ自動売買ボット本体。"""

    def __init__(self, mode: str = SETTINGS.trading_mode):
        """各モジュール（データ取得、シグナル判定、リスク管理、執行エンジン）を初期化する。"""
        self.mode = mode.upper()
        self.market_client = SolanaMarketDataClient()
        self.signal_engine = QuantSignalEngine()
        self.risk_manager = RiskAndPortfolioManager()
        self.executor = JupiterSwapExecutor(mode=self.mode)

    def print_status_dashboard(self, latest_prices: Dict[str, float] | None = None) -> None:
        """現在の資産状況・勝率・保有ポジションを見やすく表示する。"""
        state = self.risk_manager.state
        total_equity = self.risk_manager.get_total_equity_usdc(latest_prices)
        pnl_pct = (
            ((total_equity - state.initial_usdc) / state.initial_usdc) * 100.0
            if state.initial_usdc > 0
            else 0.0
        )
        win_rate = (
            (state.winning_trades / state.total_trades) * 100.0
            if state.total_trades > 0
            else 0.0
        )

        print("\n" + "=" * 68)
        print(
            f"📊 【Solana クオンツ自動売買ダッシュボード】 モード: {self.mode} "
            f"| {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        print("-" * 68)
        print(
            f"💰 総資産評価額: ${total_equity:.4f} USDC (初期予算: ${state.initial_usdc:.2f} USDC / 約{SETTINGS.initial_budget_jpy:.0f}円)"
        )
        print(
            f"📈 累計純損益  : ${total_equity - state.initial_usdc:+.4f} USDC ({pnl_pct:+.2f}%) "
            f"| 現金余力: ${state.available_usdc:.4f} USDC"
        )
        print(
            f"🏆 通算成績    : {state.total_trades}戦 {state.winning_trades}勝 "
            f"(勝率: {win_rate:.1f}%) | 現在の連敗数: {state.consecutive_losses}"
        )

        if state.open_positions:
            print("-" * 68)
            print("📂 【現在保有中のポジション】")
            for sym, pos in state.open_positions.items():
                cur_price = (latest_prices or {}).get(sym, pos.entry_price)
                unrealized_pct = ((cur_price - pos.entry_price) / pos.entry_price) * 100.0
                trail_tag = " [🔒トレーリング稼働中]" if pos.trailing_active else ""
                print(
                    f"  ・{sym}: 数量={pos.token_amount:.5f} | 取得=${pos.entry_price:.4f} -> 現在=${cur_price:.4f} "
                    f"({unrealized_pct:+.2f}%){trail_tag}\n"
                    f"    └ 損切り(SL)=${pos.stop_loss_price:.4f} | 利確(TP)=${pos.take_profit_price:.4f} "
                    f"| 戦略={pos.strategy_name}"
                )
        else:
            print("📂 保有ポジション: なし（厳選シグナル待機中）")
        print("=" * 68)

    def run_single_cycle(self, force_telegram_report: bool = False) -> None:
        """
        全監視銘柄（SOL, JUP, RAY, BONK）の最新データを取得し、
        1. 保有ポジションの決済（利確・損切り・トレーリング・タイムストップ）判定
        2. 新規エントリーの厳選・執行
        を1サイクル実行する。
        force_telegram_report=True の場合は売買がなくても現在の市場診断レポートをTelegramに送信する。
        """
        snapshots: Dict[str, MarketSnapshot] = {}
        latest_prices: Dict[str, float] = {}

        print(f"\n🔍 [{datetime.now().strftime('%H:%M:%S')}] 主要銘柄のオンチェーン市場データをスキャン中...")
        for symbol in SETTINGS.target_symbols:
            snap = self.market_client.fetch_market_snapshot(symbol)
            if snap:
                snapshots[symbol] = snap
                latest_prices[symbol] = snap.price_usd

        # 1. 保有中ポジションの監視とエグジット判定
        open_symbols = list(self.risk_manager.state.open_positions.keys())
        for sym in open_symbols:
            cur_price = latest_prices.get(sym)
            if not cur_price:
                continue

            pos = self.risk_manager.state.open_positions[sym]
            snap = snapshots.get(sym)
            # GitHub Actions 10分間隔実行対策：エントリー時刻以降の5分足高値を抽出してトレーリングストップに反映
            recent_high = cur_price
            if snap and snap.candles_5m:
                highs_after_entry = [
                    c.high for c in snap.candles_5m if c.timestamp >= int(pos.opened_at_ts)
                ]
                if highs_after_entry:
                    recent_high = max(cur_price, max(highs_after_entry))

            should_exit, exit_reason = self.risk_manager.check_exit_conditions(
                sym, cur_price, recent_high_since_open=recent_high
            )
            if should_exit:
                print(f"⚡ 【決済シグナル発動】{sym}: {exit_reason}")
                returned_usdc = self.executor.execute_sell_to_usdc(sym, pos.token_amount, cur_price)
                if returned_usdc is not None:
                    closed = self.risk_manager.record_close_position(
                        sym, cur_price, returned_usdc, exit_reason
                    )
                    if closed:
                        msg = (
                            f"✅ <b>【ポジション決済完了 ({self.mode})】</b>\n"
                            f"銘柄: {closed.symbol} ({closed.strategy_name})\n"
                            f"理由: {closed.exit_reason}\n"
                            f"損益: <b>${closed.net_pnl_usdc:+.4f} USDC ({closed.net_pnl_pct:+.2f}%)</b>"
                        )
                        print(
                            f"💰 【決済確定】{closed.symbol} 純損益: "
                            f"${closed.net_pnl_usdc:+.4f} USDC ({closed.net_pnl_pct:+.2f}%)"
                        )
                        send_telegram_alert(msg)

        # 2. 各銘柄のテクニカル指標とシグナルを評価・一覧表示（SOLの地合いも連動チェック）
        sol_1h_change = snapshots["SOL"].price_change_1h_pct if "SOL" in snapshots else 0.0
        buy_candidates: list[TradeSignal] = []
        report_lines: list[str] = []
        print("\n📈 【クオンツ指標・シグナル判定結果】")
        for symbol, snap in snapshots.items():
            signal = self.signal_engine.evaluate_snapshot(snap, sol_1h_change_pct=sol_1h_change)
            ind = signal.indicators
            status_icon = "🟢 BUY候補" if signal.action == "BUY" else "⚪ 待機(HOLD)"
            print(
                f"  [{symbol:<4}] {status_icon} | 価格: ${ind.current_price:<10.5f} | "
                f"RSI(14): {ind.rsi_14:5.1f} | 出来高倍率: {ind.volume_ratio:4.2f}x | "
                f"ATR: {ind.atr_pct:4.2f}% | {signal.reason}"
            )
            report_lines.append(
                f"・<b>{symbol}</b>: ${ind.current_price:.4f} | RSI:{ind.rsi_14:.1f} | Vol:{ind.volume_ratio:.1f}x -> {status_icon}"
            )
            if signal.action == "BUY" and symbol not in self.risk_manager.state.open_positions:
                buy_candidates.append(signal)

        # 3. 新規エントリーの審査・執行（1,000円予算のため最も確信度スコアが高い1銘柄に集中）
        can_enter, guard_reason = self.risk_manager.can_open_new_position(latest_prices)
        if not can_enter:
            print(f"\n🛡️ 【リスク管理フィルター】新規エントリー見送り: {guard_reason}")
        elif buy_candidates:
            buy_candidates.sort(
                key=lambda s: (s.confidence_score, s.net_risk_reward_ratio),
                reverse=True,
            )
            best_signal = buy_candidates[0]
            order_usdc = self.risk_manager.calculate_order_size_usdc()

            print(
                f"\n🎯 【最良エントリー選出】{best_signal.symbol} "
                f"(戦略: {best_signal.strategy_name}, スコア: {best_signal.confidence_score}/100)\n"
                f"   投入額: ${order_usdc:.2f} USDC | 目標TP: ${best_signal.take_profit_price:.5f} (+{best_signal.take_profit_pct:.2f}%) "
                f"| 損切SL: ${best_signal.stop_loss_price:.5f} (-{best_signal.stop_loss_pct:.2f}%)"
            )

            bought_tokens = self.executor.execute_buy_with_usdc(
                best_signal.symbol, order_usdc, best_signal.entry_price
            )
            if bought_tokens and bought_tokens > 0:
                self.risk_manager.record_open_position(
                    symbol=best_signal.symbol,
                    mint_address=best_signal.mint_address,
                    strategy_name=best_signal.strategy_name,
                    entry_price=best_signal.entry_price,
                    stop_loss_price=best_signal.stop_loss_price,
                    take_profit_price=best_signal.take_profit_price,
                    invested_usdc=order_usdc,
                    token_amount=bought_tokens,
                )
                alert_msg = (
                    f"🚀 <b>【新規エントリー完了 ({self.mode})】</b>\n"
                    f"銘柄: <b>{best_signal.symbol}</b>\n"
                    f"戦略: {best_signal.strategy_name} (スコア {best_signal.confidence_score})\n"
                    f"取得単価: ${best_signal.entry_price:.5f}\n"
                    f"投入額: ${order_usdc:.2f} USDC\n"
                    f"利確目標(TP): ${best_signal.take_profit_price:.5f} (+{best_signal.take_profit_pct:.2f}%)\n"
                    f"損切設定(SL): ${best_signal.stop_loss_price:.5f} (-{best_signal.stop_loss_pct:.2f}%)"
                )
                send_telegram_alert(alert_msg)

        self.print_status_dashboard(latest_prices)

        if force_telegram_report:
            state = self.risk_manager.state
            total_eq = self.risk_manager.get_total_equity_usdc(latest_prices)
            pnl_pct = ((total_eq - state.initial_usdc) / state.initial_usdc * 100.0) if state.initial_usdc > 0 else 0.0
            summary_msg = (
                f"📊 <b>【Solana クオンツ定期診断レポート ({self.mode})】</b>\n"
                f"💰 総資産: <b>${total_eq:.4f} USDC ({pnl_pct:+.2f}%)</b>\n"
                f"🏆 成績: {state.total_trades}戦 {state.winning_trades}勝 | 保有中: {len(state.open_positions)}銘柄\n\n"
                + ("\n".join(report_lines) if report_lines else "※市場データ取得待機中")
            )
            send_telegram_alert(summary_msg)
            print("📲 Telegramに診断レポートを送信しました。")

    def run_forever(self) -> None:
        """指定秒数ごとにマーケットを監視し続けるメインループ。"""
        print(
            f"🚀 Solana クオンツ自動売買システムを起動しました "
            f"(モード: {self.mode}, 監視銘柄: {', '.join(SETTINGS.target_symbols)})"
        )
        while True:
            try:
                self.run_single_cycle()
                print(f"⏳ 次のスキャンまで {SETTINGS.scan_interval_seconds} 秒待機します (Ctrl+Cで安全に停止)...")
                time.sleep(SETTINGS.scan_interval_seconds)
            except KeyboardInterrupt:
                print("\n🛑 ユーザー操作により自動売買ボットを安全に停止しました。")
                break
            except Exception as exc:
                print(f"【システム警告】サイクル実行中に例外が発生しました（30秒後に自動復帰します）: {exc}")
                time.sleep(30)


def main() -> None:
    """コマンドライン引数を解析してボットを起動する。"""
    parser = argparse.ArgumentParser(description="Solana 1,000円スタート対応 クオンツ自動売買ボット")
    parser.add_argument(
        "--mode",
        choices=["paper", "live"],
        default=SETTINGS.trading_mode.lower(),
        help="取引モード: paper (仮想売買・デフォルト) または live (実資金取引)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="1サイクルだけリアルタイム市場分析・シグナル判定を実行して終了する",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="現在のポートフォリオ状況・勝率・保有ポジションを表示して終了する",
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        help="売買がなくても現在のスキャン結果と資産レポートをTelegramにテスト送信する",
    )
    args = parser.parse_args()

    bot = SolanaQuantBot(mode=args.mode.upper())
    if args.status:
        bot.print_status_dashboard()
    elif args.once:
        bot.run_single_cycle(force_telegram_report=args.notify)
    else:
        bot.run_forever()


if __name__ == "__main__":
    main()
