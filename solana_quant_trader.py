"""
Solana 凄腕クオンツ自動売買ボット（AIコパイロット・勝敗分析・Telegram対話型判断伺い搭載 100点完成版）
対象銘柄: SOL, JUP, RAY, BONK (対USDC決済)

【主な機能】
1. 鉄板シグナル(スコア76点以上): 自動で即エントリーし、買い根拠をTelegramへ通知
2. 微妙なボーダーライン(スコア60〜75点): 「プラス材料」と「懸念材料」を提示し、Telegramの【🟢買う / ⚪見送る】ボタンでユーザーの判断を仰ぐ
3. 決済時＆定期レポートの勝敗分析: 「なぜ勝てたか／なぜ負けたか」のクオンツ分析と戦略・銘柄別勝率をTelegramへ通知
"""

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional
import requests

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import SETTINGS
from src.data.market_data import MarketSnapshot, SolanaMarketDataClient
from src.execution.jupiter_executor import JupiterSwapExecutor
from src.risk.risk_manager import RiskAndPortfolioManager
from src.strategy.signal_engine import QuantSignalEngine, TradeSignal


def send_telegram_alert(message: str, reply_markup: Optional[dict] = None) -> Optional[int]:
    """TelegramへHTMLメッセージを送信し、送信成功時は message_id を返す。"""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not bot_token or not chat_id or bot_token == "your_telegram_bot_token_here":
        return None

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload: dict = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        resp = requests.post(url, json=payload, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        return data.get("result", {}).get("message_id")
    except Exception as exc:
        print(f"【Telegram通知警告】送信失敗: {exc}")
        return None


def ask_user_decision_via_telegram(
    signal: TradeSignal, order_usdc: float, timeout_sec: int = 90
) -> bool:
    """
    微妙なボーダーラインシグナル（スコア60〜75点）の際に、
    Telegramへ「プラス判断材料」「懸念材料」と【🟢買う / ⚪見送る】ボタンを送信し、
    ユーザーのワンタップ指示を最大 timeout_sec 秒待機する。
    """
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not bot_token or not chat_id or bot_token == "your_telegram_bot_token_here":
        return False

    base_url = f"https://api.telegram.org/bot{bot_token}"

    # 既存の古い未処理アップデートIDをスキップするために最新update_idを取得
    last_update_id = 0
    try:
        init_resp = requests.get(f"{base_url}/getUpdates", params={"timeout": 1}, timeout=5).json()
        results = init_resp.get("result") or []
        if results:
            last_update_id = results[-1]["update_id"]
    except Exception:
        pass

    pros_text = "\n".join(f"  ✅ {p}" for p in signal.pros) if signal.pros else "  ・特になし"
    cons_text = "\n".join(f"  ⚠️ {c}" for c in signal.cons) if signal.cons else "  ・特になし"

    question_text = (
        f"🤔 <b>【判断伺い：微妙なボーダーライン検出】</b>\n"
        f"銘柄: <b>{signal.symbol}</b> (現在価格: ${signal.entry_price:.5f})\n"
        f"候補戦略: {signal.strategy_name} | 確信度: <b>{signal.confidence_score}/100点</b>\n"
        f"想定投入額: ${order_usdc:.2f} USDC\n"
        f"目標利確(TP): ${signal.take_profit_price:.5f} (+{signal.take_profit_pct:.2f}%)\n"
        f"損切設定(SL): ${signal.stop_loss_price:.5f} (-{signal.stop_loss_pct:.2f}%)\n"
        f"実質リスクリワード比: <b>1 : {signal.net_risk_reward_ratio:.2f}</b>\n\n"
        f"📊 <b>【プラス材料（買い根拠）】</b>\n{pros_text}\n\n"
        f"🔍 <b>【懸念材料（微妙な理由）】</b>\n{cons_text}\n\n"
        f"👇 <b>{timeout_sec}秒以内に下のボタンで指示してください</b>\n"
        f"<i>（※時間内に応答がない場合は安全のため自動で見送ります）</i>"
    )

    reply_markup = {
        "inline_keyboard": [
            [
                {"text": f"🟢 買う ({signal.symbol})", "callback_data": f"BUY:{signal.symbol}"},
                {"text": "⚪ 今回は見送る", "callback_data": f"SKIP:{signal.symbol}"},
            ]
        ]
    }

    msg_id = send_telegram_alert(question_text, reply_markup=reply_markup)
    if not msg_id:
        return False

    print(f"📲 Telegramへ {signal.symbol} の判断伺い（ボタン付き）を送信しました。ユーザーの応答を最大{timeout_sec}秒待機中...")
    start_ts = time.time()
    decision: Optional[bool] = None

    while time.time() - start_ts < timeout_sec:
        try:
            poll_resp = requests.get(
                f"{base_url}/getUpdates",
                params={"offset": last_update_id + 1, "timeout": 5},
                timeout=10,
            ).json()
            for upd in poll_resp.get("result") or []:
                last_update_id = upd["update_id"]

                # 1. インラインボタン（callback_query）が押された場合
                cb = upd.get("callback_query")
                if cb:
                    cb_id = cb.get("id")
                    cb_data = cb.get("data", "")
                    if cb_data == f"BUY:{signal.symbol}":
                        requests.post(
                            f"{base_url}/answerCallbackQuery",
                            json={"callback_query_id": cb_id, "text": "🟢 購入指示を受け付けました！"},
                            timeout=5,
                        )
                        decision = True
                        break
                    if cb_data == f"SKIP:{signal.symbol}":
                        requests.post(
                            f"{base_url}/answerCallbackQuery",
                            json={"callback_query_id": cb_id, "text": "⚪ 見送り指示を受け付けました。"},
                            timeout=5,
                        )
                        decision = False
                        break

                # 2. テキストメッセージで「買う」「buy」「見送る」「skip」と返信された場合も対応
                msg = upd.get("message") or {}
                if str(msg.get("chat", {}).get("id")) == str(chat_id):
                    txt = (msg.get("text") or "").strip().lower()
                    if txt in ("buy", "買う", "yes", "ok", "購入"):
                        decision = True
                        break
                    if txt in ("skip", "no", "見送る", "見送り", "パス"):
                        decision = False
                        break

            if decision is not None:
                break
        except Exception:
            time.sleep(2)

    # ボタンを消去して結果をメッセージに追記
    try:
        requests.post(
            f"{base_url}/editMessageReplyMarkup",
            json={"chat_id": chat_id, "message_id": msg_id, "reply_markup": {"inline_keyboard": []}},
            timeout=5,
        )
    except Exception:
        pass

    if decision is True:
        print(f"🟢 ユーザーから【購入(BUY)】の指示を受信しました！ {signal.symbol} をエントリーします。")
        return True
    if decision is False:
        send_telegram_alert(f"⏭️ <b>【見送り完了】</b> あなたの指示により {signal.symbol} のエントリーを見送りました。")
        print(f"⚪ ユーザーから【見送り(SKIP)】の指示を受信しました。")
        return False

    send_telegram_alert(
        f"⏱️ <b>【自動見送り】</b> {timeout_sec}秒以内に応答がなかったため、安全を優先して {signal.symbol} は見送りました。"
    )
    print(f"⏱️ 応答タイムアウト（{timeout_sec}秒）のため、安全に見送りました。")
    return False


def sync_portfolio_state_to_git() -> None:
    """
    GitHub Actions上で稼働中、ポジションや資産状態(data/portfolio_state.json)に変更があれば即座にGitへ自動保存する。
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
        # 同じ微妙銘柄で何度も連続質問しないためのクールダウン記録（銘柄 -> 最終質問時刻）
        self.last_asked_by_symbol: Dict[str, float] = {}

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

    def _execute_and_record_entry(self, signal: TradeSignal, order_usdc: float, is_user_approved: bool = False) -> None:
        """選出またはユーザー承認されたシグナルの購入を執行し、判断材料付きでTelegramへ通知する。"""
        bought_tokens = self.executor.execute_buy_with_usdc(
            signal.symbol, order_usdc, signal.entry_price
        )
        if bought_tokens and bought_tokens > 0:
            self.risk_manager.record_open_position(
                symbol=signal.symbol,
                mint_address=signal.mint_address,
                strategy_name=signal.strategy_name,
                entry_price=signal.entry_price,
                stop_loss_price=signal.stop_loss_price,
                take_profit_price=signal.take_profit_price,
                invested_usdc=order_usdc,
                token_amount=bought_tokens,
            )
            header = (
                f"🚀 <b>【ユーザー承認エントリー完了 ({self.mode})】</b>"
                if is_user_approved
                else f"🚀 <b>【鉄板シグナル自動エントリー完了 ({self.mode})】</b>"
            )
            pros_lines = "\n".join(f"  ✅ {p}" for p in signal.pros) if signal.pros else f"  ✅ {signal.reason}"
            alert_msg = (
                f"{header}\n"
                f"銘柄: <b>{signal.symbol}</b> | 戦略: {signal.strategy_name} (スコア {signal.confidence_score}/100)\n"
                f"取得単価: ${signal.entry_price:.5f} | 投入額: ${order_usdc:.2f} USDC\n"
                f"利確目標(TP): ${signal.take_profit_price:.5f} (+{signal.take_profit_pct:.2f}%)\n"
                f"損切設定(SL): ${signal.stop_loss_price:.5f} (-{signal.stop_loss_pct:.2f}%)\n\n"
                f"📊 <b>【エントリー判断根拠】</b>\n{pros_lines}"
            )
            send_telegram_alert(alert_msg)
            sync_portfolio_state_to_git()

    def run_single_cycle(self, force_telegram_report: bool = False) -> None:
        """
        全監視銘柄（SOL, JUP, RAY, BONK）の最新データを取得し、
        1. 保有ポジションの決済（利確・損切り・トレーリング・タイムストップ）＆勝因・敗因分析の通知
        2. 新規エントリーの厳選（鉄板BUYは即実行、微妙なASKはTelegramで判断材料を出して指示を仰ぐ）
        を1サイクル実行する。
        """
        snapshots: Dict[str, MarketSnapshot] = {}
        latest_prices: Dict[str, float] = {}

        print(f"\n🔍 [{datetime.now().strftime('%H:%M:%S')}] 主要銘柄のオンチェーン市場データをスキャン中...")
        for symbol in SETTINGS.target_symbols:
            snap = self.market_client.fetch_market_snapshot(symbol)
            if snap:
                snapshots[symbol] = snap
                latest_prices[symbol] = snap.price_usd

        # 1. 保有中ポジションの監視とエグジット判定（勝因・敗因アナリシス付き）
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
                        result_badge = "🎉 勝ちトレード" if closed.net_pnl_usdc > 0 else "🛡️ 損切り・撤退トレード"
                        msg = (
                            f"✅ <b>【ポジション決済完了 ({self.mode}) - {result_badge}】</b>\n"
                            f"銘柄: <b>{closed.symbol}</b> ({closed.strategy_name})\n"
                            f"価格: ${closed.entry_price:.5f} -> ${closed.exit_price:.5f} (最大含み益: +{closed.max_favorable_pct:.2f}%)\n"
                            f"理由: {closed.exit_reason}\n"
                            f"純損益: <b>${closed.net_pnl_usdc:+.4f} USDC ({closed.net_pnl_pct:+.2f}%)</b>\n\n"
                            f"🧠 <b>{closed.analysis_comment}</b>\n\n"
                            f"{self.risk_manager.get_trade_analytics_summary()}"
                        )
                        print(
                            f"💰 【決済確定】{closed.symbol} 純損益: "
                            f"${closed.net_pnl_usdc:+.4f} USDC ({closed.net_pnl_pct:+.2f}%)\n"
                            f"   └ {closed.analysis_comment}"
                        )
                        send_telegram_alert(msg)
                        sync_portfolio_state_to_git()

        # 2. 各銘柄のテクニカル指標とシグナルを評価・一覧表示
        sol_1h_change = snapshots["SOL"].price_change_1h_pct if "SOL" in snapshots else 0.0
        buy_candidates: list[TradeSignal] = []
        ask_candidates: list[TradeSignal] = []
        report_lines: list[str] = []

        print("\n📈 【クオンツ指標・シグナル判定結果】")
        for symbol, snap in snapshots.items():
            signal = self.signal_engine.evaluate_snapshot(snap, sol_1h_change_pct=sol_1h_change)
            ind = signal.indicators
            if signal.action == "BUY":
                status_icon = "🟢 鉄板BUY"
            elif signal.action == "ASK":
                status_icon = "🤔 判断伺い"
            else:
                status_icon = "⚪ 待機(HOLD)"

            print(
                f"  [{symbol:<4}] {status_icon} | 価格: ${ind.current_price:<10.5f} | "
                f"RSI(14): {ind.rsi_14:5.1f} | 出来高倍率: {ind.volume_ratio:4.2f}x | "
                f"ATR: {ind.atr_pct:4.2f}% | {signal.reason}"
            )
            report_lines.append(
                f"・<b>{symbol}</b>: ${ind.current_price:.4f} | RSI:{ind.rsi_14:.1f} | Vol:{ind.volume_ratio:.1f}x -> {status_icon}"
            )
            if symbol not in self.risk_manager.state.open_positions:
                if signal.action == "BUY":
                    buy_candidates.append(signal)
                elif signal.action == "ASK":
                    ask_candidates.append(signal)

        # 3. 新規エントリーの審査・執行
        can_enter, guard_reason = self.risk_manager.can_open_new_position(latest_prices)
        if not can_enter:
            print(f"\n🛡️ 【リスク管理フィルター】新規エントリー見送り: {guard_reason}")
        elif buy_candidates:
            # 鉄板シグナル（スコア76点以上）があれば迷わず即エントリー
            buy_candidates.sort(
                key=lambda s: (s.confidence_score, s.net_risk_reward_ratio),
                reverse=True,
            )
            best_signal = buy_candidates[0]
            order_usdc = self.risk_manager.calculate_order_size_usdc()
            print(
                f"\n🎯 【鉄板エントリー選出】{best_signal.symbol} "
                f"(戦略: {best_signal.strategy_name}, スコア: {best_signal.confidence_score}/100)"
            )
            self._execute_and_record_entry(best_signal, order_usdc, is_user_approved=False)

        elif ask_candidates:
            # 鉄板はないが「微妙（スコア60〜75点）」の候補がある場合、判断材料を出してTelegramでユーザーに聞く
            ask_candidates.sort(
                key=lambda s: (s.confidence_score, s.net_risk_reward_ratio),
                reverse=True,
            )
            best_ask = ask_candidates[0]
            last_asked = self.last_asked_by_symbol.get(best_ask.symbol, 0.0)
            # 同じ銘柄について30分以内に質問済みならスパム防止のためスキップ
            if time.time() - last_asked >= 1800:
                self.last_asked_by_symbol[best_ask.symbol] = time.time()
                order_usdc = self.risk_manager.calculate_order_size_usdc()
                user_approved = ask_user_decision_via_telegram(best_ask, order_usdc, timeout_sec=90)
                if user_approved:
                    self._execute_and_record_entry(best_ask, order_usdc, is_user_approved=True)

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

            analytics_text = self.risk_manager.get_trade_analytics_summary()
            summary_msg = (
                f"📊 <b>【Solana クオンツ定期診断レポート ({self.mode})】</b>\n"
                f"💰 総資産: <b>${total_eq:.4f} USDC ({pnl_pct:+.2f}%)</b>\n"
                f"🏆 成績: {state.total_trades}戦 {state.winning_trades}勝 | 保有中: {pos_summary}\n\n"
                + ("\n".join(report_lines) if report_lines else "※市場データ取得待機中")
                + f"\n\n{analytics_text}"
            )
            send_telegram_alert(summary_msg)
            print("📲 Telegramに勝敗分析付き診断レポートを送信しました。")

    def run_cloud_session(
        self,
        duration_minutes: int = 210,
        scan_interval_sec: int = 180,
        notify_interval_min: int = 30,
    ) -> None:
        """
        GitHub Actionsの無料実行枠（最大6時間）をフル活用し、1回の起動で数時間連続して監視を続けるモード。
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
        help="売買がなくても現在のスキャン結果と勝敗分析レポートをTelegramに送信する",
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
