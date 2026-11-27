from flask import Flask, request, jsonify
import requests
import json
import os
import csv
import threading
import math
import hmac
import re
import html
from contextlib import contextmanager
import psycopg2
from psycopg2.extras import RealDictCursor
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
USDJPY = float(os.environ.get("USDJPY", 159.3))
EURUSD = float(os.environ.get("EURUSD", 1.16))

# Giá trị (USD) khi giá đi 1.00 điểm với 1 lot — đã đối chiếu với lệnh thật trên tài khoản của bạn
CONTRACT_SIZE = {
    "XAUUSD": 100,          # đã kiểm tra: 0.01 lot, giá đi 3.25 -> 3.25 USD
    "US100": 1,             # đã kiểm tra: 4 lot, giá đi 162.45 -> 649.80 USD
    "GER40": 1 * EURUSD,    # đã kiểm tra: 1 EUR/điểm/lot (~1.16 USD)
    "JP225": 10 / USDJPY,   # đã kiểm tra: ~0.063 USD/điểm/lot (10 JPY)
    "BTCUSD": 1,            # đã kiểm tra: 0.24 lot, giá đi 803.66 -> 192.88 USD
}

# Đã đối chiếu với lệnh thật 0.05 lot trên tài khoản của bạn (MT5):
CONTRACT_SIZE["EURUSD"] = 100000          # giá đi 0.00403 -> -20.15 USD
CONTRACT_SIZE["USDJPY"] = 100000 / USDJPY # ~627.6 USD / 1.00 / lot (100.000 JPY quy theo tỷ giá)
CONTRACT_SIZE["US30"] = 1                 # giá đi 966.02 -> +48.30 USD (1 USD/điểm/lot)
CONTRACT_SIZE["NEARUSD"] = 10000          # giá đi 0.187 -> -93.50 USD (1 lot = 10.000 NEAR)
CONTRACT_SIZE["USOIL"] = 100              # giá đi 0.129 -> -0.65 USD (1 lot = 100 thùng)
# Có thể ghi đè bằng biến môi trường trên Render, ví dụ CONTRACT_SIZE_JSON={"USOIL": 100}
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
    with db() as c:
        for symbol, trend in h1_trend_state.items():
            c.execute('INSERT INTO trend_v37 VALUES (%s,%s) ON CONFLICT (symbol) DO UPDATE SET trend=EXCLUDED.trend', (symbol,trend))


@contextmanager
def db():
    conn = psycopg2.connect(os.environ['DATABASE_URL'], connect_timeout=5, cursor_factory=RealDictCursor)
    try:
        with conn:
            with conn.cursor() as cur:
                yield cur
    finally:
        conn.close()


def init_db():
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS trend_v37 (symbol text PRIMARY KEY, trend integer NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS signals_v37 (id bigserial PRIMARY KEY, created timestamptz DEFAULT now(), payload jsonb NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS risk_positions (id text PRIMARY KEY, symbol text NOT NULL, side text NOT NULL, entry double precision NOT NULL, sl double precision NOT NULL, qty double precision NOT NULL, multiplier double precision NOT NULL, initial_sl double precision NOT NULL, closed boolean NOT NULL DEFAULT false)")
        c.execute("CREATE TABLE IF NOT EXISTS signals_v382 (signal_id text PRIMARY KEY, payload jsonb NOT NULL, status text NOT NULL, message text, created timestamptz DEFAULT now())")
        c.execute("CREATE TABLE IF NOT EXISTS risk_updates (id bigint PRIMARY KEY)")


init_db()
with db() as c:
    c.execute('SELECT symbol, trend FROM trend_v37')
    h1_trend_state = {r['symbol']: r['trend'] for r in c.fetchall()}
  # {"XAUUSD": 1 (tăng) | -1 (giảm) | 0 (chưa rõ)}


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
    payload = dict(time=time_str, symbol=symbol, action=action, signal=sig_type, entry=entry, sl=sl, tp1=tp1, tp2=tp2, tp3=tp3, volume=vol, status=status)
    with db() as c:
        c.execute('INSERT INTO signals_v37 (payload) VALUES (%s::jsonb)', (json.dumps(payload, ensure_ascii=False),))


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
# Gộp các tín hiệu cùng mã + cùng chiều + cùng nến (cùng giá đóng) thành MỘT tin ghi đủ lý do.
MERGE_WINDOW = float(os.environ.get("MERGE_WINDOW_SEC", 5))
_pending = {}
_pending_lock = threading.Lock()


def _flush_card(key):
    with _pending_lock:
        card = _pending.pop(key, None)
    if not card:
        return
    try:
        reasons = " + ".join(card["labels"])
        msg = (f"{card['status_text']} <b>{reasons}: {card['side_txt']} {card['symbol']}</b> ({card['entry_tf']})\n"
               f"{card['icon']} <b>SuperTrend{card['entry_tf']}:</b> {card['st_trend']}\n"
               f"💰 Entry: <code>{card['price']}</code>\n"
               f"🛑 SL: <code>{fmt_price(card['sl'], card['price'])}</code>\n"
               f"🎯 TP1: <code>{fmt_price(card['tp1'], card['price'])}</code> | "
               f"TP2: <code>{fmt_price(card['tp2'], card['price'])}</code> | "
               f"TP3: <code>{fmt_price(card['tp3'], card['price'])}</code>\n"
               f"{card['vol_line']}{card['capped_note']}")
        send_telegram_message(msg)
        write_to_csv(card["now_str"], card["symbol"], card["direction"], reasons, card["price"], card["sl"],
                     card["tp1"], card["tp2"], card["tp3"], card["vol"], card["status_text"])
    except Exception as e:
        print(f"Lỗi gửi tin gộp: {e}")


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

        card = {"symbol": symbol, "direction": direction, "side_txt": side_txt, "icon": icon,
                "entry_tf": entry_tf, "st_trend": st_trend, "price": price, "sl": sl,
                "tp1": tp1, "tp2": tp2, "tp3": tp3, "vol": vol, "vol_line": vol_line,
                "capped_note": capped_note, "status_text": status_text, "now_str": now_str,
                "labels": [sig_label]}
        key = (symbol, direction, entry_tf, price)
        with _pending_lock:
            existing = _pending.get(key)
            if existing is not None:
                if sig_label not in existing["labels"]:
                    existing["labels"].append(sig_label)
                return  # đã có tin đang chờ gửi cho nến này -> chỉ bổ sung lý do
            _pending[key] = card
        if MERGE_WINDOW <= 0:
            _flush_card(key)
        else:
            t = threading.Timer(MERGE_WINDOW, _flush_card, args=(key,))
            t.daemon = True
            t.start()
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
        "version": "3.8.2",
        "h1_states": h1_trend_state,
        "time": now_local().isoformat()
    })


def install(app, send):
    secret=os.getenv('TELEGRAM_WEBHOOK_SECRET','')
    chat=os.environ['CHAT_ID']
    users={x.strip() for x in os.getenv('RISK_ADMIN_IDS','').split(',') if x.strip()}
    limit=float(os.getenv('PORTFOLIO_RISK_LIMIT','300'))
    capital=float(os.getenv('ACCOUNT_CAPITAL','25000'))
    if not secret or not users or not all(x.isdigit() for x in users) or not all(math.isfinite(x) and x>0 for x in (limit,capital)): raise ValueError('Invalid risk configuration')
    def num(s):
        x=float(s)
        if not math.isfinite(x) or x<=0: raise ValueError('Số phải hữu hạn và > 0')
        return x
    def report(c):
        c.execute('SELECT * FROM risk_positions WHERE NOT closed ORDER BY id')
        rows=c.fetchall()
        total=0; lines=['<b>V3.7 PORTFOLIO RISK</b>']
        for p in rows:
            r=max(0,(p['entry']-p['sl'])*(1 if p['side']=='BUY' else -1))*p['qty']*p['multiplier']; total+=r
            lines.append(f"{p['id']} | {p['symbol']} {p['side']} | ${r:.2f}")
        lines += [f'Tổng: ${total:.2f} / ${limit:.2f} ({100*total/capital:.2f}% vốn tham chiếu)', '⚠️ VƯỢT NGƯỠNG' if total>limit else '⚠️ CHẠM NGƯỠNG' if total==limit else 'Dưới ngưỡng theo dữ liệu đã nhập', 'Chỉ lệnh đã xác nhận; chưa đối soát broker; chưa gồm phí/trượt giá. Không chặn tín hiệu.']
        return '\n'.join(lines)
    @app.post('/telegram_risk')
    def telegram_risk():
        if not hmac.compare_digest(request.headers.get('X-Telegram-Bot-Api-Secret-Token',''),secret): return jsonify(error='unauthorized'),403
        d=request.get_json() or {}; m=d.get('message') or {}
        if str(m.get('chat',{}).get('id'))!=chat or str(m.get('from',{}).get('id')) not in users: return jsonify(ok=True)
        a=m.get('text','').split()
        if not a: return jsonify(ok=True)
        cmd=a[0].split('@')[0].lower()
        if cmd not in ('/risk','/open','/sl','/reduce','/close','/riskhelp'): return jsonify(ok=True)
        try:
            with db() as c:
                c.execute('INSERT INTO risk_updates VALUES (%s) ON CONFLICT DO NOTHING RETURNING id',(d['update_id'],))
                if not c.fetchone(): return jsonify(ok=True)
                c.execute('SELECT pg_advisory_xact_lock(37001)')
                if cmd=='/open':
                    if len(a) not in (7,8): raise ValueError('/open ID SYMBOL BUY|SELL ENTRY SL LOT [USD_PER_POINT_PER_LOT]')
                    _,i,s,side,e,sl,q,*extra=a; s=normalize_symbol(s); side=side.upper(); e,sl,q=map(num,(e,sl,q)); k=num(extra[0] if extra else CONTRACT_SIZE.get(s,0))
                    if not all(re.fullmatch(r'[A-Za-z0-9_-]{1,32}',v) for v in (i,s)) or side not in ('BUY','SELL') or (e-sl)*(1 if side=='BUY' else -1)<=0: raise ValueError('ID/mã/chiều/SL không hợp lệ')
                    c.execute('INSERT INTO risk_positions VALUES (%s,%s,%s,%s,%s,%s,%s,%s,false)',(i,s,side,e,sl,q,k,sl))
                elif cmd in ('/sl','/reduce','/close'):
                    if len(a)!=(2 if cmd=='/close' else 3): raise ValueError('Sai cú pháp')
                    c.execute('SELECT * FROM risk_positions WHERE id=%s AND NOT closed FOR UPDATE',(a[1],))
                    p=c.fetchone()
                    if not p: raise ValueError('Không tìm thấy lệnh mở')
                    if cmd=='/sl': c.execute('UPDATE risk_positions SET sl=%s WHERE id=%s',(num(a[2]),a[1]))
                    elif cmd=='/reduce':
                        q=num(a[2]);
                        if q>=p['qty']: raise ValueError('Nhập LOT còn lại nhỏ hơn LOT hiện tại; dùng /close nếu hết')
                        c.execute('UPDATE risk_positions SET qty=%s WHERE id=%s',(q,a[1]))
                    else: c.execute('UPDATE risk_positions SET closed=true WHERE id=%s',(a[1],))
                msg=report(c) if cmd!='/riskhelp' else '/open ID SYMBOL BUY|SELL ENTRY SL LOT [USD_PER_POINT_PER_LOT]\n/sl ID SL_MOI\n/reduce ID LOT_CON_LAI\n/close ID\n/risk'
            send(msg)
        except (ValueError, psycopg2.errors.UniqueViolation) as e:
            send('Không cập nhật: '+('ID đã tồn tại' if isinstance(e,psycopg2.errors.UniqueViolation) else str(e).replace('<','&lt;').replace('>','&gt;')))
        return jsonify(ok=True)
    return report


@app.get('/health/db')
def health_db():
    try:
        with db() as c:
            c.execute('SELECT 1')
        return jsonify(status='ok', database='connected', version='3.8.2', telegram_risk_enabled=bool(os.getenv('TELEGRAM_WEBHOOK_SECRET') and os.getenv('RISK_ADMIN_IDS')))
    except Exception:
        return jsonify(status='error', database='unavailable'), 503


if os.getenv('TELEGRAM_WEBHOOK_SECRET') and os.getenv('RISK_ADMIN_IDS'):
    risk_report = install(app, send_telegram_message)

# V3.8.2: separate authenticated endpoint; legacy routes remain unchanged.
from decimal import Decimal, ROUND_DOWN

SYNC_SYMBOLS = {'XAUUSD', 'JP225', 'US100'}
SYNC_TYPES = {'SLOW_BASELINE_CROSS', 'BASELINE_CROSS', 'RSI_PULLBACK'}

def sync_validate(d):
    if not isinstance(d, dict) or d.get('version') != '3.8.1' or d.get('event') != 'SIMULATED_ENTRY' or d.get('timeframe') != '60':
        raise ValueError('Invalid version/event/timeframe')
    s, side, source, ts = (d.get(k) for k in ('symbol','direction','source','bar_close_time'))
    if s not in SYNC_SYMBOLS or side not in ('BUY','SELL') or not isinstance(source,str) or not re.fullmatch(r'[A-Za-z0-9_.:!/-]{1,100}',source) or type(ts) is not int or not 0 < ts < 4102444800000:
        raise ValueError('Invalid symbol/source/direction/time')
    if d.get('signal_id') != f'{source}|60|{ts}|{side}': raise ValueError('Invalid signal_id')
    if d.get('h4_trend') not in ('UP','DOWN') or d.get('h1_trend') not in ('UP','DOWN','UNKNOWN'): raise ValueError('Invalid trend')
    signals=d.get('signals')
    if not isinstance(signals,list) or not signals or any(not isinstance(x,str) or x not in SYNC_TYPES for x in signals) or len(set(signals))!=len(signals): raise ValueError('Invalid signals')
    for k in ('entry','sl','target','target_r','risk_usd','sim_qty','sim_usd_per_point_per_unit','sim_planned_risk'):
        x=d.get(k)
        if type(x) not in (int,float) or not math.isfinite(x) or not 0<x<1e12: raise ValueError('Invalid '+k)
    e,sl,t=map(lambda k: Decimal(str(d[k])),('entry','sl','target'))
    sign=1 if side=='BUY' else -1
    if (e-sl)*sign<=0 or (t-e)*sign<=0: raise ValueError('Invalid SL/target direction')
    if not math.isclose(float(abs(t-e)/abs(e-sl)),d['target_r'],rel_tol=1e-6,abs_tol=1e-6): raise ValueError('Target R mismatch')
    if not math.isclose(d['risk_usd'],RISK_USD,rel_tol=1e-9): raise ValueError('Pine/server risk mismatch')
    for k in ('tp1','tp2','tp3','score','broker_lot'):
        if d.get(k) is not None: raise ValueError('Unexpected '+k)
    return d

def sync_size(d):
    s=d['symbol']; distance=abs(Decimal(str(d['entry']))-Decimal(str(d['sl'])))
    k=Decimal(str(CONTRACT_SIZE[s])); step=Decimal(str(json.loads(os.getenv('SYNC_LOT_STEP_JSON','{}')).get(s,0.01)))
    cap=Decimal(str(MAX_LOT_BY_SYMBOL.get(s,MAX_LOT)))
    if any(not x.is_finite() or x<=0 for x in (k,step,cap)): raise ValueError('Invalid lot configuration')
    lot=(min(Decimal(str(RISK_USD))/(distance*k),cap)/step).to_integral_value(rounding=ROUND_DOWN)*step
    return float(lot),float(lot*distance*k),float(k)

def sync_send(message):
    try:
        r=requests.post(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',json={'chat_id':CHAT_ID,'text':message},timeout=8)
        return r.ok and r.json().get('ok') is True
    except Exception:
        return False

@app.get('/health/sync')
def sync_health():
    try:
        with db() as c:
            c.execute('SELECT status,count(*) AS n FROM signals_v382 GROUP BY status')
            counts={r['status']:r['n'] for r in c.fetchall()}
        return jsonify(version='3.8.2',enabled=bool(os.getenv('SYNC_WEBHOOK_SECRET')),symbols=sorted(SYNC_SYMBOLS),counts=counts)
    except Exception: return jsonify(status='database unavailable'),503

@app.post('/webhook_v382/<secret>')
def webhook_v382(secret):
    expected=os.getenv('SYNC_WEBHOOK_SECRET','')
    if not expected or not hmac.compare_digest(secret,expected): return jsonify(error='unauthorized'),403
    if request.content_length and request.content_length>16384: return jsonify(error='payload too large'),413
    try:
        d=sync_validate(request.get_json(silent=True)); lot,risk,k=sync_size(d)
        payload=json.dumps(d,sort_keys=True,allow_nan=False)
        limit=float(os.getenv('PORTFOLIO_RISK_LIMIT','300'))
        with db() as c:
            c.execute('SELECT pg_advisory_xact_lock(37001)')
            c.execute('INSERT INTO signals_v382 (signal_id,payload,status) VALUES (%s,%s::jsonb,%s) ON CONFLICT DO NOTHING RETURNING signal_id',(d['signal_id'],payload,'sending'))
            if not c.fetchone():
                c.execute('SELECT payload,status FROM signals_v382 WHERE signal_id=%s',(d['signal_id'],)); old=c.fetchone()
                if old['payload']!=d: return jsonify(error='signal conflict'),409
                return jsonify(status='duplicate',delivery=old['status'])
            c.execute("SELECT COALESCE(SUM(GREATEST(0,(entry-sl)*CASE WHEN side='BUY' THEN 1 ELSE -1 END)*qty*multiplier),0) AS total FROM risk_positions WHERE NOT closed")
            total=float(c.fetchone()['total']); projected=total+risk
            warning='VUOT NGUONG' if projected>limit else 'CHAM NGUONG' if math.isclose(projected,limit) else 'DUOI NGUONG'
            message='\n'.join([f"V3.8.2 | {d['symbol']} {d['direction']}", 'SIMULATED ENTRY - CHO XAC NHAN',f"H4: {d['h4_trend']} | H1: {d['h1_trend']}",f"Signals: {', '.join(d['signals'])}",f"Profile: {d.get('profile')} | PB: {d.get('pullback_number')} | Base: {d.get('baseline_number')}",f"Entry: {d['entry']} | SL: {d['sl']}",f"Target: {d['target']} ({d['target_r']}R)",f"Lot de xuat: {lot:g} | Risk: ${risk:.2f} (budget ${RISK_USD:g})",f"USD/point/lot: {k:.8g}",f"Risk hien tai: ${total:.2f} | Du kien: ${projected:.2f}/${limit:g} | {warning}", 'Lot=0: khong du khoi luong toi thieu' if lot==0 else 'Kiem tra lot va he so broker truoc khi xac nhan', 'Chua doi soat broker; chua gom phi/truot gia. Khong chan tin hieu.',d['signal_id']])
            c.execute('UPDATE signals_v382 SET message=%s WHERE signal_id=%s',(message,d['signal_id']))
        ok=sync_send(message)
        with db() as c: c.execute('UPDATE signals_v382 SET status=%s WHERE signal_id=%s',('sent' if ok else 'delivery_uncertain',d['signal_id']))
        return jsonify(status='accepted',delivery='sent' if ok else 'delivery_uncertain',lot=lot,risk=risk)
    except (ValueError,TypeError,KeyError,ArithmeticError): return jsonify(error='invalid signal or configuration'),400
    except Exception: return jsonify(error='processing failed'),503


if __name__ == '__main__':
    # Render cần đọc PORT từ biến môi trường
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
