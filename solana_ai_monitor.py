import os
import time
import json
import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types

# 環境変数の読み込み
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "5000"))
MIN_VOLUME_24H = float(os.getenv("MIN_VOLUME_24H", "10000"))

# すでに通知済みのトークンを記録するセット（二重通知防止）
notified_tokens = set()

def send_telegram_notification(token_info: dict, ai_analysis: dict) -> bool:
    """
    Telegramボットを通じて、iPhoneにAIの分析結果付きでプッシュ通知を送る。
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("【エラー】Telegramの設定が足りないため、通知を送れません。")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    # 送信テキストの構築（HTMLフォーマット）
    token_name = token_info.get("name", "Unknown")
    token_symbol = token_info.get("symbol", "UNKNOWN")
    token_address = token_info.get("address", "")
    
    mcap = token_info.get("mcap", 0)
    liq = token_info.get("liq", 0)
    vol = token_info.get("vol", 0)
    
    ai_score = ai_analysis.get("score", 0)
    ai_reason = ai_analysis.get("reason", "理由なし")
    
    # DEX Screenerの取引リンクと、BonkBotで即買いできるリファラルリンクを構築
    dex_url = f"https://dexscreener.com/solana/{token_address}"
    bonkbot_url = f"https://t.me/bonkbot_bot?start=ref_antigravity_{token_address}"  # BonkBotで直接買えるリンク
    
    text = (
        f"🚨 <b>【AIミーム検出アラート】</b>\n\n"
        f"💎 <b>トークン名:</b> {token_name} ({token_symbol})\n"
        f"📍 <b>アドレス:</b> <code>{token_address}</code>\n\n"
        f"📊 <b>指標データ:</b>\n"
        f" └ 時価総額 (MCAP): ${mcap:,.0f}\n"
        f" └ 流動性 (LIQ): ${liq:,.0f}\n"
        f" └ 24h取引高 (VOL): ${vol:,.0f}\n\n"
        f"🤖 <b>AI判定スコア:</b> <pre>{ai_score}/100</pre>\n"
        f"📝 <b>AIによる分析・評価:</b>\n"
        f"<i>{ai_reason}</i>\n\n"
        f"🔗 <a href='{dex_url}'>DEX Screenerでチャートを見る</a>\n"
        f"⚡️ <a href='{bonkbot_url}'>BonkBotで今すぐ取引する</a>"
    )
    
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        return True
    except Exception as e:
        print(f"【エラー】Telegram通知の送信に失敗しました: {e}")
        return False

def analyze_meme_with_ai(token_info: dict) -> dict:
    """
    Gemini APIを使い、トークンのメタデータからミームとしての有望性を再検証（スコアリング）する。
    """
    if not GEMINI_API_KEY or GEMINI_API_KEY == "your_gemini_api_key_here":
        print("【警告】Gemini APIキーが設定されていません。AI分析をスキップし、仮スコア(80)で判定します。")
        return {
            "score": 80,
            "reason": "※Gemini APIキー未設定のため、AIによる詳細な分析は行われませんでした。"
        }
        
    try:
        # Geminiクライアントの初期化
        ai_client = genai.Client(api_key=GEMINI_API_KEY)
        
        prompt = (
            f"あなたはプロの仮想通貨ミームコイントレーダー兼アナリストです。\n"
            f"以下の新しいSolanaミームコインの情報を評価し、このコインが海外コミュニティでバズって急騰する可能性（ミームの面白さ、流行りやすさ）があるか、また典型的な詐欺トークンの名前（有名コインの劣化コピーなど）に似ていないかを厳しく分析してください。\n\n"
            f"【トークン情報】\n"
            f"名前: {token_info.get('name')}\n"
            f"シンボル（ティッカー）: {token_info.get('symbol')}\n"
            f"時価総額 (MCAP): ${token_info.get('mcap'):,.0f}\n"
            f"流動性 (LIQ): ${token_info.get('liq'):,.0f}\n"
            f"24h取引高: ${token_info.get('vol'):,.0f}\n\n"
            f"【出力フォーマット】\n"
            f"必ず以下のJSON形式でのみ出力してください。他の説明文は一切含めないでください。\n"
            f"{{\n"
            f"  \"score\": 0から100までの数値（80以上はバズる可能性が極めて高く、危険度が低いもの）,\n"
            f"  \"reason\": \"なぜその点数にしたのかの冷静な分析と理由。ミームのキャッチーさや詐欺リスクについて日本語で2〜3文で簡潔に記述\"\n"
            f"}}"
        )
        
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        
        result = json.loads(response.text)
        return {
            "score": int(result.get("score", 0)),
            "reason": result.get("reason", "分析エラー")
        }
        
    except Exception as e:
        print(f"【エラー】Gemini APIの呼び出しに失敗しました: {e}")
        return {
            "score": 50,
            "reason": f"AI分析エラーが発生しました: {e}"
        }

def get_new_solana_tokens() -> list:
    """
    GeckoTerminal APIを使用して、Solanaの最新プール（新規上場トークン）の一覧を取得する。
    """
    # GeckoTerminalのパブリックAPI（APIキー不要）
    url = "https://api.geckoterminal.com/api/v2/networks/solana/new_pools"
    headers = {"Accept": "application/json;version=20230302"}
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        pools = data.get("data", [])
        tokens_found = []
        
        for pool in pools:
            attributes = pool.get("attributes", {})
            
            # プール名（例：WIF / SOL）から、SOL以外のターゲットトークンの情報を探す
            # 通常、base_tokenが新規トークン、quote_tokenがSOL(またはUSDT)
            relationships = pool.get("relationships", {})
            base_token_data = relationships.get("base_token", {}).get("data", {})
            base_token_id = base_token_data.get("id", "")
            
            # アドレスの抽出 (例: "solana_EKpQG...") -> "EKpQG..."
            if "solana_" in base_token_id:
                token_address = base_token_id.replace("solana_", "")
            else:
                continue
                
            # WSOLなどの共通通貨はスキップ
            if token_address == "So11111111111111111111111111111111111111112":
                continue
                
            # 二重検出の防止
            if token_address in notified_tokens:
                continue
                
            # 指標の取得 (DEX Screener APIを使って詳細なデータを補完する)
            token_details = fetch_dex_screener_details(token_address)
            if token_details:
                tokens_found.append(token_details)
                
        return tokens_found
    except Exception as e:
        print(f"【エラー】新規プールの取得に失敗しました: {e}")
        return []

def fetch_dex_screener_details(token_address: str) -> dict:
    """
    DEX Screener APIを使用して、トークンの流動性や時価総額の詳細を取得する。
    """
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        pairs = data.get("pairs", [])
        if not pairs:
            return None
            
        # 最も流動性の高いSolanaのペアを選択
        solana_pairs = [p for p in pairs if p.get("chainId") == "solana"]
        if not solana_pairs:
            return None
            
        # 流動性でソート
        solana_pairs.sort(key=lambda x: float(x.get("liquidity", {}).get("usd", 0)), reverse=True)
        best_pair = solana_pairs[0]
        
        # 必要な情報の整理
        mcap = float(best_pair.get("fdv", 0) or best_pair.get("marketCap", 0) or 0)
        liq = float(best_pair.get("liquidity", {}).get("usd", 0))
        vol = float(best_pair.get("volume", {}).get("h24", 0))
        
        base_token = best_pair.get("baseToken", {})
        
        return {
            "address": token_address,
            "name": base_token.get("name", "Unknown"),
            "symbol": base_token.get("symbol", "UNKNOWN"),
            "mcap": mcap,
            "liq": liq,
            "vol": vol
        }
    except Exception as e:
        print(f"【警告】DEX Screenerからの詳細取得に失敗({token_address}): {e}")
        return None

def monitor_loop():
    """
    監視のメインループ処理。1分ごとに新着プールをスキャン。
    """
    print("🚀 Solana AI ミームコイン監視システムを起動しました。")
    print(f"設定条件: 最低流動性 ${MIN_LIQUIDITY_USD:,.0f} | 最低取引高 ${MIN_VOLUME_24H:,.0f}")
    
    # 起動時に一度テストメッセージを送る（動作確認用）
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            test_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            test_payload = {"chat_id": TELEGRAM_CHAT_ID, "text": "🤖 Solana AI監視システムが起動しました！監視を開始します。"}
            requests.post(test_url, json=test_payload, timeout=10)
            print("【初期化】テスト通知をTelegramに送信しました。")
        except Exception as e:
            print(f"【警告】テスト通知の送信に失敗しました。設定を確認してください: {e}")
            
    while True:
        print(f"\n【{time.strftime('%H:%M:%S')}】新着プールをスキャン中...")
        new_tokens = get_new_solana_tokens()
        
        for token in new_tokens:
            addr = token["address"]
            liq = token["liq"]
            vol = token["vol"]
            
            # 一次審査（数値フィルター）
            if liq >= MIN_LIQUIDITY_USD and vol >= MIN_VOLUME_24H:
                print(f"✨ 条件合致トークン検知: {token['name']} ({token['symbol']}) - LIQ: ${liq:,.0f}, VOL: ${vol:,.0f}")
                print("  → AIによる二次審査を開始します...")
                
                # 二次審査（AI評価）
                ai_analysis = analyze_meme_with_ai(token)
                score = ai_analysis["score"]
                print(f"  → AI評価完了: スコア {score}/100")
                
                # スコアが70点以上ならTelegramに通知
                if score >= 70:
                    print("  → 🎉 高評価！Telegramに通知します。")
                    send_telegram_notification(token, ai_analysis)
                else:
                    print("  → 🥱 スコアが低いため、通知をスキップします。")
                    
                # スコアに関わらず、一度処理したトークンは二重通知防止リストに追加
                notified_tokens.add(addr)
            else:
                # 条件に満たないものはスキップ
                pass
                
        # APIレート制限を考慮し、60秒待機
        time.sleep(60)

if __name__ == "__main__":
    monitor_loop()
