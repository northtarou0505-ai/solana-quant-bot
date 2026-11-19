import os
import time
import json
import base64
import asyncio
import requests
from dotenv import load_dotenv
from solana.rpc.api import Client
from solders.keypair import Keypair
from solders.message import to_bytes_versioned
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solana.rpc.types import TxOpts
import base58

# .envファイルから環境変数を読み込む
load_dotenv()

# 設定情報の読み込み
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
PRIVATE_KEY_STR = os.getenv("SOLANA_PRIVATE_KEY")
SLIPPAGE_BPS = int(os.getenv("SLIPPAGE_BPS", "100"))  # デフォルト1.0%

# 定数
WSOL_ADDRESS = "So11111111111111111111111111111111111111112"  # Solanaのネイティブトークン(Wrapped SOL)

# RPCクライアントの初期化
client = Client(SOLANA_RPC_URL)

def get_keypair() -> Keypair:
    """
    環境変数から秘密鍵を読み込み、Keypairオブジェクトを生成する。
    """
    if not PRIVATE_KEY_STR or PRIVATE_KEY_STR == "your_private_key_here":
        raise ValueError("環境変数 SOLANA_PRIVATE_KEY が設定されていません。")
    
    # JSON配列形式か、Base58形式かを判定して読み込む
    try:
        if PRIVATE_KEY_STR.strip().startswith("["):
            # [12,34,56...] の形式
            secret_key_bytes = bytes(json.loads(PRIVATE_KEY_STR))
            return Keypair.from_bytes(secret_key_bytes)
        else:
            # Base58形式（4v5nBEF...）
            secret_key_bytes = base58.b58decode(PRIVATE_KEY_STR)
            return Keypair.from_bytes(secret_key_bytes)
    except Exception as e:
        raise ValueError(f"秘密鍵のデコードに失敗しました。形式を確認してください: {e}")

def get_token_price_sol(token_address: str) -> float:
    """
    DEX Screener APIを使用して、指定されたトークンのSOL建て価格(priceNative)を取得する。
    """
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        pairs = data.get("pairs", [])
        if not pairs:
            raise ValueError("ペア情報が見つかりません。")
        
        # Solanaネットワークのペアを探す
        for pair in pairs:
            if pair.get("chainId") == "solana":
                # priceNativeは対SOL（または対WSOL）の価格
                price_native = pair.get("priceNative")
                if price_native:
                    return float(price_native)
                    
        raise ValueError("Solanaネットワーク上のペアが見つかりません。")
    except Exception as e:
        print(f"【警告】価格取得に失敗しました: {e}")
        return None

async def get_jupiter_quote(input_mint: str, output_mint: str, amount_lamports: int) -> dict:
    """
    Jupiter APIからスワップのQuote（見積もり）を取得する。
    """
    url = "https://quote-api.jup.ag/v6/quote"
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_lamports),
        "slippageBps": str(SLIPPAGE_BPS)
    }
    
    # 非同期ではなく通常のrequestsを使用（簡単のため）
    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(
        None, lambda: requests.get(url, params=params, timeout=10)
    )
    response.raise_for_status()
    return response.json()

async def execute_jupiter_swap(keypair: Keypair, quote_response: dict) -> str:
    """
    Jupiter APIを使用してスワップトランザクションを生成し、署名・送信する。
    """
    url = "https://quote-api.jup.ag/v6/swap"
    payload = {
        "userPublicKey": str(keypair.pubkey()),
        "quoteResponse": quote_response,
        "wrapAndUnwrapSol": True
    }
    
    # トランザクション生成のリクエスト
    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(
        None, lambda: requests.post(url, json=payload, timeout=10)
    )
    response.raise_for_status()
    swap_data = response.json()
    
    # Base64でエンコードされたトランザクションをデコード
    raw_transaction = base64.b64decode(swap_data["swapTransaction"])
    
    # トランザクションオブジェクトの復元と署名（solders 0.27+ 対応）
    unsigned_tx = VersionedTransaction.from_bytes(raw_transaction)
    signature = keypair.sign_message(to_bytes_versioned(unsigned_tx.message))
    transaction = VersionedTransaction.populate(unsigned_tx.message, [signature])
    
    # トランザクションの送信
    opts = TxOpts(skip_preflight=False, preflight_commitment="confirmed")
    # 同期クライアントのsend_raw_transactionを使用
    tx_sig = client.send_raw_transaction(bytes(transaction), opts)
    
    return str(tx_sig.value)

async def buy_token(keypair: Keypair, token_address: str, amount_sol: float) -> float:
    """
    指定したSOLの額でトークンを購入する。
    購入できたトークンのおおよその枚数を返す。
    """
    amount_lamports = int(amount_sol * (10**9))
    print(f"【購入】{amount_sol} SOL を使用してトークン({token_address})を購入します...")
    
    try:
        quote = await get_jupiter_quote(WSOL_ADDRESS, token_address, amount_lamports)
        expected_output = int(quote.get("outAmount", 0))
        
        # デシマル（桁数）の調整のための情報取得
        # 通常のトークンは6〜9桁が多い
        print(f"【購入】見積もり取得成功。想定獲得トークン量(最小単位): {expected_output}")
        
        tx_sig = await execute_jupiter_swap(keypair, quote)
        print(f"【購入】トランザクション送信成功。署名: {tx_sig}")
        
        # 簡易的な待機（トランザクション確定待ち）
        await asyncio.sleep(5)
        
        return expected_output
    except Exception as e:
        print(f"【エラー】購入に失敗しました: {e}")
        return 0

async def sell_token_percent(keypair: Keypair, token_address: str, amount_token_lamports: int, percent: float) -> bool:
    """
    保有しているトークンの指定された割合(%)を売却してSOLに戻す。
    """
    sell_amount = int(amount_token_lamports * (percent / 100.0))
    if sell_amount <= 0:
        print("【売却】売却量が少なすぎます。")
        return False
        
    print(f"【売却】トークン({token_address})の {percent}% ({sell_amount} 最小単位) を売却します...")
    
    try:
        quote = await get_jupiter_quote(token_address, WSOL_ADDRESS, sell_amount)
        tx_sig = await execute_jupiter_swap(keypair, quote)
        print(f"【売却】トランザクション送信成功。署名: {tx_sig}")
        return True
    except Exception as e:
        print(f"【エラー】売却に失敗しました: {e}")
        return False

async def monitor_and_manage_position(token_address: str, amount_sol: float, stop_loss_percent: float = -50.0):
    """
    トークンを購入し、価格を監視して「2倍で半分売る」管理を行うメインループ。
    """
    try:
        keypair = get_keypair()
        print(f"【初期化】ウォレットアドレス: {keypair.pubkey()}")
        
        # 現在のSOL残高確認
        balance_resp = client.get_balance(keypair.pubkey())
        current_balance = balance_resp.value / (10**9)
        print(f"【初期化】現在のウォレット残高: {current_balance} SOL")
        
        if current_balance < amount_sol:
            print("【エラー】残高が不足しています。")
            return
            
    except Exception as e:
        print(f"【エラー】初期化エラー: {e}")
        return

    # 1. 購入前の基準価格を取得
    entry_price = get_token_price_sol(token_address)
    if not entry_price:
        print("【エラー】基準価格が取得できないため、開始できません。")
        return
    print(f"【開始】購入前基準価格: {entry_price:.10f} SOL")

    # 2. 購入実行
    bought_amount_lamports = await buy_token(keypair, token_address, amount_sol)
    if bought_amount_lamports == 0:
        print("【終了】購入に失敗したため監視を終了します。")
        return

    # 実質的なエントリー価格の記録
    print(f"【監視】購入完了。監視を開始します。目標利確ライン（2倍）: {entry_price * 2:.10f} SOL")
    
    has_half_sold = False
    remaining_amount = bought_amount_lamports

    # 3. 監視ループ
    while True:
        await asyncio.sleep(10)  # 10秒ごとに監視
        current_price = get_token_price_sol(token_address)
        if not current_price:
            continue

        # 変動率の計算
        change_percent = ((current_price - entry_price) / entry_price) * 100.0
        print(f"【価格監視】現在価格: {current_price:.10f} SOL (購入時比: {change_percent:+.2f}%)")

        # 利確判定：2倍 (+100%) 以上かつ、まだ半分売却していない場合
        if change_percent >= 100.0 and not has_half_sold:
            print("🎉【利確トリガー】価格が2倍に達しました！保有量の50%を自動売却します。")
            success = await sell_token_percent(keypair, token_address, bought_amount_lamports, 50.0)
            if success:
                has_half_sold = True
                remaining_amount = bought_amount_lamports - int(bought_amount_lamports * 0.5)
                print(f"✅【利確完了】半分を利益確定しました。残りの {remaining_amount} トークンを監視し続けます。")

        # 損切り判定：設定した損切りライン以下になった場合
        elif change_percent <= stop_loss_percent:
            print(f"⚠️【損切りトリガー】価格が損切りライン({stop_loss_percent}%)を下回りました。全額売却します。")
            percent_to_sell = 100.0 if not has_half_sold else 100.0  # 残り全て
            await sell_token_percent(keypair, token_address, remaining_amount, 100.0)
            print("🛑【損切り完了】ポジションを全決済しました。監視を終了します。")
            break

        # オプション：利確後に元の価格まで戻ってしまった場合などの終了判定（必要に応じて追加）

if __name__ == "__main__":
    import sys
    # テスト実行用の引数チェック
    if len(sys.argv) < 3:
        print("使用法: python solana_meme_trader.py [トークンアドレス] [投資SOL額]")
        print("例: python solana_meme_trader.py DezXAZ8z7PnrnresjzR24dB3e9BYssvqj2C2nAFZ6j3 0.01")
        sys.exit(1)
        
    target_token = sys.argv[1]
    invest_sol = float(sys.argv[2])
    
    # 監視スタート
    asyncio.run(monitor_and_manage_position(target_token, invest_sol))
