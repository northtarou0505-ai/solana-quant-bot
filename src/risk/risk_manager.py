"""
1,000円少額資金を守り抜くための資金管理・ポジション管理・サーキットブレーカーモジュール（100点完成版）。
- 日次ドローダウン上限（5%）および連敗制限（3連敗）による自動停止
- 含み益発生時のオート・トレーリングストップ（GitHub Actionsの10分間隔実行でもローソク足高値を検知して建値ロック）
- 長時間ヨコヨコ停滞時のタイムストップ（塩漬けによる資金ロック防止）
- ポートフォリオ状態のJSON永続化
"""

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from config.settings import SETTINGS

# プロジェクトルートディレクトリ（どのフォルダから実行しても確実にdataフォルダを参照できるようにする）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_STATE_FILE = str(PROJECT_ROOT / "data" / "portfolio_state.json")


@dataclass
class Position:
    """保有中のポジション情報。"""
    symbol: str
    mint_address: str
    strategy_name: str
    entry_price: float
    highest_price: float
    stop_loss_price: float
    take_profit_price: float
    invested_usdc: float
    token_amount: float
    opened_at: str
    opened_at_ts: float = field(default_factory=time.time)
    trailing_active: bool = False


@dataclass
class ClosedTrade:
    """決済済みトレードの履歴レコード。"""
    symbol: str
    strategy_name: str
    entry_price: float
    exit_price: float
    invested_usdc: float
    net_pnl_usdc: float
    net_pnl_pct: float
    exit_reason: str
    closed_at: str


@dataclass
class PortfolioState:
    """ボット全体の資産・ポジション・リスク管理状態。"""
    mode: str = SETTINGS.trading_mode
    initial_usdc: float = SETTINGS.initial_paper_usdc
    available_usdc: float = SETTINGS.initial_paper_usdc
    daily_start_equity_usdc: float = SETTINGS.initial_paper_usdc
    current_date: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d"))
    consecutive_losses: int = 0
    cooldown_until_ts: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    total_net_pnl_usdc: float = 0.0
    open_positions: Dict[str, Position] = field(default_factory=dict)
    trade_history: List[ClosedTrade] = field(default_factory=list)


class RiskAndPortfolioManager:
    """資産保護・ポジションサイジング・決済判定を統括するマネージャー。"""

    def __init__(self, state_file_path: str = DEFAULT_STATE_FILE):
        """状態ファイルを読み込み、無ければ初期予算（約1,000円＝6.80 USDC）で初期化する。"""
        self.state_file_path = os.path.abspath(state_file_path)
        os.makedirs(os.path.dirname(self.state_file_path), exist_ok=True)
        self.state = self._load_state()
        self._refresh_daily_reset()

    def _load_state(self) -> PortfolioState:
        """永続化されたポートフォリオ状態をJSONから復元する。"""
        if not os.path.exists(self.state_file_path):
            state = PortfolioState()
            self._save_state(state)
            return state

        try:
            with open(self.state_file_path, "r", encoding="utf-8") as f:
                raw = json.load(f)

            open_pos: Dict[str, Position] = {}
            for sym, pos_data in raw.get("open_positions", {}).items():
                if "opened_at_ts" not in pos_data:
                    pos_data["opened_at_ts"] = time.time()
                open_pos[sym] = Position(**pos_data)

            history = [ClosedTrade(**t) for t in raw.get("trade_history", [])]

            return PortfolioState(
                mode=raw.get("mode", SETTINGS.trading_mode),
                initial_usdc=float(raw.get("initial_usdc", SETTINGS.initial_paper_usdc)),
                available_usdc=float(raw.get("available_usdc", SETTINGS.initial_paper_usdc)),
                daily_start_equity_usdc=float(
                    raw.get("daily_start_equity_usdc", SETTINGS.initial_paper_usdc)
                ),
                current_date=raw.get("current_date", datetime.now().strftime("%Y-%m-%d")),
                consecutive_losses=int(raw.get("consecutive_losses", 0)),
                cooldown_until_ts=float(raw.get("cooldown_until_ts", 0.0)),
                total_trades=int(raw.get("total_trades", 0)),
                winning_trades=int(raw.get("winning_trades", 0)),
                total_net_pnl_usdc=float(raw.get("total_net_pnl_usdc", 0.0)),
                open_positions=open_pos,
                trade_history=history,
            )
        except Exception as exc:
            print(f"【警告】状態ファイルの読み込みに失敗したため新規作成します: {exc}")
            return PortfolioState()

    def _save_state(self, state: Optional[PortfolioState] = None) -> None:
        """現在のポートフォリオ状態をJSONファイルへ保存する。"""
        target = state or self.state
        payload = asdict(target)
        with open(self.state_file_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

    def _refresh_daily_reset(self) -> None:
        """日付が変わっていれば日次基準資産額をリセットする。"""
        today_str = datetime.now().strftime("%Y-%m-%d")
        if self.state.current_date != today_str:
            self.state.current_date = today_str
            self.state.daily_start_equity_usdc = self.get_total_equity_usdc()
            self._save_state()

    def get_total_equity_usdc(self, latest_prices: Optional[Dict[str, float]] = None) -> float:
        """現金(USDC)と保有ポジション評価額の合計（総資産額）を算出する。"""
        pos_value = 0.0
        for sym, pos in self.state.open_positions.items():
            price = (latest_prices or {}).get(sym, pos.entry_price)
            pos_value += pos.token_amount * price
        return self.state.available_usdc + pos_value

    def can_open_new_position(self, latest_prices: Optional[Dict[str, float]] = None) -> tuple[bool, str]:
        """
        サーキットブレーカー・同時保有上限・余力をチェックし、新規エントリー可能か判定する。
        """
        self._refresh_daily_reset()

        if len(self.state.open_positions) >= SETTINGS.max_concurrent_positions:
            return False, f"最大同時保有数({SETTINGS.max_concurrent_positions}銘柄)に達しています。"

        now_ts = time.time()
        if now_ts < self.state.cooldown_until_ts:
            remain_min = int((self.state.cooldown_until_ts - now_ts) / 60) + 1
            return False, f"連敗クールダウン中です（残り約 {remain_min} 分）。"

        current_equity = self.get_total_equity_usdc(latest_prices)
        if self.state.daily_start_equity_usdc > 0:
            daily_dd_pct = (
                (self.state.daily_start_equity_usdc - current_equity)
                / self.state.daily_start_equity_usdc
                * 100.0
            )
            if daily_dd_pct >= SETTINGS.max_daily_drawdown_pct:
                return (
                    False,
                    f"🚨 サーキットブレーカー発動: 本日のドローダウン({daily_dd_pct:.2f}%)が"
                    f"上限({SETTINGS.max_daily_drawdown_pct}%)に達したため新規取引を停止します。",
                )

        if self.state.available_usdc < 1.50:
            return False, f"利用可能USDC残高(${self.state.available_usdc:.2f})が最小発注額未満です。"

        return True, "OK"

    def calculate_order_size_usdc(self) -> float:
        """1回のトレードに投入するUSDC額を計算する。"""
        order_usdc = self.state.available_usdc * SETTINGS.position_size_ratio
        return round(order_usdc, 4)

    def record_open_position(
        self,
        symbol: str,
        mint_address: str,
        strategy_name: str,
        entry_price: float,
        stop_loss_price: float,
        take_profit_price: float,
        invested_usdc: float,
        token_amount: float,
    ) -> Position:
        """新規ポジションを記録し、利用可能USDCを減算して保存する。"""
        pos = Position(
            symbol=symbol,
            mint_address=mint_address,
            strategy_name=strategy_name,
            entry_price=entry_price,
            highest_price=entry_price,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            invested_usdc=invested_usdc,
            token_amount=token_amount,
            opened_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            opened_at_ts=time.time(),
            trailing_active=False,
        )
        self.state.available_usdc = round(max(0.0, self.state.available_usdc - invested_usdc), 4)
        self.state.open_positions[symbol] = pos
        self._save_state()
        return pos

    def check_exit_conditions(
        self,
        symbol: str,
        current_price: float,
        recent_high_since_open: Optional[float] = None,
    ) -> tuple[bool, str]:
        """
        保有ポジションに対して「利確」「損切り」「トレーリングストップ」「時間切れ塩漬け撤退」を判定する。
        GitHub Actionsの10分間隔実行でも、直近ローソク足高値(recent_high_since_open)を加味してトレーリングを起動する。
        """
        pos = self.state.open_positions.get(symbol)
        if not pos:
            return False, ""

        observed_peak = max(current_price, recent_high_since_open or current_price)

        # 最高値の更新チェックとトレーリングストップの引き上げ
        if observed_peak > pos.highest_price:
            pos.highest_price = observed_peak
            gain_from_entry_pct = ((observed_peak - pos.entry_price) / pos.entry_price) * 100.0

            if gain_from_entry_pct >= SETTINGS.trailing_activation_pct:
                new_trailing_sl = observed_peak * (1.0 - SETTINGS.trailing_distance_pct / 100.0)
                # 最低でも建値＋往復手数料(0.3%)以上を確保する
                breakeven_plus_fee = pos.entry_price * 1.003
                target_sl = max(new_trailing_sl, breakeven_plus_fee)
                if target_sl > pos.stop_loss_price:
                    pos.stop_loss_price = target_sl
                    pos.trailing_active = True
            self._save_state()

        # 1. 目標利確（Take Profit）到達判定
        if current_price >= pos.take_profit_price:
            pnl_pct = ((current_price - pos.entry_price) / pos.entry_price) * 100.0
            return True, f"🎯 目標利確到達 (+{pnl_pct:.2f}% / Target=${pos.take_profit_price:.4f})"

        # 2. 損切り（Stop Loss）またはトレーリングストップ到達判定
        if current_price <= pos.stop_loss_price:
            pnl_pct = ((current_price - pos.entry_price) / pos.entry_price) * 100.0
            if pos.trailing_active:
                return True, f"🔒 トレーリングストップ利確 ({pnl_pct:+.2f}% / TrailSL=${pos.stop_loss_price:.4f})"
            return True, f"🛡️ 損切りストップ発動 ({pnl_pct:+.2f}% / SL=${pos.stop_loss_price:.4f})"

        # 3. タイムストップ（6時間以上経過しても勢いがなく -0.8%〜+0.8% で塩漬けになっている場合は資金解放）
        holding_hours = (time.time() - pos.opened_at_ts) / 3600.0
        pnl_pct = ((current_price - pos.entry_price) / pos.entry_price) * 100.0
        if holding_hours >= 6.0 and -0.8 <= pnl_pct <= 0.8 and not pos.trailing_active:
            return True, f"⏱️ タイムストップ撤退 (保有{holding_hours:.1f}時間・モメンタム停滞 {pnl_pct:+.2f}%)"

        return False, ""

    def record_close_position(
        self, symbol: str, exit_price: float, returned_usdc: float, exit_reason: str
    ) -> Optional[ClosedTrade]:
        """ポジション決済を記録し、勝敗カウントや連敗クールダウンを更新する。"""
        pos = self.state.open_positions.pop(symbol, None)
        if not pos:
            return None

        net_pnl_usdc = round(returned_usdc - pos.invested_usdc, 4)
        net_pnl_pct = round((net_pnl_usdc / pos.invested_usdc) * 100.0, 2) if pos.invested_usdc > 0 else 0.0

        self.state.available_usdc = round(self.state.available_usdc + returned_usdc, 4)
        self.state.total_trades += 1
        self.state.total_net_pnl_usdc = round(self.state.total_net_pnl_usdc + net_pnl_usdc, 4)

        if net_pnl_usdc > 0:
            self.state.winning_trades += 1
            self.state.consecutive_losses = 0
        else:
            self.state.consecutive_losses += 1
            if self.state.consecutive_losses >= SETTINGS.max_consecutive_losses:
                self.state.cooldown_until_ts = time.time() + (SETTINGS.cooldown_minutes * 60)
                print(
                    f"⚠️ {self.state.consecutive_losses}連敗に達したため、"
                    f"{SETTINGS.cooldown_minutes}分間のクールダウン休止に入ります。"
                )

        closed = ClosedTrade(
            symbol=symbol,
            strategy_name=pos.strategy_name,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            invested_usdc=pos.invested_usdc,
            net_pnl_usdc=net_pnl_usdc,
            net_pnl_pct=net_pnl_pct,
            exit_reason=exit_reason,
            closed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        self.state.trade_history.append(closed)
        if len(self.state.trade_history) > 100:
            self.state.trade_history = self.state.trade_history[-100:]

        self._save_state()
        return closed
