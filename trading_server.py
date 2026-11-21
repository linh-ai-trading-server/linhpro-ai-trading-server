from flask import Flask, request, jsonify
import requests
import json
import os
import csv
import threading
from datetime import datetime, timezone, timedelta

app = Flask(__name__)

# ==========================================
# CẤU HÌNH
# ==========================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "DÁN_TOKEN_CỦA_BẠN")
CHAT_ID = os.environ.get("CHAT_ID", "DÁN_CHAT_ID_CỦA_BẠN")

RISK_USD = float(os.environ.get("RISK_USD", 100))
MAX_LOT = float(os.environ.get("MAX_LOT", 3.0))        # Trần volume an toàn (chỉnh theo vốn của bạn)
MIN_RISK_ATR = float(os.environ.get("MIN_RISK_ATR", 0.3))  # SL tối thiểu = 0.3 x ATR, gần hơn thì bỏ tín hiệu

# Tỷ giá quy đổi sang USD (chỉnh bằng biến môi trường trên Render khi tỷ giá lệch nhiều)
USDJPY = float(os.environ.get("USDJPY", 158.8))
EURUSD = float(os.environ.get("EURUSD", 1.16))

# Giá trị (USD) khi giá đi 1.00 điểm với 1 lot — đã đối chiếu với lệnh thật trên tài khoản của bạn
CONTRACT_SIZE = {
    "XAUUSD": 100,          # đã kiểm tra: 0.01 lot, giá đi 3.25 -> 3.25 USD
    "US100": 1,             # đã kiểm tra: 4 lot, giá đi 162.45 -> 649.80 USD
    "GER40": 1 * EURUSD,    # đã kiểm tra: 1 EUR/điểm/lot (~1.16 USD)
    "JP225": 10 / USDJPY,   # đã kiểm tra: ~0.063 USD/điểm/lot (10 JPY)
    "BTCUSD": 1,            # đã kiểm tra: 0.24 lot, giá đi 803.66 -> 192.88 USD
}

# Trần volume riêng cho từng mã (mã không có ở đây dùng MAX_LOT chung)
MAX_LOT_BY_SYMBOL = {"JP225": 10.0}  # 1 lot JP225 chỉ ~0.063 USD/điểm nên cần trần cao hơn

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.environ.get("TZ_NAME", "Asia/Ho_Chi_Minh"))
except Exception:
    TZ = timezone(timedelta(hours=7))


def now_local():
    return datetime.now(TZ)


STATE_FILE = "h1_state.json"
CSV_FILE = "trading_report.csv"
_lock = threading.Lock()


# ==========================================
# TRẠNG THÁI H1 (lưu file để sống qua lần restart nhẹ)
# Lưu ý: ổ đĩa gói free của Render bị xóa khi redeploy/ngủ lâu,
# khi đó trạng thái về 0 (chưa xác định) cho đến khi H1 đổi xu hướng.
# ==========================================
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return {k: int(v) for k, v in json.load(f).items()}
    except Exception:
        return {}


def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(h1_trend_state, f)
    except Exception as e:
        print(f"LỖI LƯU STATE: {e}")


h1_trend_state = load_state()  # {"XAUUSD": 1 (tăng) | -1 (giảm) | 0 (chưa rõ)}


# ==========================================
# TIỆN ÍCH
# ==========================================
def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        response = requests.post(url, json=payload, timeout=10)
        print(f"TELEGRAM RESPONSE: {response.text}")
    except Exception as e:
        print(f"LỖI GỬI TELEGRAM: {e}")


def write_to_csv(time_str, symbol, action, sig_type, entry, sl, tp1, tp2, tp3, vol, status):
    with _lock:
        file_exists = os.path.isfile(CSV_FILE)
        with open(CSV_FILE, mode='a', newline='', encoding='utf-8') as file:
            writer = csv.writer(file)
            if not file_exists:
                writer.writerow(["Thời Gian", "Mã", "Hành Động", "Loại Tín Hiệu", "Trạng Thái",
                                 "Entry", "SL", "TP1", "TP2", "TP3", "Volume"])
            clean_sig = sig_type
            for icon in ("✅ ", "⚠️ ", "⚡ ", "🔥 "):
                clean_sig = clean_sig.replace(icon, "")
            writer.writerow([time_str, symbol, action, clean_sig, status,
                             entry, round(sl, 2), round(tp1, 2), round(tp2, 2), round(tp3, 2), vol])


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def normalize_symbol(raw):
    # "OANDA:XAUUSD" -> "XAUUSD"
    return str(raw or "").strip().upper().split(":")[-1]


def parse_h1_action(action):
    """H1_TREND_UP / H1_CROSS_UP / H1_ST_CROSS_UP ... -> 1 | -1 ; không phải H1 -> None"""
    a = action.upper()
    if a.startswith("H1_"):
        if a.endswith("_UP"):
            return 1
        if a.endswith("_DOWN"):
            return -1
    return None


def parse_direction(action):
    a = action.upper()
    if a.endswith("_BUY") or a.endswith("_UP"):
        return "BUY"
    if a.endswith("_SELL") or a.endswith("_DOWN"):
        return "SELL"
    return None


def handle_h1_trend(symbol, trend, data=None):
    """Chỉ cập nhật trạng thái H1 và báo tin, KHÔNG tạo thẻ vào lệnh."""
    data = data or {}
    if not symbol:
        print("H1 bỏ qua: thiếu symbol")
        return
    with _lock:
        old = h1_trend_state.get(symbol, 0)
        h1_trend_state[symbol] = trend
        save_state()
    if old == trend:
        return  # không đổi thì không báo lại

    names = {1: "TĂNG", -1: "GIẢM", 0: "CHƯA RÕ"}
    icon = "📈" if trend == 1 else "📉"
    lines = [f"{icon} <b>H1 BÁO ĐỘNG ({symbol}):</b> SuperTrend H1 đổi sang <b>{names[trend]}</b>"
             f" (trước đó: {names[old]})."]

    price = to_float(data.get("price"))
    atr = to_float(data.get("atr"))
    if price > 0:
        info = f"💰 Giá đóng nến H1: <code>{price}</code>"
        if atr > 0:
            info += f" | ATR14: <code>{round(atr, 2)}</code>"
        lines.append(info)
    st_m15 = data.get("st_trend")
    if st_m15:
        lines.append(f"📊 SuperTrend M15 hiện tại: {st_m15}")
    lines.append(f"🕒 {now_local().strftime('%d/%m %H:%M')} (nến H1 đã đóng)")
    send_telegram_message("\n".join(lines))


# ==========================================
# XỬ LÝ TÍN HIỆU M15 (TRIGGER)
# ==========================================
def process_signal_data(data):
    try:
        action = str(data.get("action", ""))
        symbol = normalize_symbol(data.get("symbol"))

        # 1) Tín hiệu xu hướng H1 (kể cả khi TradingView gửi nhầm vào cổng M15)
        # H1 chỉ được nhận qua /webhook_h1 (alert trên chart 1h, nến đã đóng).
        # Tin H1 đến từ cổng M15 có thể là báo giả do nến H1 chưa đóng -> bỏ qua.
        if parse_h1_action(action) is not None:
            print(f"BỎ QUA {action} từ /webhook_m15 (H1 chỉ nhận qua /webhook_h1)")
            return

        # 2) Tín hiệu vào lệnh
        direction = parse_direction(action)
        if direction is None:
            print(f"BỎ QUA action không nhận diện: {action}")
            return

        if symbol not in CONTRACT_SIZE:
            print(f"BỎ QUA mã không có contract size: '{symbol}' (action={action})")
            return

        price = to_float(data.get("price"))
        high = to_float(data.get("high"))
        low = to_float(data.get("low"))
        atr = to_float(data.get("atr"))
        st_trend = data.get("st_trend", "Không rõ")

        # Kiểm tra dữ liệu: thiếu/0 thì tuyệt đối không tính lệnh
        if price <= 0 or high <= 0 or low <= 0 or high < low or not (low <= price <= high):
            print(f"BỎ QUA dữ liệu không hợp lệ: {data}")
            return

        contract_size = CONTRACT_SIZE[symbol]
        current_h1_trend = h1_trend_state.get(symbol, 0)

        sig_label = "🔥 CẮT SLOW BASELINE"
        if "EARLY_WARNING" in action:
            sig_label = "⚠️ CẢNH BÁO SỚM"
        elif "RSI" in action:
            sig_label = "⚡ RSI"

        # Trạng thái so với H1
        aligned = (current_h1_trend == 1 and direction == "BUY") or \
                  (current_h1_trend == -1 and direction == "SELL")
        if current_h1_trend == 0:
            status_text = "❔ H1 CHƯA XÁC ĐỊNH (Chờ xác nhận)"
        elif aligned:
            status_text = "✅ VÀO LỆNH NGAY (Thuận H1)"
        else:
            status_text = "⏳ CHỜ XÁC NHẬN (Ngược H1)"

        if direction == "BUY":
            sl = low - 0.5
            risk = price - sl
            sign = 1
            side_txt, icon = "MUA", "📈"
        else:
            sl = high + 0.5
            risk = sl - price
            sign = -1
            side_txt, icon = "BÁN", "📉"

        if risk <= 0:
            return

        # SL quá sát so với ATR -> volume sẽ phình to, bỏ tín hiệu
        if atr > 0 and risk < MIN_RISK_ATR * atr:
            send_telegram_message(
                f"⚠️ Bỏ qua {side_txt} {symbol} ({sig_label}): SL quá gần "
                f"(risk {round(risk, 2)} < {MIN_RISK_ATR} x ATR {round(atr, 2)})")
            return

        raw_vol = RISK_USD / (risk * contract_size)
        vol = max(round(raw_vol, 2), 0.01)
        capped_note = ""
        max_lot = MAX_LOT_BY_SYMBOL.get(symbol, MAX_LOT)
        if vol > max_lot:
            vol = max_lot
            capped_note = f"\n⚠️ Volume đã bị giới hạn ở {max_lot} lots"

        tp1, tp2, tp3 = (price + sign * risk * k for k in (3, 5, 7))
        now_str = now_local().strftime("%Y-%m-%d %H:%M:%S")

        msg = (f"{status_text} <b>{sig_label}: {side_txt} {symbol}</b>\n"
               f"{icon} <b>SuperTrendM15:</b> {st_trend}\n"
               f"💰 Entry: <code>{price}</code>\n"
               f"🛑 SL: <code>{round(sl, 2)}</code>\n"
               f"🎯 TP1: <code>{round(tp1, 2)}</code> | TP2: <code>{round(tp2, 2)}</code> | TP3: <code>{round(tp3, 2)}</code>\n"
               f"📦 Vol: <code>{vol} lots</code>{capped_note}")
        send_telegram_message(msg)
        write_to_csv(now_str, symbol, direction, sig_label, price, sl, tp1, tp2, tp3, vol, status_text)
    except Exception as e:
        print(f"Lỗi xử lý: {e}")


# ==========================================
# CỔNG NHẬN TÍN HIỆU H1 (BIAS - SUPERTREND H1)
# ==========================================
@app.route('/webhook_h1', methods=['POST'])
def webhook_h1():
    try:
        data = request.get_json(force=True) or {}
        action = str(data.get("action", ""))
        symbol = normalize_symbol(data.get("symbol"))

        if not symbol:
            return jsonify({"status": "error", "message": "missing symbol"}), 400

        trend = parse_h1_action(action)
        if trend is None:
            return jsonify({"status": "ignored", "action": action})

        handle_h1_trend(symbol, trend, data)
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
        "time": now_local().isoformat()
    })


if __name__ == '__main__':
    # Render cần đọc PORT từ biến môi trường
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
