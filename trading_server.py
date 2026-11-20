from flask import Flask, request, jsonify
import requests
import time
import json
import os
import re
import csv
from datetime import datetime
import threading

app = Flask(__name__)

# ==========================================
# CẤU HÌNH
# ==========================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "DÁN_TOKEN_CỦA_BẠN")
CHAT_ID = os.environ.get("CHAT_ID", "DÁN_CHAT_ID_CỦA_BẠN")

RISK_USD = 100.0
h1_trend_state = {}  # Trạng thái SuperTrend H1 cho từng mã

# Contract size theo từng mã (để tính volume từ RISK_USD)
CONTRACT_SIZE = {
    "XAUUSD": 100,
    "US100": 10,
    "GER40": 5,
    "JP225": 10,
    "BTCUSD": 1,
}

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        response = requests.post(url, json=payload, timeout=10)
        print(f"TELEGRAM RESPONSE: {response.text}")
    except Exception as e:
        print(f"LỖI GỬI TELEGRAM: {e}")

def write_to_csv(time_str, symbol, action, sig_type, entry, sl, tp1, tp2, tp3, vol, status):
    file_exists = os.path.isfile('trading_report.csv')
    with open('trading_report.csv', mode='a', newline='', encoding='utf-8') as file:
        writer = csv.writer(file)
        if not file_exists:
            writer.writerow(["Thời Gian", "Mã", "Hành Động", "Loại Tín Hiệu", "Trạng Thái",
                             "Entry", "SL", "TP1", "TP2", "TP3", "Volume"])
        clean_sig = sig_type.replace("✅ ", "").replace("⚠️ ", "").replace("⚡ ", "").replace("🔥 ", "")
        writer.writerow([time_str, symbol, action, clean_sig, status,
                         entry, round(sl, 2), round(tp1, 2), round(tp2, 2), round(tp3, 2), vol])

# ==========================================
# XỬ LÝ TÍN HIỆU M15 (TRIGGER)
# ==========================================
def process_signal_data(data):
    try:
        action = data.get("action", "")
        price = float(data.get("price", 0))
        high = float(data.get("high", 0))
        low = float(data.get("low", 0))
        st_trend = data.get("st_trend", "Không rõ")

        symbol = data.get("symbol", "UNKNOWN")
        if symbol == "UNKNOWN":
            if 3300 <= price <= 5000:
                symbol = "XAUUSD"
            elif price > 62100:
                symbol = "BTCUSD"
            elif 27000 <= price <= 35000:
                symbol = "US100"
            elif 20000 <= price <= 26900:
                symbol = "GER40"
            elif 55000 <= price <= 62000:
                symbol = "JP225"

        contract_size = CONTRACT_SIZE.get(symbol, 1)

        # Trạng thái SuperTrend H1 riêng của mã này (do /webhook_h1 cập nhật)
        current_h1_trend = h1_trend_state.get(symbol, 0)

        # XÁC ĐỊNH MUA/BÁN
        is_buy = False
        if "UP" in action:
            is_buy = True
        elif "DOWN" in action:
            is_buy = False
        elif "BUY" in action:
            is_buy = True
        elif "SELL" in action:
            is_buy = False

        # Gắn nhãn tín hiệu
        sig_label = "🔥 CẮT SLOW BASELINE"
        if "EARLY_WARNING" in action:
            sig_label = "⚠️ CẢNH BÁO SỚM"
        elif "RSI" in action:
            sig_label = "⚡ RSI"

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if is_buy:
            status_text = "✅ VÀO LỆNH NGAY (Thuận H1)" if current_h1_trend == 1 else "⏳ CHỜ XÁC NHẬN (Ngược H1)"
            sl = low - 0.5
            risk = price - sl
            if risk > 0:
                vol = round(RISK_USD / (risk * contract_size), 2)
                if vol < 0.01:
                    vol = 0.01
                tp1, tp2, tp3 = price + risk * 3, price + risk * 5, price + risk * 7
                msg = (f"{status_text} <b>{sig_label}: MUA {symbol}</b>\n"
                       f"📈 <b>SuperTrendM15:</b> {st_trend}\n"
                       f"💰 Entry: <code>{price}</code>\n"
                       f"🛑 SL: <code>{round(sl, 2)}</code>\n"
                       f"🎯 TP1: <code>{round(tp1, 2)}</code> | TP2: <code>{round(tp2, 2)}</code> | TP3: <code>{round(tp3, 2)}</code>\n"
                       f"📦 Vol: <code>{vol} lots</code>")
                send_telegram_message(msg)
                write_to_csv(now_str, symbol, "BUY", sig_label, price, sl, tp1, tp2, tp3, vol, status_text)
        else:
            status_text = "✅ VÀO LỆNH NGAY (Thuận H1)" if current_h1_trend == -1 else "⏳ CHỜ XÁC NHẬN (Ngược H1)"
            sl = high + 0.5
            risk = sl - price
            if risk > 0:
                vol = round(RISK_USD / (risk * contract_size), 2)
                if vol < 0.01:
                    vol = 0.01
                tp1, tp2, tp3 = price - risk * 3, price - risk * 5, price - risk * 7
                msg = (f"{status_text} <b>{sig_label}: BÁN {symbol}</b>\n"
                       f"📉 <b>SuperTrendM15:</b> {st_trend}\n"
                       f"💰 Entry: <code>{price}</code>\n"
                       f"🛑 SL: <code>{round(sl, 2)}</code>\n"
                       f"🎯 TP1: <code>{round(tp1, 2)}</code> | TP2: <code>{round(tp2, 2)}</code> | TP3: <code>{round(tp3, 2)}</code>\n"
                       f"📦 Vol: <code>{vol} lots</code>")
                send_telegram_message(msg)
                write_to_csv(now_str, symbol, "SELL", sig_label, price, sl, tp1, tp2, tp3, vol, status_text)
    except Exception as e:
        print(f"Lỗi xử lý: {e}")

# ==========================================
# CỔNG NHẬN TÍN HIỆU H1 (BIAS - SUPERTREND H1)
# ==========================================
@app.route('/webhook_h1', methods=['POST'])
def webhook_h1():
    """
    Nhận alert H1 từ TradingView.
    Hỗ trợ 2 format:
    - Cũ: action = "H1_CROSS_UP" / "H1_CROSS_DOWN"
    - Mới: action = "H1_ST_CROSS_UP" / "H1_ST_CROSS_DOWN"
    """
    try:
        data = request.get_json(force=True) or {}
        action = data.get("action", "")
        symbol = data.get("symbol", "UNKNOWN")

        if symbol == "UNKNOWN":
            return jsonify({"status": "error", "message": "missing symbol"}), 400

        if "H1_CROSS_UP" in action or "H1_ST_CROSS_UP" in action:
            h1_trend_state[symbol] = 1
            send_telegram_message(f"📈 <b>H1 BÁO ĐỘNG ({symbol}):</b> SuperTrend H1 đổi sang TĂNG.")
        elif "H1_CROSS_DOWN" in action or "H1_ST_CROSS_DOWN" in action:
            h1_trend_state[symbol] = -1
            send_telegram_message(f"📉 <b>H1 BÁO ĐỘNG ({symbol}):</b> SuperTrend H1 đổi sang GIẢM.")

        return jsonify({"status": "success", "current_trend": h1_trend_state.get(symbol, 0)})
    except Exception as e:
        print(f"LỖI webhook_h1: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# ==========================================
# CỔNG NHẬN TÍN HIỆU M15 (TRIGGER)
# ==========================================
@app.route('/webhook_m15', methods=['POST'])
def webhook_m15():
    try:
        data = request.get_json(force=True) or {}
        # Chạy ngầm để trả lời TradingView ngay lập tức
        threading.Thread(target=process_signal_data, args=(data,), daemon=True).start()
        return jsonify({"status": "success - Queued"})
    except Exception as e:
        print(f"LỖI webhook_m15: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# ==========================================
# HEALTH CHECK
# ==========================================
@app.route('/', methods=['GET'])
def home():
    return jsonify({
        "status": "running",
        "h1_states": h1_trend_state,
        "time": datetime.now().isoformat()
    })

if __name__ == '__main__':
    # Render cần đọc PORT từ biến môi trường
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
