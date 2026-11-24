"""
Jupiter API v6 を用いた注文執行モジュール（PAPER仮想売買モード ＆ LIVE実資金モード両対応・100点完成版）。
依存ライブラリのバージョン競合（solana-py / httpx2 / websockets）を100%排除するため、
Solana RPC通信は標準の requests による JSON-RPC 2.0 直接通信で実装し、
トランザクション署名のみ solders (VersionedTransaction.populate) を使用する高信頼設計。
"""

import base64
import json
import os
import time
from typing import Optional
import requests
from config.settings import SETTINGS, SUPPORTED_TOKENS


def _format_tx_for_log(tx_sig: str) -> str:
    """
    PublicリポジトリのGitHub Actions上で実行されている場合、
    第三者にウォレットアドレスを特定されないよう公開ログ上のトランザクション署名をマスクする。
    """
    if os.getenv("GITHUB_ACTIONS") == "true":
        return f"{tx_sig[:6]}...{tx_sig[-6:]} (Publicログ保護のためマスク済)"
    return f"https://solscan.io/tx/{tx_sig}"


class JupiterSwapExecutor:
    """Jupiter v6 アグリゲーターを通じたスワップ執行クラス。"""

    def __init__(self, mode: str = SETTINGS.trading_mode):
        """執行モード（PAPER または LIVE）とRPCエンドポイントを初期化する。"""
        self.mode = (mode or "PAPER").strip().upper()
        self.rpc_url = (SETTINGS.solana_rpc_url or "https://api.mainnet-beta.solana.com").strip()
        self.quote_url = "https://quote-api.jup.ag/v6/quote"
        self.swap_url = "https://quote-api.jup.ag/v6/swap"
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def _rpc_call(self, method: str, params: list) -> dict:
        """Solana JSON-RPC 2.0 エンドポイントへ直接リクエストを送信する。"""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        }
        resp = self.session.post(self.rpc_url, json=payload, timeout=12)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data and data["error"]:
            raise RuntimeError(f"Solana RPC error ({method}): {data['error']}")
        return data.get("result", {})

    def _load_keypair(self):
        """環境変数 SOLANA_PRIVATE_KEY からPhantomサブウォレットの秘密鍵を読み込む。"""
        import base58
        from solders.keypair import Keypair

        raw_key = os.getenv("SOLANA_PRIVATE_KEY", "").strip()
        if not raw_key or raw_key == "your_private_key_here":
            raise ValueError(
                "LIVEモードには環境変数 SOLANA_PRIVATE_KEY の設定が必要です。"
                "Phantomの自動売買専用サブアカウントの秘密鍵を Secrets または .env に設定してください。"
            )

        if raw_key.startswith("["):
            secret_bytes = bytes(json.loads(raw_key))
            return Keypair.from_bytes(secret_bytes)
        secret_bytes = base58.b58decode(raw_key)
        return Keypair.from_bytes(secret_bytes)

    def check_sol_gas_reserve(self) -> tuple[bool, float]:
        """
        ウォレット内のネイティブSOL残高がガス代リザーブ（0.012 SOL）以上あるか確認する。
        """
        if self.mode == "PAPER":
            return True, 0.05

        keypair = self._load_keypair()
        result = self._rpc_call("getBalance", [str(keypair.pubkey())])
        lamports = int(result.get("value", 0))
        sol_balance = lamports / (10 ** 9)
        return sol_balance >= SETTINGS.min_sol_gas_reserve, sol_balance

    def get_quote(self, input_mint: str, output_mint: str, amount_raw: int) -> Optional[dict]:
        """Jupiter v6 Quote APIから最適ルートと想定受取量を取得する。"""
        if amount_raw <= 0:
            return None

        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_raw),
            "slippageBps": str(SETTINGS.slippage_bps),
        }
        try:
            resp = requests.get(self.quote_url, params=params, timeout=10)
            resp.raise_for_status()
            quote_data = resp.json()
            price_impact_pct = float(quote_data.get("priceImpactPct", 0) or 0) * 100.0
            if price_impact_pct > 1.0:
                print(f"🛑【スリッページ保護】価格影響度({price_impact_pct:.2f}%)が高すぎるため注文を拒否しました。")
                return None
            return quote_data
        except Exception as exc:
            print(f"【Jupiter見積警告】Quote API取得失敗（フォールバック計算を使用）: {exc}")
            return None

    def execute_buy_with_usdc(
        self, target_symbol: str, usdc_amount: float, spot_price_usd: float
    ) -> Optional[float]:
        """
        USDCを使って指定銘柄（SOL, JUP, RAY, BONK）を購入し、獲得したトークン数量を返す。
        """
        usdc_cfg = SUPPORTED_TOKENS["USDC"]
        target_cfg = SUPPORTED_TOKENS[target_symbol]
        amount_raw = int(usdc_amount * (10 ** usdc_cfg.decimals))

        quote = self.get_quote(usdc_cfg.mint_address, target_cfg.mint_address, amount_raw)

        if self.mode == "PAPER":
            if quote and "outAmount" in quote:
                out_raw = int(quote["outAmount"])
                received_tokens = out_raw / (10 ** target_cfg.decimals)
            else:
                effective_usdc = usdc_amount * (1.0 - (SETTINGS.estimated_roundtrip_fee_pct / 200.0))
                received_tokens = effective_usdc / spot_price_usd

            print(
                f"📝【PAPER購入完了】${usdc_amount:.2f} USDC -> "
                f"{received_tokens:.6f} {target_symbol} (@ ${spot_price_usd:.4f})"
            )
            return received_tokens

        # --- LIVEモード（実資金スワップ執行） ---
        has_gas, sol_bal = self.check_sol_gas_reserve()
        if not has_gas:
            print(
                f"🛑【ガス保護停止】SOL残高({sol_bal:.4f} SOL)が安全リザーブ"
                f"({SETTINGS.min_sol_gas_reserve} SOL)未満のため発注を中止しました。"
            )
            return None

        if not quote or "outAmount" not in quote:
            print("🛑【LIVE購入中止】Jupiterから有効なQuoteが取得できませんでした。")
            return None

        tx_sig = self._sign_and_send_swap(quote)
        if not tx_sig:
            return None

        out_raw = int(quote["outAmount"])
        received_tokens = out_raw / (10 ** target_cfg.decimals)
        print(
            f"🚀【LIVE購入成功】${usdc_amount:.2f} USDC -> {received_tokens:.6f} {target_symbol} "
            f"| TX: {_format_tx_for_log(tx_sig)}"
        )
        return received_tokens

    def execute_sell_to_usdc(
        self, target_symbol: str, token_amount: float, spot_price_usd: float
    ) -> Optional[float]:
        """
        保有トークンをすべて売却してUSDCに戻し、獲得したUSDC額を返す。
        """
        usdc_cfg = SUPPORTED_TOKENS["USDC"]
        target_cfg = SUPPORTED_TOKENS[target_symbol]
        amount_raw = int(token_amount * (10 ** target_cfg.decimals))

        quote = self.get_quote(target_cfg.mint_address, usdc_cfg.mint_address, amount_raw)

        if self.mode == "PAPER":
            if quote and "outAmount" in quote:
                out_raw = int(quote["outAmount"])
                received_usdc = out_raw / (10 ** usdc_cfg.decimals)
            else:
                gross_usdc = token_amount * spot_price_usd
                received_usdc = gross_usdc * (1.0 - (SETTINGS.estimated_roundtrip_fee_pct / 200.0))

            print(
                f"📝【PAPER売却完了】{token_amount:.6f} {target_symbol} -> "
                f"${received_usdc:.4f} USDC (@ ${spot_price_usd:.4f})"
            )
            return received_usdc

        # --- LIVEモード（実資金スワップ執行） ---
        if not quote or "outAmount" not in quote:
            print("🛑【LIVE売却失敗】Jupiterから有効なQuoteが取得できませんでした。")
            return None

        tx_sig = self._sign_and_send_swap(quote)
        if not tx_sig:
            return None

        out_raw = int(quote["outAmount"])
        received_usdc = out_raw / (10 ** usdc_cfg.decimals)
        print(
            f"🚀【LIVE売却成功】{token_amount:.6f} {target_symbol} -> ${received_usdc:.4f} USDC "
            f"| TX: {_format_tx_for_log(tx_sig)}"
        )
        return received_usdc

    def _sign_and_send_swap(self, quote_response: dict) -> Optional[str]:
        """
        Priority Fee上限を設定してJupiter Swapトランザクションを生成し、
        soldersのVersionedTransaction.populate方式で署名・送信・ブロック確定確認を行う。
        """
        try:
            from solders.message import to_bytes_versioned
            from solders.transaction import VersionedTransaction

            keypair = self._load_keypair()
            payload = {
                "userPublicKey": str(keypair.pubkey()),
                "quoteResponse": quote_response,
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": {
                    "priorityLevelWithMaxLamports": {
                        "maxLamports": SETTINGS.max_priority_fee_lamports,
                        "priorityLevel": "medium",
                    }
                },
            }
            resp = requests.post(self.swap_url, json=payload, timeout=12)
            resp.raise_for_status()
            swap_data = resp.json()

            raw_tx_bytes = base64.b64decode(swap_data["swapTransaction"])
            unsigned_tx = VersionedTransaction.from_bytes(raw_tx_bytes)

            msg_bytes = to_bytes_versioned(unsigned_tx.message)
            signature = keypair.sign_message(msg_bytes)
            signed_tx = VersionedTransaction.populate(unsigned_tx.message, [signature])

            encoded_tx = base64.b64encode(bytes(signed_tx)).decode("ascii")
            tx_sig_str = self._rpc_call(
                "sendTransaction",
                [
                    encoded_tx,
                    {
                        "encoding": "base64",
                        "skipPreflight": False,
                        "preflightCommitment": "confirmed",
                    },
                ],
            )
            if not isinstance(tx_sig_str, str) or not tx_sig_str:
                print(f"🛑【送信エラー】不正なトランザクション署名レスポンス: {tx_sig_str}")
                return None

            if not self._wait_for_confirmation(tx_sig_str):
                print("🛑【TX未確定警告】トランザクションがオンチェーンで確定しなかったか失敗しました。")
                return None

            return tx_sig_str
        except Exception as exc:
            print(f"【エラー】トランザクション署名・送信に失敗しました: {exc}")
            return None

    def _wait_for_confirmation(self, tx_sig_str: str, max_wait_sec: int = 25) -> bool:
        """送信したトランザクションがオンチェーンで成功（err == None）したか確認する。"""
        start = time.time()
        while time.time() - start < max_wait_sec:
            try:
                result = self._rpc_call(
                    "getSignatureStatuses",
                    [[tx_sig_str], {"searchTransactionHistory": True}],
                )
                statuses = result.get("value") or []
                if statuses and statuses[0] is not None:
                    st = statuses[0]
                    if st.get("err") is not None:
                        print(f"🛑【オンチェーンエラー】TX失敗: {st.get('err')}")
                        return False
                    conf_status = st.get("confirmationStatus")
                    if conf_status in ("confirmed", "finalized"):
                        return True
            except Exception:
                pass
            time.sleep(2.0)
        return False
