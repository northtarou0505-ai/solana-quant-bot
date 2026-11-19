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
    trading_mode: str = os.getenv("TRADING_MODE", "PAPER").upper()

    # 初期予算設定（日本円換算およびUSD換算の目安）
    initial_budget_jpy: float = float(os.getenv("INITIAL_BUDGET_JPY", "1000"))
    initial_paper_usdc: float = float(os.getenv("INITIAL_PAPER_USDC", "6.80"))  # 約1,000円相当

    # 監視対象の銘柄リスト（USDC建てで売買を行う主要銘柄）
    target_symbols: list[str] = field(
        default_factory=lambda: ["SOL", "JUP", "RAY", "BONK"]
    )
    base_currency_symbol: str = "USDC"

    # --- 1,000円少額運用のための特別防衛ルール ---
    # ガス代およびトークンアカウント(ATA)維持のために絶対に手を付けないSOL残高
    # 0.012 SOL (約250〜300円相当) をウォレットに常時残すことでガス欠による売却不能を防止
    min_sol_gas_reserve: float = float(os.getenv("MIN_SOL_GAS_RESERVE", "0.012"))

    # 少額資金では分散しすぎると手数料負けするため、最も優位性の高い1銘柄に集中投資する
    max_concurrent_positions: int = int(os.getenv("MAX_CONCURRENT_POSITIONS", "1"))

    # 1回のエントリーに使う利用可能USDCの割合（0.95 = 余力の95%を最良シグナルへ投入）
    position_size_ratio: float = float(os.getenv("POSITION_SIZE_RATIO", "0.95"))

    # --- 損益・リスク管理（凄腕トレーダー基準） ---
    # 1トレードあたりの最大許容損切り幅（価格変動率ベース: -2.2%で素早く損切り）
    max_stop_loss_pct: float = float(os.getenv("MAX_STOP_LOSS_PCT", "2.2"))
    # 最小目標利確幅（手数料・スリッページを確実に上回るため +3.8% 以上を狙う）
    min_take_profit_pct: float = float(os.getenv("MIN_TAKE_PROFIT_PCT", "3.8"))
    # 最小リスクリワード比（手数料控除後で 1 : 1.6 以上必須）
    min_risk_reward_ratio: float = float(os.getenv("MIN_RISK_REWARD_RATIO", "1.6"))

    # トレーリングストップ設定（含み益が +2.0% を超えたらストップを建値以上に引き上げる）
    trailing_activation_pct: float = float(os.getenv("TRAILING_ACTIVATION_PCT", "2.0"))
    trailing_distance_pct: float = float(os.getenv("TRAILING_DISTANCE_PCT", "1.0"))

    # --- サーキットブレーカー（退場防止装置） ---
    # 1日の最大許容ドローダウン（総資産の 5.0% 減少で当日取引停止）
    max_daily_drawdown_pct: float = float(os.getenv("MAX_DAILY_DRAWDOWN_PCT", "5.0"))
    # 最大連敗数（3連敗したらクールダウン休止）
    max_consecutive_losses: int = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3"))
    cooldown_minutes: int = int(os.getenv("COOLDOWN_MINUTES", "60"))

    # --- Solana / Jupiter 執行最適化パラメータ ---
    solana_rpc_url: str = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
    # スリッページ許容上限（主要銘柄なので 50 bps = 0.5% に厳格化）
    slippage_bps: int = int(os.getenv("SLIPPAGE_BPS", "50"))
    # 1,000円予算でガス代負けしないためのPriority Fee上限（50,000 lamports = 0.00005 SOL ≈ 約0.01ドル以下）
    max_priority_fee_lamports: int = int(os.getenv("MAX_PRIORITY_FEE_LAMPORTS", "50000"))
    # 往復の推定DEX＋ネットワークコスト率（約0.25%として純利益計算に使用）
    estimated_roundtrip_fee_pct: float = 0.25

    # ループ間隔（秒）
    scan_interval_seconds: int = int(os.getenv("SCAN_INTERVAL_SECONDS", "30"))


SETTINGS = TradingSettings()
