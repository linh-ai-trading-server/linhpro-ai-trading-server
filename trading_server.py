from flask import Flask, request, jsonify
import requests
import time
import json
import os
import re
import csv
from datetime import datetime
import threading
from google import genai

app = Flask(__name__)

# ==========================================
# CẤU HÌNH
# ==========================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "DÁN_TOKEN_CỦA_BẠN")
CHAT_ID = os.environ.get("CHAT_ID", "DÁN_CHAT_ID_CỦA_BẠN")
RAW_API_KEY = os.environ.get("RAW_API_KEY", "DÁN_API_KEY_CỦA_BẠN")
clean_key = re.sub(r'\s+', '', RAW_API_KEY).strip()
client = genai.Client(api_key=clean_key)

RISK_USD = 100.0
h1_trend_state = {}  # Từ điển lưu trạng thái H1 cho từng mã

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        response = requests.post(url, json=payload)
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

# ==========================================
# HÀM XỬ LÝ CHUNG CHO TÍN HIỆU M15
# ==========================================
def process_signal_data(data):
    try:
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

        if symbol == "XAUUSD": contract_size = 100
        elif symbol == "US100": contract_size = 10
        elif symbol == "GER40": contract_size = 5
        elif symbol == "JP225": contract_size = 10
        else: contract_size = 1

        # Lấy trạng thái H1 riêng của mã này
        current_h1_trend = h1_trend_state.get(symbol, 0)

        # XÁC ĐỊNH MUA/BÁN CHÍNH XÁC
        is_buy = False
        if "BASE_SLOW" in action:
            if "UP" in action: 
                is_buy = True
            elif "DOWN" in action: 
                is_buy = False
        elif "EARLY_WARNING" in action or "RSI" in action:
            if "BUY" in action: 
                is_buy = True
            elif "SELL" in action: 
                is_buy = False
        else:
            # Nếu không khớp các trường hợp trên, tự động dò chữ BUY/SELL
            if "BUY" in action: is_buy = True
            elif "SELL" in action: is_buy = False

        # Gắn nhãn tín hiệu
        sig_label = "🔥 CẮT SLOW BASELINE"
        if "EARLY_WARNING" in action: sig_label = "⚠️ CẢNH BÁO SỚM"
        elif "RSI" in action: sig_label = "⚡ RSI"

        if is_buy:
            status_text = "✅ VÀO LỆNH NGAY (Thuận H1)" if current_h1_trend == 1 else "⏳ CHỜ XÁC NHẬN (Ngược H1)"
            sl = low - 0.5
            risk = price - sl
            if risk > 0:
                vol = round(RISK_USD / (risk * contract_size), 2)
                if vol < 0.01: vol = 0.01
                tp1, tp2, tp3 = price + risk*3, price + risk*5, price + risk*7
                msg = (f"{status_text} <b>{sig_label}: MUA {symbol}</b>\n📈 <b>SuperTrend:</b> {st_trend}\n"
                       f"💰 Entry: <code>{price}</code>\n🛑 SL: <code>{round(sl,2)}</code>\n"
                       f"🎯 TP1: <code>{round(tp1,2)}</code> | TP2: <code>{round(tp2,2)}</code> | TP3: <code>{round(tp3,2)}</code>\n"
                       f"📦 Vol: <code>{vol} lots</code>")
                send_telegram_message(msg)
                write_to_csv(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), symbol, "BUY", sig_label, price, sl, tp1, tp2, tp3, vol, status_text)
        else:
            status_text = "✅ VÀO LỆNH NGAY (Thuận H1)" if current_h1_trend == -1 else "⏳ CHỜ XÁC NHẬN (Ngược H1)"
            sl = high + 0.5
            risk = sl - price
            if risk > 0:
                vol = round(RISK_USD / (risk * contract_size), 2)
                if vol < 0.01: vol = 0.01
                tp1, tp2, tp3 = price - risk*3, price - risk*5, price - risk*7
                msg = (f"{status_text} <b>{sig_label}: BÁN {symbol}</b>\n📉 <b>SuperTrend:</b> {st_trend}\n"
                       f"💰 Entry: <code>{price}</code>\n🛑 SL: <code>{round(sl,2)}</code>\n"
                       f"🎯 TP1: <code>{round(tp1,2)}</code> | TP2: <code>{round(tp2,2)}</code> | TP3: <code>{round(tp3,2)}</code>\n"
                       f"📦 Vol: <code>{vol} lots</code>")
                send_telegram_message(msg)
                write_to_csv(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), symbol, "SELL", sig_label, price, sl, tp1, tp2, tp3, vol, status_text)
    except Exception as e:
        print(f"Lỗi xử lý: {e}")

# ==========================================
# CÁC CỔNG NHẬN TÍN HIỆU
# ==========================================
@app.route('/webhook_h1', methods=['POST'])
def webhook_h1():
    data = request.get_json()
    action = data.get("action")
    symbol = data.get("symbol", "UNKNOWN")
    
    if "H1_TREND_UP" in action or "H1_CROSS_UP" in action:
        h1_trend_state[symbol] = 1
        send_telegram_message(f"📈 <b>H1 BÁO ĐỘNG ({symbol}):</b> SuperTrend đổi sang TĂNG.")
    elif "H1_TREND_DOWN" in action or "H1_CROSS_DOWN" in action:
        h1_trend_state[symbol] = -1
        send_telegram_message(f"📉 <b>H1 BÁO ĐỘNG ({symbol}):</b> SuperTrend đổi sang GIẢM.")
        
    return jsonify({"status": "success", "current_trend": h1_trend_state.get(symbol, 0)})

@app.route('/webhook_m15', methods=['POST'])
def webhook_m15():
    data = request.get_json()
    # Chạy ngầm để trả lời TradingView ngay lập tức
    threading.Thread(target=process_signal_data, args=(data,)).start()
    return jsonify({"status": "success - Queued"})

@app.route('/', methods=['GET'])
def home():
    return "AI Server is running perfectly!"

if __name__ == '__main__':
    app.run()
