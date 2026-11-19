import os
import sys
from dotenv import load_dotenv

# プロジェクト内のモジュールをインポート
try:
    from solana_meme_trader import get_keypair, client
except ImportError as e:
    print(f"【エラー】solana_meme_trader.py のインポートに失敗しました: {e}")
    sys.exit(1)

def main():
    load_dotenv()
    
    print("--- Solana自動取引スクリプト初期化テスト ---")
    
    # 1. 秘密鍵の設定チェック
    private_key = os.getenv("SOLANA_PRIVATE_KEY")
    if not private_key or private_key == "your_private_key_here":
        print("【検証結果】❌ SOLANA_PRIVATE_KEY が設定されていません。")
        print("  → .env.template から .env を作成し、あなたのウォレットの秘密鍵を設定してください。")
        sys.exit(1)
    else:
        print("【検証結果】✅ 秘密鍵の環境変数検知OK。")
        
    # 2. Keypairオブジェクト生成テスト
    try:
        keypair = get_keypair()
        pubkey = keypair.pubkey()
        print(f"【検証結果】✅ 秘密鍵のデコード＆Keypair生成成功！")
        print(f"  → あなたのウォレットアドレス: {pubkey}")
    except Exception as e:
        print(f"【検証結果】❌ Keypairの生成に失敗しました: {e}")
        sys.exit(1)
        
    # 3. RPCサーバー接続と残高取得テスト
    try:
        balance_resp = client.get_balance(pubkey)
        balance_sol = balance_resp.value / (10**9)
        print(f"【検証結果】✅ RPCサーバー接続成功！")
        print(f"  → 現在の残高: {balance_sol} SOL")
    except Exception as e:
        print(f"【検証結果】❌ RPCサーバーへの接続または残高取得に失敗しました: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
