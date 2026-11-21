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

# Forex chuẩn (1 lot = 100.000 đơn vị) — CHƯA kiểm tra trên tài khoản của bạn, xác nhận trước khi dùng thật
CONTRACT_SIZE["EURUSD"] = 100000
CONTRACT_SIZE["USDJPY"] = 100000 / USDJPY
# US30, NEARUSD, USOIL: chưa có số đã kiểm tra -> tin vẫn gửi nhưng Vol ghi "chưa cấu hình".
# Khi đã xác nhận, đặt trên Render, ví dụ: CONTRACT_SIZE_JSON={"US30": 1, "USOIL": 1000}
try:
    CONTRACT_SIZE.update({k.upper(): float(v) for k, v in json.loads(os.environ.get("CONTRACT_SIZE_JSON", "{}")).items()})
except Exception as e:
    print(f"LỖI CONTRACT_SIZE_JSON: {e}")

TF_NAMES = {"1": "M1", "5": "M5", "15": "M15", "30": "M30", "60": "H1", "240": "H4", "D": "D1", "1D": "D1"}
SYMBOL_ALIAS = {"WTICOUSD": "USOIL", "USOILSPOT": "USOIL", "CL1!": "USOIL", "US30USD": "US30", "DJI": "US30"}

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
            for icon in ("✅ ", "⚠️ ", "⚡ ", "🔥 ", "🎯 "):
                clean_sig = clean_sig.replace(icon, "")
            writer.writerow([time_str, symbol, action, clean_sig, status,
                             entry, round(sl, 6), round(tp1, 6), round(tp2, 6), round(tp3, 6), vol])


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def normalize_symbol(raw):
    # "OANDA:XAUUSD" -> "XAUUSD"
    sym = str(raw or "").strip().upper().split(":")[-1]
    return SYMBOL_ALIAS.get(sym, sym)


def fmt_price(x, ref):
    """Số chữ số thập phân theo mức giá (EURUSD 5 số, USDJPY 3 số, chỉ số/vàng/BTC 2 số)."""
    ref = abs(ref)
    nd = 2 if ref >= 1000 else 3 if ref >= 100 else 4 if ref >= 10 else 5
    return f"{round(x, nd):.{nd}f}"


def parse_h1_action(action):
    """H1_/H4_ ..._UP / ..._DOWN -> 1 | -1 ; không phải tín hiệu xu hướng -> None"""
    a_ = action.upper()
    if a_.startswith("H1_") or a_.startswith("H4_"):
        if a_.endswith("_UP"):
            return 1
        if a_.endswith("_DOWN"):
            return -1
    return None


def trend_tf_of_action(action):
    return "H4" if action.upper().startswith("H4_") else "H1"


def parse_direction(action):
    a = action.upper()
    if a.endswith("_BUY") or a.endswith("_UP"):
        return "BUY"
    if a.endswith("_SELL") or a.endswith("_DOWN"):
        return "SELL"
    return None


def handle_h1_trend(symbol, trend, data=None, tf_name="H1"):
    """Chỉ cập nhật trạng thái xu hướng lớn (H1 hoặc H4) và báo tin, KHÔNG tạo thẻ vào lệnh."""
    data = data or {}
    if not symbol:
        print("Xu hướng bỏ qua: thiếu symbol")
        return
    with _lock:
        old = h1_trend_state.get(symbol, 0)
        h1_trend_state[symbol] = trend
        save_state()
    if old == trend:
        return  # không đổi thì không báo lại

    names = {1: "TĂNG", -1: "GIẢM", 0: "CHƯA RÕ"}
    icon = "📈" if trend == 1 else "📉"
    lines = [f"{icon} <b>{tf_name} BÁO ĐỘNG ({symbol}):</b> SuperTrend {tf_name} đổi sang <b>{names[trend]}</b>"
             f" (trước đó: {names[old]})."]

    price = to_float(data.get("price"))
    atr = to_float(data.get("atr"))
    if price > 0:
        info = f"💰 Giá đóng nến {tf_name}: <code>{price}</code>"
        if atr > 0:
            info += f" | ATR14: <code>{fmt_price(atr, price)}</code>"
        lines.append(info)
    st_cur = data.get("st_trend")
    if st_cur:
        lines.append(f"📊 SuperTrend khung chart hiện tại: {st_cur}")
    lines.append(f"🕒 {now_local().strftime('%d/%m %H:%M')} (nến {tf_name} đã đóng)")
    send_telegram_message("\n".join(lines))


# ==========================================
# XỬ LÝ TÍN HIỆU M15 (TRIGGER)
# ==========================================
def process_signal_data(data):
    try:
        action = str(data.get("action", ""))
        symbol = normalize_symbol(data.get("symbol"))

        # 1) Tín hiệu đổi hướng xu hướng lớn (H1/H4) chỉ được nhận qua /webhook_h1
        # (alert trên chart đúng khung, nến đã đóng). Đến từ cổng vào lệnh -> bỏ qua.
        if parse_h1_action(action) is not None:
            print(f"BỎ QUA {action} từ /webhook_m15 (xu hướng chỉ nhận qua /webhook_h1)")
            return

        # 2) Tín hiệu vào lệnh
        direction = parse_direction(action)
        if direction is None:
            print(f"BỎ QUA action không nhận diện: {action}")
            return

        price = to_float(data.get("price"))
        high = to_float(data.get("high"))
        low = to_float(data.get("low"))
        atr = to_float(data.get("atr"))
        buf = to_float(data.get("buf"))
        if buf <= 0:
            buf = 0.5  # đệm SL mặc định như cũ
        st_trend = data.get("st_trend", "Không rõ")

        # Kiểm tra dữ liệu: thiếu/0 thì tuyệt đối không tính lệnh
        if price <= 0 or high <= 0 or low <= 0 or high < low or not (low <= price <= high):
            print(f"BỎ QUA dữ liệu không hợp lệ: {data}")
            return

        contract_size = CONTRACT_SIZE.get(symbol)  # None nếu chưa cấu hình
        current_trend = h1_trend_state.get(symbol, 0)

        # Tên khung xu hướng lớn (H1 hoặc H4) và khung vào lệnh (M15, H1...) do Pine gửi kèm
        htf_name = str(data.get("htf") or "H1").upper()
        if htf_name not in ("H1", "H4", "D"):
            htf_name = "H1"
        entry_tf = TF_NAMES.get(str(data.get("tf") or "15"), "M15")

        # Alert (bản mới) gửi kèm hướng xu hướng lớn của nến đã đóng -> dùng luôn và tự phục hồi state,
        # không phụ thuộc việc Render có bị reset hay không.
        h1_in = to_float(data.get("h1"))
        if h1_in in (1.0, -1.0):
            current_trend = int(h1_in)
            if h1_trend_state.get(symbol, 0) != current_trend:
                with _lock:
                    h1_trend_state[symbol] = current_trend
                    save_state()

        sig_label = "🔥 CẮT SLOW BASELINE"
        if "EARLY_WARNING" in action:
            sig_label = "⚠️ CẢNH BÁO SỚM"
        elif "PULLBACK" in action:
            sig_label = "🎯 RSI PULLBACK"
        elif "RSI" in action:
            sig_label = "⚡ RSI"

        # Trạng thái so với xu hướng lớn
        aligned = (current_trend == 1 and direction == "BUY") or \
                  (current_trend == -1 and direction == "SELL")
        if current_trend == 0:
            status_text = f"❔ {htf_name} CHƯA XÁC ĐỊNH (Chờ xác nhận)"
        elif aligned:
            status_text = f"✅ VÀO LỆNH NGAY (Thuận {htf_name})"
        else:
            status_text = f"⏳ CHỜ XÁC NHẬN (Ngược {htf_name})"

        if direction == "BUY":
            sl = low - buf
            risk = price - sl
            sign = 1
            side_txt, icon = "MUA", "📈"
        else:
            sl = high + buf
            risk = sl - price
            sign = -1
            side_txt, icon = "BÁN", "📉"

        if risk <= 0:
            return

        # SL quá sát so với ATR -> volume sẽ phình to, bỏ tín hiệu
        if atr > 0 and risk < MIN_RISK_ATR * atr:
            send_telegram_message(
                f"⚠️ Bỏ qua {side_txt} {symbol} ({sig_label}): SL quá gần "
                f"(risk {fmt_price(risk, price)} < {MIN_RISK_ATR} x ATR {fmt_price(atr, price)})")
            return

        capped_note = ""
        if contract_size is None:
            vol = ""
            vol_line = "📦 Vol: <code>chưa cấu hình contract size</code>"
        else:
            raw_vol = RISK_USD / (risk * contract_size)
            vol = max(round(raw_vol, 2), 0.01)
            max_lot = MAX_LOT_BY_SYMBOL.get(symbol, MAX_LOT)
            if vol > max_lot:
                vol = max_lot
                capped_note = f"\n⚠️ Volume đã bị giới hạn ở {max_lot} lots"
            vol_line = f"📦 Vol: <code>{vol} lots</code>"

        tp1, tp2, tp3 = (price + sign * risk * k for k in (3, 5, 7))
        now_str = now_local().strftime("%Y-%m-%d %H:%M:%S")

        msg = (f"{status_text} <b>{sig_label}: {side_txt} {symbol}</b> ({entry_tf})\n"
               f"{icon} <b>SuperTrend{entry_tf}:</b> {st_trend}\n"
               f"💰 Entry: <code>{price}</code>\n"
               f"🛑 SL: <code>{fmt_price(sl, price)}</code>\n"
               f"🎯 TP1: <code>{fmt_price(tp1, price)}</code> | TP2: <code>{fmt_price(tp2, price)}</code> | TP3: <code>{fmt_price(tp3, price)}</code>\n"
               f"{vol_line}{capped_note}")
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

        handle_h1_trend(symbol, trend, data, trend_tf_of_action(action))
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
