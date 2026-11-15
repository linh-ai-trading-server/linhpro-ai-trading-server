from flask import Flask, request, jsonify
import threading
import requests
import time
import json
import os
import re
import csv
from datetime import datetime
from google import genai

app = Flask(__name__)

# ==========================================
# 1. CẤU HÌNH TELEGRAM & AI
# ==========================================
import os

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "DÁN_TOKEN_CỦA_BẠN")
CHAT_ID = os.environ.get("CHAT_ID", "DÁN_CHAT_ID_CỦA_BẠN")
RAW_API_KEY = os.environ.get("RAW_API_KEY", "DÁN_API_KEY_CỦA_BẠN")
clean_key = re.sub(r'\s+', '', RAW_API_KEY).strip()
client = genai.Client(api_key=clean_key)

# ==========================================
# 2. CẤU HÌNH QUẢN LÝ RỦI RO
# ==========================================
RISK_USD = 100.0
h1_trend_state = 0
last_h1_update_time = 0

# ==========================================
# 3. CÁC HÀM TIỆN ÍCH
# ==========================================
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}

    try:
        # Thử gửi với cấu hình mạng linh hoạt hơn
        session = requests.Session()
        # Bỏ qua kiểm tra SSL (đôi khi proxy bị lỗi SSL)
        session.verify = False

        # Thêm timeout và thử lại tự động 3 lần nếu gặp lỗi mạng
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("https://", adapter)

        response = session.post(url, json=payload, timeout=10)
        print(f"TELEGRAM RESPONSE: {response.text}")
    except Exception as e:
        print(f"LỖI GỬI TELEGRAM: {e}")

def write_to_csv(time_str, symbol, action, sig_type, entry, sl, tp1, tp2, tp3, vol, status):
    file_exists = os.path.isfile('trading_report.csv')
    with open('trading_report.csv', mode='a', newline='', encoding='utf-8') as file:
        writer = csv.writer(file)
        if not file_exists:
            writer.writerow(["Thời Gian", "Mã", "Hành Động", "Loại Tín Hiệu", "Trạng Thái", "Entry", "SL", "TP1", "TP2", "TP3", "Volume"])

        clean_sig = sig_type.replace("✅ ", "").replace("⚠️ ", "").replace("⚡ ", "").replace("🔥 ", "")
        writer.writerow([time_str, symbol, action, clean_sig, status, entry, round(sl, 2), round(tp1, 2), round(tp2, 2), round(tp3, 2), vol])

def ask_ai_analyst(symbol, action, price, atr, h1_trend):
    prompt = f"""
    You are a professional Technical Analyst. Analyze this signal for {symbol}:
    - Action: {action} (BUY or SELL)
    - Price: {price}
    - H1 Trend: {h1_trend} (1 is Up, -1 is Down)
    - ATR: {atr}
    Return EXACTLY this JSON format (no extra text):
    {{"score": 85, "advice": "Good signal...", "warning": "None"}}
    """
    models_to_try = ['gemini-3.5-flash', 'gemini-3.8-flash', 'gemini-2.5-flash']
    for model_name in models_to_try:
        for attempt in range(2):
            try:
                response = client.models.generate_content(model=model_name, contents=prompt)
                clean_text = response.text.strip().replace("```json", "").replace("```", "")
                return json.loads(clean_text)
            except Exception:
                time.sleep(1)
    return {"score": 0, "advice": "Khong the ket noi AI luc nay.", "warning": "Loi AI"}

# ==========================================
# 4. CÁC CỔNG NHẬN TÍN HIỆU TỪ TRADINGVIEW
# ==========================================
@app.route('/webhook_h1', methods=['POST'])
def webhook_h1():
    global h1_trend_state, last_h1_update_time
    try:
        data = request.get_json()
        action = data.get("action")

        # Lấy tên mã từ JSON (nếu có), nếu không có thì ghi là UNKNOWN
        symbol = data.get("symbol", "UNKNOWN")

        if "H1_TREND_UP" in action or "H1_CROSS_UP" in action:
            h1_trend_state = 1
            last_h1_update_time = time.time()
            send_telegram_message(f"📈 <b>H1 BÁO ĐỘNG ({symbol}):</b> SuperTrend đổi sang TĂNG.\n👉 Đang chờ tín hiệu M15...")
        elif "H1_TREND_DOWN" in action or "H1_CROSS_DOWN" in action:
            h1_trend_state = -1
            last_h1_update_time = time.time()
            send_telegram_message(f"📉 <b>H1 BÁO ĐỘNG ({symbol}):</b> SuperTrend đổi sang GIẢM.\n👉 Đang chờ tín hiệu M15...")

        return jsonify({"status": "success", "current_trend": h1_trend_state, "symbol": symbol})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


import threading # Đảm bảo đã import ở đầu file

@app.route('/webhook_m15', methods=['POST'])
def webhook_m15():
    global h1_trend_state, last_h1_update_time
    try:
        data = request.get_json()
        action = data.get("action")
        price = float(data.get("price", 0))
        high = float(data.get("high", 0))
        low = float(data.get("low", 0))
        st_trend = data.get("st_trend", "Không rõ")

        symbol = data.get("symbol", "UNKNOWN")
        if symbol == "UNKNOWN":
            if 3300 <= price <= 5000: symbol = "XAUUSD"
            elif price > 64100: symbol = "BTCUSD"
            elif 27300 <= price <= 35000: symbol = "US100"
            elif 20000 <= price <= 27200: symbol = "GER40"
            elif 55000 <= price <= 64000: symbol = "JP225"
    except Exception as e:
        return jsonify({"status": "error", "message": "Invalid data"})

    # Hàm xử lý chạy ngầm (Không làm treo Server)
    def process_and_send():
        try:
            if symbol == "XAUUSD": contract_size = 100
            elif symbol == "US100": contract_size = 10
            elif symbol == "GER40": contract_size = 5
            elif symbol == "JP225": contract_size = 10
            else: contract_size = 1

            is_buy = "BUY" in action
            if is_buy:
                status_text = "✅ VÀO LỆNH NGAY (Thuận H1)" if h1_trend_state == 1 else "⏳ CHỜ XÁC NHẬN (Ngược H1)"
                sl = low - 0.5
                risk = price - sl
                if risk > 0:
                    vol = round(RISK_USD / (risk * contract_size), 2)
                    if vol < 0.01: vol = 0.01
                    tp1, tp2, tp3 = price + risk*3, price + risk*5, price + risk*7
                    msg = (f"{status_text} <b>{action}: MUA {symbol}</b>\n📈 <b>SuperTrend:</b> {st_trend}\n"
                           f"💰 Entry: <code>{price}</code>\n🛑 SL: <code>{round(sl,2)}</code>\n"
                           f"🎯 TP1: <code>{round(tp1,2)}</code> | TP2: <code>{round(tp2,2)}</code> | TP3: <code>{round(tp3,2)}</code>\n"
                           f"📦 Vol: <code>{vol} lots</code>")
                    send_telegram_message(msg)
            else:
                status_text = "✅ VÀO LỆNH NGAY (Thuận H1)" if h1_trend_state == -1 else "⏳ CHỜ XÁC NHẬN (Ngược H1)"
                sl = high + 0.5
                risk = sl - price
                if risk > 0:
                    vol = round(RISK_USD / (risk * contract_size), 2)
                    if vol < 0.01: vol = 0.01
                    tp1, tp2, tp3 = price - risk*3, price - risk*5, price - risk*7
                    msg = (f"{status_text} <b>{action}: BÁN {symbol}</b>\n📉 <b>SuperTrend:</b> {st_trend}\n"
                           f"💰 Entry: <code>{price}</code>\n🛑 SL: <code>{round(sl,2)}</code>\n"
                           f"🎯 TP1: <code>{round(tp1,2)}</code> | TP2: <code>{round(tp2,2)}</code> | TP3: <code>{round(tp3,2)}</code>\n"
                           f"📦 Vol: <code>{vol} lots</code>")
                    send_telegram_message(msg)
        except Exception as e:
            print(f"Lỗi xử lý tín hiệu: {e}")

    # Kích hoạt chạy ngầm và trả lời TradingView ngay lập tức
    threading.Thread(target=process_and_send).start()
    return jsonify({"status": "success - Queued"})

    # Phân luồng tín hiệu
    if "EARLY_WARNING" in action:
        if "BUY" in action: process_and_send(True, "⚠️ CẢNH BÁO SỚM", st_trend)
        elif "SELL" in action: process_and_send(False, "⚠️ CẢNH BÁO SỚM", st_trend)
        return jsonify({"status": "success - Early Warning"})

    if "RSI" in action:
        if "BUY" in action: process_and_send(True, "⚡ RSI", st_trend)
        elif "SELL" in action: process_and_send(False, "⚡ RSI", st_trend)
        return jsonify({"status": "success - RSI"})

    if "BASE_SLOW" in action:
        if "BUY" in action: process_and_send(True, "🔥 CẮT SLOW BASELINE", st_trend)
        elif "SELL" in action: process_and_send(False, "🔥 CẮT SLOW BASELINE", st_trend)
        return jsonify({"status": "success - Base Slow"})

    return jsonify({"status": "success"})

    # Hàm xử lý tính toán và gửi tin nhắn
    def process_and_send():
        if symbol == "XAUUSD": contract_size = 100
        elif symbol == "US100": contract_size = 10
        elif symbol == "GER40": contract_size = 5
        elif symbol == "JP225": contract_size = 10
        else: contract_size = 1

        is_buy = "BUY" in action
        if is_buy:
            status_text = "✅ VÀO LỆNH NGAY (Thuận H1)" if h1_trend_state == 1 else "⏳ CHỜ XÁC NHẬN (Ngược H1)"
            sl = low - 0.5
            risk = price - sl
            if risk > 0:
                vol = round(RISK_USD / (risk * contract_size), 2)
                if vol < 0.01: vol = 0.01
                tp1, tp2, tp3 = price + risk*3, price + risk*5, price + risk*7
                msg = (f"{status_text} <b>{action}: MUA {symbol}</b>\n📈 <b>SuperTrend:</b> {st_trend}\n"
                       f"💰 Entry: <code>{price}</code>\n🛑 SL: <code>{round(sl,2)}</code>\n"
                       f"🎯 TP1: <code>{round(tp1,2)}</code> | TP2: <code>{round(tp2,2)}</code> | TP3: <code>{round(tp3,2)}</code>\n"
                       f"📦 Vol: <code>{vol} lots</code>")
                send_telegram_message(msg)
                write_to_csv(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), symbol, "BUY", action, price, sl, tp1, tp2, tp3, vol, status_text)
        else:
            status_text = "✅ VÀO LỆNH NGAY (Thuận H1)" if h1_trend_state == -1 else "⏳ CHỜ XÁC NHẬN (Ngược H1)"
            sl = high + 0.5
            risk = sl - price
            if risk > 0:
                vol = round(RISK_USD / (risk * contract_size), 2)
                if vol < 0.01: vol = 0.01
                tp1, tp2, tp3 = price - risk*3, price - risk*5, price - risk*7
                msg = (f"{status_text} <b>{action}: BÁN {symbol}</b>\n📉 <b>SuperTrend:</b> {st_trend}\n"
                       f"💰 Entry: <code>{price}</code>\n🛑 SL: <code>{round(sl,2)}</code>\n"
                       f"🎯 TP1: <code>{round(tp1,2)}</code> | TP2: <code>{round(tp2,2)}</code> | TP3: <code>{round(tp3,2)}</code>\n"
                       f"📦 Vol: <code>{vol} lots</code>")
                send_telegram_message(msg)
                write_to_csv(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), symbol, "SELL", action, price, sl, tp1, tp2, tp3, vol, status_text)

    # ==========================================
    # CHẠY NGẦM (THREADING) ĐỂ TRẢ LỜI TRADINGVIEW NGAY LẬP TỨC
    # ==========================================
    threading.Thread(target=process_and_send).start()

    return jsonify({"status": "success - Queued"})

        # Phân luồng tín hiệu
    if "EARLY_WARNING" in action:
        if "BUY" in action:
            # Chạy ngầm hàm process_signal để trả lời TradingView ngay lập tức
            threading.Thread(target=process_signal, args=(True, "⚠️ CẢNH BÁO SỚM (MUA)", st_trend)).start()
        elif "SELL" in action:
            threading.Thread(target=process_signal, args=(False, "⚠️ CẢNH BÁO SỚM (BÁN)", st_trend)).start()
        return jsonify({"status": "success - Early Warning"})

    if "RSI" in action:
        if "BUY" in action:
            threading.Thread(target=process_signal, args=(True, "⚡ RSI (MUA)", st_trend)).start()
        elif "SELL" in action:
            threading.Thread(target=process_signal, args=(False, "⚡ RSI (BÁN)", st_trend)).start()
        return jsonify({"status": "success - RSI"})

    if "BASE_SLOW" in action:
        if "BUY" in action:
            threading.Thread(target=process_signal, args=(True, "🔥 CẮT SLOW BASELINE (MUA)", st_trend)).start()
        elif "SELL" in action:
            threading.Thread(target=process_signal, args=(False, "🔥 CẮT SLOW BASELINE (BÁN)", st_trend)).start()
        return jsonify({"status": "success - Base Slow"})

@app.route('/')
def home():
    return "AI Server is running perfectly!"

if __name__ == '__main__':
    app.run()