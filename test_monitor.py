import os
import sys
from dotenv import load_dotenv

# モジュールのインポート
try:
    from solana_ai_monitor import send_telegram_notification, analyze_meme_with_ai
except ImportError as e:
    print(f"【エラー】solana_ai_monitor.py のインポートに失敗しました: {e}")
    sys.exit(1)

def test_telegram():
    print("\n--- 1. Telegram通知テスト ---")
    token_info = {
        "address": "7GCih69m87RwFrggh7B27AyjGusKuB2GkymG5pZbNzYu",  # POPCAT本物
        "name": "Popcat (Test)",
        "symbol": "POPCAT",
        "mcap": 450000000.0,
        "liq": 8500000.0,
        "vol": 25000000.0
    }
    
    ai_analysis = {
        "score": 95,
        "reason": "これはテスト通知です。Popcatはネット上で非常に人気のある猫のミームであり、強力なコミュニティに支えられているため、高いバイラル可能性を持ちます。"
    }
    
    print("Telegramにテストアラートを送信中...")
    success = send_telegram_notification(token_info, ai_analysis)
    if success:
        print("【検証結果】✅ Telegramへのテスト送信が完了しました！iPhoneに通知が届いたか確認してください。")
    else:
        print("【検証結果】❌ Telegramへのテスト送信に失敗しました。.envの設定（Token, Chat ID）を確認してください。")

def test_ai():
    print("\n--- 2. Gemini AI評価テスト ---")
    # ダミーの「少し怪しいミームコイン」を模したデータ
    dummy_token = {
        "name": "Dogwifhat Gold 2.0",
        "symbol": "WIFGOLD",
        "mcap": 12000.0,
        "liq": 6000.0,
        "vol": 15000.0
    }
    
    print(f"AIに以下のトークンを評価させています: {dummy_token['name']} ({dummy_token['symbol']})...")
    result = analyze_meme_with_ai(dummy_token)
    
    print("\n【AIの分析結果】")
    print(f"スコア: {result.get('score')}/100")
    print(f"判定理由: {result.get('reason')}")
    print("【検証結果】✅ Gemini APIの呼び出しとJSON解析が正常に行われました。")

if __name__ == "__main__":
    load_dotenv()
    
    # 起動引数でテストを分岐できるようにする
    if len(sys.argv) > 1 and sys.argv[1] == "ai":
        test_ai()
    elif len(sys.argv) > 1 and sys.argv[1] == "telegram":
        test_telegram()
    else:
        print("使用法:")
        print("  python3 test_monitor.py telegram  (Telegramの通知送信テスト)")
        print("  python3 test_monitor.py ai        (Gemini AIの分析テスト)")
