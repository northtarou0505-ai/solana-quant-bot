"""
Solana 自動売買システム（少額1,000円スタート＆主要高流動性銘柄特化）の設定モジュール。
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict
from dotenv import load_dotenv

# プロジェクトルートの .env を確実に読み込む
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _env_str(key: str, default: str) -> str:
    """環境変数が未設定または空文字の場合でも安全にデフォルト文字列を返すヘルパー。"""
    val = (os.getenv(key) or "").strip()
    return val if val else default


def _env_float(key: str, default: float) -> float:
    """環境変数が未設定・空文字・不正値の場合でも安全にfloatを返すヘルパー。"""
    val = (os.getenv(key) or "").strip()
    if not val:
        return default
    try:
        return float(val)
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    """環境変数が未設定・空文字・不正値の場合でも安全にintを返すヘルパー。"""
    val = (os.getenv(key) or "").strip()
    if not val:
        return default
    try:
        return int(float(val))
    except ValueError:
        return default


@dataclass(frozen=True)
class TokenConfig:
    """取引対象トークンの基本情報とコントラクト定義。"""
    symbol: str
    name: str
    mint_address: str
    decimals: int
    min_liquidity_usd: float


# 安全・高流動性の主要Solana銘柄リスト（ラグプルリスクが極めて低い銘柄に限定）
SUPPORTED_TOKENS: Dict[str, TokenConfig] = {
    "SOL": TokenConfig(
        symbol="SOL",
        name="Wrapped SOL",
        mint_address="So11111111111111111111111111111111111111112",
        decimals=9,
        min_liquidity_usd=5_000_000.0,
    ),
    "USDC": TokenConfig(
        symbol="USDC",
        name="USD Coin",
        mint_address="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        decimals=6,
        min_liquidity_usd=10_000_000.0,
    ),
    "JUP": TokenConfig(
        symbol="JUP",
        name="Jupiter",
        mint_address="JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
        decimals=6,
        min_liquidity_usd=1_000_000.0,
    ),
    "RAY": TokenConfig(
        symbol="RAY",
        name="Raydium",
        mint_address="4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
        decimals=6,
        min_liquidity_usd=1_000_000.0,
    ),
    "BONK": TokenConfig(
        symbol="BONK",
        name="Bonk",
        mint_address="DezXAZ8z7PnrnresjzR24dB3e9BYssvqj2C2nAFZ6j3",
        decimals=5,
        min_liquidity_usd=1_000_000.0,
    ),
}


@dataclass
class TradingSettings:
    """
    1,000円（約6.5〜7 USD）からの少額運用に最適化したクオンツ・リスク管理設定。
    """
    # 動作モード: "PAPER" (仮想売買・デフォルト) または "LIVE" (実資金取引)
    trading_mode: str = _env_str("TRADING_MODE", "PAPER").upper()

    # 初期予算設定（日本円換算およびUSD換算の目安）
    initial_budget_jpy: float = _env_float("INITIAL_BUDGET_JPY", 1000.0)
    initial_paper_usdc: float = _env_float("INITIAL_PAPER_USDC", 6.80)

    # 監視対象の銘柄リスト（USDC建てで売買を行う主要銘柄）
    target_symbols: list[str] = field(
        default_factory=lambda: ["SOL", "JUP", "RAY", "BONK"]
    )
    base_currency_symbol: str = "USDC"

    # --- 1,000円少額運用のための特別防衛ルール ---
    min_sol_gas_reserve: float = _env_float("MIN_SOL_GAS_RESERVE", 0.012)
    max_concurrent_positions: int = _env_int("MAX_CONCURRENT_POSITIONS", 1)
    position_size_ratio: float = _env_float("POSITION_SIZE_RATIO", 0.95)

    # --- 損益・リスク管理（凄腕トレーダー基準） ---
    max_stop_loss_pct: float = _env_float("MAX_STOP_LOSS_PCT", 2.2)
    min_take_profit_pct: float = _env_float("MIN_TAKE_PROFIT_PCT", 3.8)
    min_risk_reward_ratio: float = _env_float("MIN_RISK_REWARD_RATIO", 1.6)

    # トレーリングストップ設定
    trailing_activation_pct: float = _env_float("TRAILING_ACTIVATION_PCT", 2.0)
    trailing_distance_pct: float = _env_float("TRAILING_DISTANCE_PCT", 1.0)

    # --- サーキットブレーカー（退場防止装置） ---
    max_daily_drawdown_pct: float = _env_float("MAX_DAILY_DRAWDOWN_PCT", 5.0)
    max_consecutive_losses: int = _env_int("MAX_CONSECUTIVE_LOSSES", 3)
    cooldown_minutes: int = _env_int("COOLDOWN_MINUTES", 60)

    # --- Solana / Jupiter 執行最適化パラメータ ---
    solana_rpc_url: str = _env_str("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
    slippage_bps: int = _env_int("SLIPPAGE_BPS", 50)
    max_priority_fee_lamports: int = _env_int("MAX_PRIORITY_FEE_LAMPORTS", 50000)
    estimated_roundtrip_fee_pct: float = 0.25

    # ループ間隔（秒）
    scan_interval_seconds: int = _env_int("SCAN_INTERVAL_SECONDS", 30)


SETTINGS = TradingSettings()
