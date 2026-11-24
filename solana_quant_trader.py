"""
Solana 凄腕クオンツ自動売買ボット（1,000円少額スタート ＆ 高流動性主要銘柄特化・100点完成版）
対象銘柄: SOL, JUP, RAY, BONK (対USDC決済)

【使い方】
1. 現在の資産・ポジション状況の確認:
   python3 solana_quant_trader.py --status

2. リアルタイム市場データを取得して1回だけシグナル判定・仮想売買テストを実行:
   python3 solana_quant_trader.py --once --notify

3. GitHub Actions クラウド長時間連続稼働モード（3.5時間連続で3分おきに監視し、30分おきにTelegramへ定期レポート送信）:
   python3 solana_quant_trader.py --cloud-loop --duration-minutes 210 --notify-interval-minutes 30
"""

import argparse
import os
import subprocess
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
    """環境変数にTelegram設定がある場合のみ、エントリー・決済・定期レポート通知を送信する。"""
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


def sync_portfolio_state_to_git() -> None:
    """
    GitHub Actions上で稼働中、ポジションや資産状態(data/portfolio_state.json)に変更があれば即座にGitへ自動保存する。
    ※コミットメッセージに [skip ci] を入れるとGitHubの定期タイマー(schedule)まで抑制される仕様があるため、[skip ci] は付けない。
    """
    if os.getenv("GITHUB_ACTIONS") != "true":
        return

    try:
        subprocess.run(
            ["git", "config", "user.name", "github-actions[bot]"],
            cwd=str(PROJECT_ROOT),
            check=False,
        )
        subprocess.run(
            ["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"],
            cwd=str(PROJECT_ROOT),
            check=False,
        )
        subprocess.run(
            ["git", "add", "data/portfolio_state.json"],
            cwd=str(PROJECT_ROOT),
            check=False,
        )
        diff_res = subprocess.run(
            ["git", "diff", "--staged", "--quiet"],
            cwd=str(PROJECT_ROOT),
            check=False,
        )
        if diff_res.returncode != 0:
            subprocess.run(
                ["git", "commit", "-m", "chore: update portfolio state"],
                cwd=str(PROJECT_ROOT),
                check=False,
            )
            subprocess.run(
                ["git", "pull", "--rebase", "origin", "main"],
                cwd=str(PROJECT_ROOT),
                check=False,
            )
            subprocess.run(
                ["git", "push", "origin", "main"],
                cwd=str(PROJECT_ROOT),
                check=False,
            )
            print("💾 クラウド上のポートフォリオ状態をGitHubへ即時保存しました。")
    except Exception as exc:
        print(f"【Git自動保存警告】状態保存中に軽微なエラーが発生しました: {exc}")


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
                        sync_portfolio_state_to_git()

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
                sync_portfolio_state_to_git()

        self.print_status_dashboard(latest_prices)

        if force_telegram_report:
            state = self.risk_manager.state
            total_eq = self.risk_manager.get_total_equity_usdc(latest_prices)
            pnl_pct = ((total_eq - state.initial_usdc) / state.initial_usdc * 100.0) if state.initial_usdc > 0 else 0.0
            pos_summary = "なし（チャンス待機中）"
            if state.open_positions:
                pos_items = []
                for sym, pos in state.open_positions.items():
                    cp = latest_prices.get(sym, pos.entry_price)
                    upct = ((cp - pos.entry_price) / pos.entry_price) * 100.0
                    pos_items.append(f"{sym} ({upct:+.2f}%)")
                pos_summary = ", ".join(pos_items)

            summary_msg = (
                f"📊 <b>【Solana クオンツ定期診断レポート ({self.mode})】</b>\n"
                f"💰 総資産: <b>${total_eq:.4f} USDC ({pnl_pct:+.2f}%)</b>\n"
                f"🏆 成績: {state.total_trades}戦 {state.winning_trades}勝 | 保有中: {pos_summary}\n\n"
                + ("\n".join(report_lines) if report_lines else "※市場データ取得待機中")
            )
            send_telegram_alert(summary_msg)
            print("📲 Telegramに診断レポートを送信しました。")

    def run_cloud_session(
        self,
        duration_minutes: int = 210,
        scan_interval_sec: int = 180,
        notify_interval_min: int = 30,
    ) -> None:
        """
        GitHub Actionsの無料実行枠（最大6時間）をフル活用し、1回の起動で数時間連続して監視を続けるモード。
        GitHubのcronタイマー遅延の影響を受けず、3分おきに市場スキャン＆30分おきにTelegram定期レポートを確実に送信する。
        """
        print(
            f"☁️ 【クラウド連続稼働セッション開始】 稼働時間: {duration_minutes}分 | "
            f"スキャン間隔: {scan_interval_sec}秒 | Telegram定期レポート間隔: {notify_interval_min}分"
        )
        start_ts = time.time()
        end_ts = start_ts + (duration_minutes * 60)
        last_notify_ts = 0.0
        notify_interval_sec = notify_interval_min * 60

        cycle_count = 0
        while time.time() < end_ts:
            cycle_count += 1
            now_ts = time.time()
            should_notify = (now_ts - last_notify_ts) >= notify_interval_sec
            try:
                self.run_single_cycle(force_telegram_report=should_notify)
                if should_notify:
                    last_notify_ts = time.time()
                sync_portfolio_state_to_git()
            except Exception as exc:
                print(f"【クラウドサイクル警告】サイクル #{cycle_count} で例外発生（次回自動復帰）: {exc}")

            remaining_sec = end_ts - time.time()
            if remaining_sec <= 15:
                break
            sleep_sec = min(scan_interval_sec, int(remaining_sec))
            print(f"⏳ 次のスキャンまで {sleep_sec} 秒待機中 (残りセッション時間: {int(remaining_sec // 60)}分)...")
            time.sleep(sleep_sec)

        print("✅ クラウド連続稼働セッションが正常に完了しました。次のジョブへバトンタッチします。")

    def run_forever(self) -> None:
        """指定秒数ごとにマーケットを監視し続けるローカル用メインループ。"""
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
        help="売買がなくても現在のスキャン結果と資産レポートをTelegramに送信する",
    )
    parser.add_argument(
        "--cloud-loop",
        action="store_true",
        help="GitHub Actions上で数時間連続して監視＆定期Telegram通知を行うクラウド常駐モード",
    )
    parser.add_argument(
        "--duration-minutes",
        type=int,
        default=210,
        help="--cloud-loop 時の連続稼働時間（分、デフォルト210分＝3.5時間）",
    )
    parser.add_argument(
        "--notify-interval-minutes",
        type=int,
        default=30,
        help="--cloud-loop 時のTelegram定期診断レポート送信間隔（分、デフォルト30分）",
    )
    args = parser.parse_args()

    bot = SolanaQuantBot(mode=args.mode.upper())
    if args.status:
        bot.print_status_dashboard()
    elif args.cloud_loop:
        bot.run_cloud_session(
            duration_minutes=args.duration_minutes,
            scan_interval_sec=180,
            notify_interval_min=args.notify_interval_minutes,
        )
    elif args.once:
        bot.run_single_cycle(force_telegram_report=args.notify)
    else:
        bot.run_forever()


if __name__ == "__main__":
    try:
        main()
    except Exception as fatal_exc:
        import traceback
        tb_str = traceback.format_exc()
        print(f"🚨 【致命的エラー】ボット実行中に予期せぬエラーが発生しました:\n{tb_str}")
        send_telegram_alert(f"⚠️ <b>【Solanaボット実行エラー通知】</b>\n<pre>{str(fatal_exc)[:300]}</pre>")
        sys.exit(1)
