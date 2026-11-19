"""
Solana主要銘柄（SOL, JUP, RAY, BONK）のリアルタイム価格・流動性・OHLCVローソク足データを取得するモジュール。
無料かつAPIキー不要で利用できる DexScreener API および GeckoTerminal API を組み合わせ、
偽流動性プール除外フィルターとリトライ耐性を備えた高信頼データを提供する。
"""

import time
from dataclasses import dataclass
from typing import List, Optional, Set
import requests
from config.settings import SUPPORTED_TOKENS

# 信頼できる決済相手通貨（USDC, USDT, WSOL）のMintアドレスのみを許可し、偽プールを排除する
TRUSTED_QUOTE_MINTS: Set[str] = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "So11111111111111111111111111111111111111112",  # WSOL
}


@dataclass
class Candle:
    """1本のローソク足（OHLCV）を表すデータ構造。"""
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class MarketSnapshot:
    """銘柄の現在価格・流動性・出来高・ローソク足群をまとめたスナップショット。"""
    symbol: str
    mint_address: str
    pair_address: str
    price_usd: float
    liquidity_usd: float
    volume_24h_usd: float
    price_change_1h_pct: float
    price_change_24h_pct: float
    candles_5m: List[Candle]


class SolanaMarketDataClient:
    """Solana DEXの市場データを取得するクライアント。"""

    def __init__(self, request_timeout: int = 12):
        """HTTPセッションとタイムアウトを初期化する。"""
        self.timeout = request_timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "SolanaQuantTrader/2.0"
        })

    def _get_with_retry(
        self, url: str, params: Optional[dict] = None, headers: Optional[dict] = None, retries: int = 3
    ) -> Optional[requests.Response]:
        """一時的なネットワーク遅延やAPIレート制限(429)に対して指数バックオフでリトライする。"""
        for attempt in range(retries):
            try:
                resp = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
                if resp.status_code == 429:
                    wait_sec = 1.5 * (attempt + 1)
                    time.sleep(wait_sec)
                    continue
                resp.raise_for_status()
                return resp
            except Exception as exc:
                if attempt == retries - 1:
                    print(f"【API通信警告】リクエスト失敗 ({url}): {exc}")
                    return None
                time.sleep(1.0 * (attempt + 1))
        return None

    def fetch_market_snapshot(self, symbol: str) -> Optional[MarketSnapshot]:
        """
        指定した銘柄（SOL, JUP, RAY, BONK）の最良プール情報と5分足OHLCVを取得する。
        """
        token_cfg = SUPPORTED_TOKENS.get(symbol)
        if not token_cfg:
            raise ValueError(f"未対応の銘柄シンボルです: {symbol}")

        # 1. DexScreenerから最も流動性の高い正規Solanaプール情報を取得
        pair_info = self._fetch_best_solana_pair(token_cfg.mint_address)
        if not pair_info:
            return None

        pair_address = pair_info["pair_address"]
        price_usd = pair_info["price_usd"]
        liquidity_usd = pair_info["liquidity_usd"]
        volume_24h_usd = pair_info["volume_24h_usd"]
        price_change_1h_pct = pair_info["price_change_1h_pct"]
        price_change_24h_pct = pair_info["price_change_24h_pct"]

        if price_usd <= 0:
            return None

        # 最低流動性フィルターの確認
        if liquidity_usd < token_cfg.min_liquidity_usd:
            print(
                f"【流動性警告】{symbol} の流動性(${liquidity_usd:,.0f})が"
                f"基準(${token_cfg.min_liquidity_usd:,.0f})未満のためスキップします。"
            )
            return None

        # 2. GeckoTerminalから該当プールの5分足OHLCVヒストリカルデータを取得
        candles = self._fetch_geckoterminal_ohlcv(pair_address, timeframe="minute", aggregate=5, limit=60)
        if not candles or len(candles) < 25:
            print(f"【データ警告】{symbol} のローソク足データが不足しています（取得本数: {len(candles)}）。")
            return None

        return MarketSnapshot(
            symbol=symbol,
            mint_address=token_cfg.mint_address,
            pair_address=pair_address,
            price_usd=price_usd,
            liquidity_usd=liquidity_usd,
            volume_24h_usd=volume_24h_usd,
            price_change_1h_pct=price_change_1h_pct,
            price_change_24h_pct=price_change_24h_pct,
            candles_5m=candles,
        )

    def _fetch_best_solana_pair(self, mint_address: str) -> Optional[dict]:
        """DexScreener APIを叩き、正規Quote通貨（USDC/USDT/WSOL）かつ流動性が最大のSolanaペア情報を返す。"""
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint_address}"
        resp = self._get_with_retry(url)
        if not resp:
            return None

        try:
            data = resp.json()
            pairs = data.get("pairs") or []

            # Solanaチェーンかつ対象トークンがbaseTokenであり、相手通貨が信頼済み(USDC/USDT/WSOL)のペアのみ抽出
            solana_pairs = [
                p for p in pairs
                if p.get("chainId") == "solana"
                and p.get("baseToken", {}).get("address") == mint_address
                and p.get("quoteToken", {}).get("address") in TRUSTED_QUOTE_MINTS
            ]
            if not solana_pairs:
                return None

            # 流動性(USD)と24h出来高がともに高い最良プールを選択
            solana_pairs.sort(
                key=lambda x: (
                    float(x.get("liquidity", {}).get("usd", 0) or 0),
                    float(x.get("volume", {}).get("h24", 0) or 0),
                ),
                reverse=True,
            )
            best = solana_pairs[0]
            price_change = best.get("priceChange") or {}

            return {
                "pair_address": best.get("pairAddress", ""),
                "price_usd": float(best.get("priceUsd", 0) or 0),
                "liquidity_usd": float(best.get("liquidity", {}).get("usd", 0) or 0),
                "volume_24h_usd": float(best.get("volume", {}).get("h24", 0) or 0),
                "price_change_1h_pct": float(price_change.get("h1", 0) or 0),
                "price_change_24h_pct": float(price_change.get("h24", 0) or 0),
            }
        except Exception as exc:
            print(f"【API解析警告】DexScreener解析エラー ({mint_address[:8]}...): {exc}")
            return None

    def _fetch_geckoterminal_ohlcv(
        self,
        pool_address: str,
        timeframe: str = "minute",
        aggregate: int = 5,
        limit: int = 60,
    ) -> List[Candle]:
        """
        GeckoTerminal Public APIから指定プールのOHLCV（ローソク足）を取得する。
        返却されるリストは古い順（時系列順）にソートされる。
        """
        url = (
            f"https://api.geckoterminal.com/api/v2/networks/solana/pools/"
            f"{pool_address}/ohlcv/{timeframe}"
        )
        params = {
            "aggregate": str(aggregate),
            "limit": str(limit),
            "currency": "usd",
        }
        headers = {"Accept": "application/json;version=20230302"}
        time.sleep(0.35)
        resp = self._get_with_retry(url, params=params, headers=headers)
        if not resp:
            return []

        try:
            payload = resp.json()
            ohlcv_list = (
                payload.get("data", {})
                .get("attributes", {})
                .get("ohlcv_list", [])
            )
            if not ohlcv_list:
                return []

            candles: List[Candle] = []
            for row in ohlcv_list:
                if len(row) >= 6:
                    candles.append(
                        Candle(
                            timestamp=int(row[0]),
                            open=float(row[1]),
                            high=float(row[2]),
                            low=float(row[3]),
                            close=float(row[4]),
                            volume=float(row[5]),
                        )
                    )
            candles.sort(key=lambda c: c.timestamp)
            return candles
        except Exception as exc:
            print(f"【API解析警告】GeckoTerminal OHLCV解析エラー ({pool_address[:8]}...): {exc}")
            return []
