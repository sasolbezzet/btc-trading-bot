import os
import time
import logging
import json
import base64
import hmac
import hashlib
import requests
import threading
import re
from datetime import datetime
from dotenv import load_dotenv
from groq import Groq
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

AUTO_TRADE_ENABLED = True
last_signal = None
last_price = 0
last_rsi = 50
last_fg = 0
last_fg_class = "Neutral"

class BTCBot:
    def __init__(self):
        self.api_key = os.getenv('KUCOIN_API_KEY')
        self.api_secret = os.getenv('KUCOIN_API_SECRET')
        self.api_passphrase = os.getenv('KUCOIN_API_PASSPHRASE')
        self.telegram_token = os.getenv('TELEGRAM_BOT_TOKEN')
        self.telegram_chat_id = os.getenv('TELEGRAM_CHAT_ID')
        self.groq = Groq(api_key=os.getenv('GROQ_API_KEY'))
        self.alpha_key = os.getenv('ALPHA_VANTAGE_KEY', '')
        
        self.trade_pct = 0.30
        self.leverage = 25
        self.min_conf = 60
        self.balance = 0
        
        self.stop_loss_pct = 0.015
        self.take_profit_pct = 0.03
        
        self.contract_size = 0.001
        self.min_contracts = 1
        
        self.news_cache = None
        self.news_cache_time = None
        self.news_cache_ttl = 7200
        
        self.onchain_cache = None
        self.onchain_cache_time = None
        self.onchain_cache_ttl = 3600
        
        self.alpha_calls_today = 0
        self.last_reset_date = datetime.now().date()
        self.max_alpha_calls = 20
        
        self.is_closing = False
        
        logger.info("BTC Bot Started - CLEAN VERSION")
        self.send_telegram("✅ BTC Bot Started!\n\n📊 Technical + News + On-Chain\n🛡️ SL 1.5% | TP 3%")
    
    def send_telegram(self, msg):
        if self.telegram_token and self.telegram_chat_id:
            try:
                url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
                requests.post(url, json={"chat_id": self.telegram_chat_id, "text": msg}, timeout=10)
            except:
                pass
    
    def get_main_keyboard(self):
        keyboard = [
            [InlineKeyboardButton("🟢 ENABLE", callback_data="on"), InlineKeyboardButton("🔴 DISABLE", callback_data="off")],
            [InlineKeyboardButton("📊 STATUS", callback_data="status"), InlineKeyboardButton("💰 BALANCE", callback_data="balance")],
            [InlineKeyboardButton("📈 PRICE", callback_data="price"), InlineKeyboardButton("📡 SIGNAL", callback_data="signal")],
            [InlineKeyboardButton("📊 TECH", callback_data="technical"), InlineKeyboardButton("📌 POSITION", callback_data="position")],
            [InlineKeyboardButton("📰 NEWS", callback_data="news"), InlineKeyboardButton("⛓️ ONCHAIN", callback_data="onchain")],
            [InlineKeyboardButton("🔄 REFRESH", callback_data="refresh")]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    def get_news_sentiment(self):
        if not self.alpha_key:
            return None
        if self.news_cache and self.news_cache_time:
            if (datetime.now() - self.news_cache_time).total_seconds() < self.news_cache_ttl:
                return self.news_cache
        today = datetime.now().date()
        if today != self.last_reset_date:
            self.alpha_calls_today = 0
            self.last_reset_date = today
        if self.alpha_calls_today >= self.max_alpha_calls:
            return self.news_cache
        try:
            url = "https://www.alphavantage.co/query"
            params = {"function": "NEWS_SENTIMENT", "tickers": "BTC", "apikey": self.alpha_key, "limit": 3}
            resp = requests.get(url, params=params, timeout=15)
            self.alpha_calls_today += 1
            if resp.status_code == 200:
                data = resp.json()
                if 'feed' in data:
                    articles = data['feed'][:3]
                    sentiments = [a.get('overall_sentiment_score', 0) for a in articles]
                    avg_sentiment = sum(sentiments) / len(sentiments) if sentiments else 0
                    label = "Bullish" if avg_sentiment > 0.15 else "Bearish" if avg_sentiment < -0.15 else "Neutral"
                    self.news_cache = {'label': label, 'score': avg_sentiment, 'articles': len(articles), 'headline': articles[0]['title'][:80] if articles else ''}
                    self.news_cache_time = datetime.now()
                    return self.news_cache
            return self.news_cache
        except:
            return self.news_cache
    
    def get_onchain_data(self):
        if self.onchain_cache and self.onchain_cache_time:
            if (datetime.now() - self.onchain_cache_time).total_seconds() < self.onchain_cache_ttl:
                return self.onchain_cache
        try:
            data = {'has_data': False, 'price': None, 'fees': None, 'blocks': None}
            try:
                resp = requests.get("https://blockchain.info/q/24hrprice", timeout=10)
                if resp.status_code == 200:
                    data['price'] = float(resp.text)
                    data['has_data'] = True
            except:
                pass
            try:
                resp = requests.get("https://mempool.space/api/v1/fees/recommended", timeout=10)
                if resp.status_code == 200:
                    fee_data = resp.json()
                    data['fees'] = {'fastest': fee_data.get('fastestFee', 0)}
                    data['has_data'] = True
            except:
                pass
            try:
                resp = requests.get("https://blockchain.info/q/getblockcount", timeout=10)
                if resp.status_code == 200:
                    data['blocks'] = int(resp.text)
                    data['has_data'] = True
            except:
                pass
            if data['has_data']:
                self.onchain_cache = data
                self.onchain_cache_time = datetime.now()
            return data
        except:
            return {'has_data': False}
    
    def get_news_text(self):
        news = self.get_news_sentiment()
        if not news:
            return "📰 *News Update*\n━━━━━━━━━━━━━━━━\nUnable to fetch news."
        return f"""📰 *CRYPTO NEWS SENTIMENT*\n━━━━━━━━━━━━━━━━━━━━━━━━━\n🎯 Overall: {news['label']} ({news['score']:+.2f})\n📊 Articles: {news['articles']}\n📰 Top: {news['headline']}...\n━━━━━━━━━━━━━━━━━━━━━━━━━"""
    
    def get_onchain_text(self):
        data = self.get_onchain_data()
        if not data.get('has_data'):
            return "⛓️ *On-Chain Data*\n━━━━━━━━━━━━━━━━\nUnable to fetch on-chain data."
        text = f"⛓️ *ON-CHAIN ANALYSIS*\n━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        if data.get('price'):
            text += f"💰 BTC Price: ${data['price']:,.2f}\n"
        if data.get('fees'):
            text += f"💸 Fees: fastest={data['fees']['fastest']} sat/vB\n"
        if data.get('blocks'):
            text += f"📦 Blocks: {data['blocks']:,}\n"
        return text
    
    def get_status_text(self):
        global AUTO_TRADE_ENABLED, last_signal, last_price, last_rsi, last_fg, last_fg_class
        status = "🟢 ACTIVE" if AUTO_TRADE_ENABLED else "🔴 INACTIVE"
        balance = self.get_balance()
        price = self.get_price()
        rsi = self.get_rsi()
        fg_val, fg_class = self.get_fear_greed()
        last_price = price
        last_rsi = rsi
        last_fg = fg_val
        last_fg_class = fg_class
        signal_action = last_signal.get('action', 'WAITING') if last_signal else 'WAITING'
        signal_conf = last_signal.get('confidence', 0) if last_signal else 0
        rsi_status = "OVERSOLD 📉" if rsi < 30 else "OVERBOUGHT 📈" if rsi > 70 else "NEUTRAL ➡️"
        has_position = self.has_active_position()
        min_margin_needed = (price * self.contract_size) / self.leverage
        news_info = ""
        news = self.get_news_sentiment()
        if news:
            news_info = f"\n📰 News: {news['label']} ({news['score']:+.2f})"
        onchain_info = ""
        onchain = self.get_onchain_data()
        if onchain.get('price'):
            onchain_info = f"\n⛓️ Price: ${onchain['price']:,.2f}"
        return (
            f"🤖 *BTC TRADING BOT*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚙️ Auto-Trade: {status}\n"
            f"💰 Balance: ${balance:.2f} USDT\n"
            f"📈 BTC Price: ${price:,.2f}\n"
            f"📊 RSI: {rsi:.1f} {rsi_status}\n"
            f"😱 Fear & Greed: {fg_val} ({fg_class}){news_info}{onchain_info}\n"
            f"📡 Last Signal: {signal_action} ({signal_conf}%)\n"
            f"📌 Position: {'🟢 ACTIVE' if has_position else '⚪ NONE'}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🛡️ SL: {self.stop_loss_pct*100}% | TP: {self.take_profit_pct*100}%\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
    
    def get_position_text(self):
        pos = self.get_position_details()
        if pos['has_position']:
            side_icon = "🟢 LONG" if pos['side'] == 'long' else "🔴 SHORT"
            pnl_usd_formatted = f"+${abs(pos['pnl_usd']):.2f}" if pos['pnl_usd'] >= 0 else f"-${abs(pos['pnl_usd']):.2f}"
            sl_price = pos['entry'] * 0.985 if pos['side'] == 'long' else pos['entry'] * 1.015
            tp_price = pos['entry'] * 1.03 if pos['side'] == 'long' else pos['entry'] * 0.97
            return (
                f"📌 *ACTIVE POSITION*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📊 Side: {side_icon}\n"
                f"📦 Contracts: {pos['qty']} (≈{pos['qty'] * self.contract_size:.4f} BTC)\n"
                f"💰 Entry: ${pos['entry']:.2f}\n"
                f"📈 Current: ${pos['current']:.2f}\n"
                f"📉 P&L: {pos['pnl_pct']:+.2f}% ({pnl_usd_formatted})\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🎯 Target:\n"
                f"   🛑 SL: ${sl_price:.2f} (-1.5%)\n"
                f"   🎯 TP: ${tp_price:.2f} (+3%)\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━"
            )
        return "📌 *NO ACTIVE POSITION*"
    
    def has_active_position(self):
        try:
            headers = self._get_headers("GET", "/api/v1/positions")
            resp = requests.get("https://api-futures.kucoin.com/api/v1/positions", headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if data.get('code') == '200000':
                    for pos in data.get('data', []):
                        if float(pos.get('currentQty', 0)) != 0:
                            return True
            return False
        except:
            return False
    
    def get_position_details(self):
        try:
            headers = self._get_headers("GET", "/api/v1/positions")
            resp = requests.get("https://api-futures.kucoin.com/api/v1/positions", headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if data.get('code') == '200000':
                    for pos in data.get('data', []):
                        qty = float(pos.get('currentQty', 0))
                        if qty != 0:
                            entry = float(pos.get('avgEntryPrice', 0))
                            mark = float(pos.get('markPrice', 0))
                            unrealised_pnl = float(pos.get('unrealisedPnl', 0))
                            unrealised_roe = float(pos.get('unrealisedRoePcnt', 0))
                            side = 'long' if qty > 0 else 'short'
                            return {
                                'has_position': True, 'side': side, 'qty': abs(qty),
                                'entry': entry, 'current': mark,
                                'pnl_usd': unrealised_pnl, 'pnl_pct': unrealised_roe * 100
                            }
            return {'has_position': False}
        except:
            return {'has_position': False}
    
    def _get_headers(self, method, endpoint, body=""):
        timestamp = int(time.time() * 1000)
        str_to_sign = str(timestamp) + method + endpoint + body
        signature = base64.b64encode(hmac.new(self.api_secret.encode(), str_to_sign.encode(), hashlib.sha256).digest()).decode()
        passphrase = base64.b64encode(hmac.new(self.api_secret.encode(), self.api_passphrase.encode(), hashlib.sha256).digest()).decode()
        return {
            "KC-API-KEY": self.api_key, "KC-API-SIGN": signature,
            "KC-API-TIMESTAMP": str(timestamp), "KC-API-PASSPHRASE": passphrase,
            "KC-API-KEY-VERSION": "2", "Content-Type": "application/json"
        }
    
    def get_balance(self):
        try:
            endpoint = "/api/v1/account-overview?currency=USDT"
            headers = self._get_headers("GET", endpoint)
            resp = requests.get("https://api-futures.kucoin.com" + endpoint, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if data.get('code') == '200000':
                    return float(data['data'].get('availableBalance', 0))
            return 0
        except:
            return 0
    
    def get_price(self):
        try:
            resp = requests.get("https://api.kucoin.com/api/v1/market/orderbook/level1?symbol=BTC-USDT", timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if data.get('code') == '200000':
                    return float(data['data']['price'])
            return 0
        except:
            return 0
    
    def get_rsi(self):
        try:
            resp = requests.get("https://api.coingecko.com/api/v3/coins/bitcoin/market_chart", 
                               params={"vs_currency": "usd", "days": 14}, timeout=10)
            if resp.status_code == 200:
                prices = [p[1] for p in resp.json().get('prices', [])]
                if len(prices) >= 14:
                    gains, losses = [], []
                    for i in range(1, len(prices)):
                        diff = prices[i] - prices[i-1]
                        if diff > 0:
                            gains.append(diff)
                            losses.append(0)
                        else:
                            gains.append(0)
                            losses.append(abs(diff))
                    avg_gain = sum(gains[-14:]) / 14
                    avg_loss = sum(losses[-14:]) / 14
                    if avg_loss == 0:
                        return 100
                    return 100 - (100 / (1 + (avg_gain / avg_loss)))
            return 50
        except:
            return 50
    
    def get_fear_greed(self):
        try:
            resp = requests.get("https://api.alternative.me/fng/", timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if data.get('data'):
                    return int(data['data'][0]['value']), data['data'][0]['value_classification']
            return 50, "Neutral"
        except:
            return 50, "Neutral"
    
    def get_groq_signal(self, price, rsi, fg_val, onchain_data):
        prompt = f"""BTC Analysis:
Price: ${price:,.0f}
RSI: {rsi:.0f}
F&G: {fg_val}
Fees: {onchain_data.get('fees', {}).get('fastest', 'N/A') if onchain_data else 'N/A'} sat/vB

RSI >70 Bearish, <30 Bullish
F&G <30 Bullish, >70 Bearish
High fees >20 = bullish

Respond JSON: {{"action":"BUY/SELL/HOLD","confidence":0-100,"reason":"brief"}}"""
        try:
            resp = self.groq.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=150
            )
            text = resp.choices[0].message.content
            match = re.search(r'\{[^{}]*\}', text)
            if match:
                return json.loads(match.group())
            return None
        except:
            return None
    
    def calculate_contracts(self, balance, price):
        margin = balance * self.trade_pct
        position_value = margin * self.leverage
        btc_amount = position_value / price
        contracts = int(btc_amount / self.contract_size)
        return max(self.min_contracts, contracts)
    
    def close_position(self, side, size):
        try:
            logger.info(f"Closing position: side={side}, size={size}")
            size_int = int(size)
            order_body = {
                "clientOid": str(int(time.time() * 1000)),
                "side": side,
                "symbol": "XBTUSDTM",
                "type": "market",
                "size": size_int,
                "reduceOnly": True,
                "marginMode": "CROSS",
                "leverage": str(self.leverage)
            }
            body = json.dumps(order_body)
            headers = self._get_headers("POST", "/api/v1/orders", body)
            resp = requests.post("https://api-futures.kucoin.com/api/v1/orders", 
                                headers=headers, data=body, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if data.get('code') == '200000':
                    logger.info("Position closed successfully")
                    return True
            logger.error(f"Close failed: {resp.text}")
            return False
        except Exception as e:
            logger.error(f"Close error: {e}")
            return False
    
    def monitor_position(self):
        if self.is_closing:
            return
        pos = self.get_position_details()
        if not pos['has_position']:
            return
        entry = pos['entry']
        current = pos['current']
        side = pos['side']
        qty = pos['qty']
        if side == 'long':
            sl_price = entry * (1 - self.stop_loss_pct)
            tp_price = entry * (1 + self.take_profit_pct)
            if current <= sl_price:
                self.is_closing = True
                self.send_telegram(f"🛑 STOP LOSS HIT! Closing LONG at ${current:.2f}")
                self.close_position("sell", qty)
                self.is_closing = False
            elif current >= tp_price:
                self.is_closing = True
                self.send_telegram(f"🎯 TAKE PROFIT HIT! Closing LONG at ${current:.2f}")
                self.close_position("sell", qty)
                self.is_closing = False
    
    def place_order(self, side, contracts, price):
        try:
            size_int = int(contracts)
            order_body = {
                "clientOid": str(int(time.time() * 1000)),
                "side": side,
                "symbol": "XBTUSDTM",
                "type": "market",
                "size": size_int,
                "leverage": str(self.leverage),
                "marginMode": "CROSS"
            }
            body = json.dumps(order_body)
            headers = self._get_headers("POST", "/api/v1/orders", body)
            resp = requests.post("https://api-futures.kucoin.com/api/v1/orders", 
                                headers=headers, data=body, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if data.get('code') == '200000':
                    return data['data']
            return None
        except:
            return None
    
    def execute_trade(self, signal, price):
        global AUTO_TRADE_ENABLED
        if not AUTO_TRADE_ENABLED or not signal:
            return False
        if self.has_active_position():
            return False
        action = signal.get('action', 'HOLD')
        confidence = signal.get('confidence', 0)
        if confidence < self.min_conf:
            return False
        balance = self.get_balance()
        if balance <= 0:
            return False
        if action in ['BUY', 'SELL']:
            contracts = self.calculate_contracts(balance, price)
            required_margin = (price * self.contract_size * contracts) / self.leverage
            if balance < required_margin:
                self.send_telegram(f"⚠️ Need ${required_margin:.2f}, have ${balance:.2f}")
                return False
            side = "buy" if action == 'BUY' else "sell"
            is_long = (action == 'BUY')
            stop_price = round(price * (1 - self.stop_loss_pct), 1) if is_long else round(price * (1 + self.stop_loss_pct), 1)
            tp_price = round(price * (1 + self.take_profit_pct), 1) if is_long else round(price * (1 - self.take_profit_pct), 1)
            msg = f"""🚀 *{action} ORDER*\n━━━━━━━━━━━━━━━━━━━━━\nEntry: ${price:.2f}\nContracts: {contracts}\nLeverage: {self.leverage}x\nMargin: ${required_margin:.2f}\nStop Loss: ${stop_price:.2f} (1.5%)\nTake Profit: ${tp_price:.2f} (3%)\nConfidence: {confidence}%\n━━━━━━━━━━━━━━━━━━━━━"""
            self.send_telegram(msg)
            order = self.place_order(side, contracts, price)
            if order:
                self.send_telegram(f"✅ Position opened! Monitoring SL/TP.")
                return True
            else:
                self.send_telegram("❌ Order failed!")
                return False
        return False
    
    def run(self):
        global last_signal, last_price, last_rsi, last_fg, last_fg_class
        logger.info("Starting monitoring loop...")
        last_analysis = 0
        while True:
            try:
                now = time.time()
                self.balance = self.get_balance()
                self.monitor_position()
                price = self.get_price()
                if now - last_analysis >= 7200:
                    price = self.get_price()
                    rsi = self.get_rsi()
                    fg_val, fg_class = self.get_fear_greed()
                    last_price = price
                    last_rsi = rsi
                    last_fg = fg_val
                    last_fg_class = fg_class
                    logger.info(f"Analysis - Price: ${price:.2f}")
                    onchain_data = self.get_onchain_data()
                    signal = self.get_groq_signal(price, rsi, fg_val, onchain_data)
                    last_signal = signal
                    if signal and signal.get('action') != 'HOLD':
                        logger.info(f"Signal: {signal}")
                        self.execute_trade(signal, price)
                    last_analysis = now
                time.sleep(5)
            except Exception as e:
                logger.error(f"Loop error: {e}")
                time.sleep(30)


btc_bot = BTCBot()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(btc_bot.get_status_text(), reply_markup=btc_bot.get_main_keyboard(), parse_mode='Markdown')

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global AUTO_TRADE_ENABLED, last_signal
    query = update.callback_query
    try:
        await query.answer()
    except:
        pass
    data = query.data
    chat_id = update.effective_chat.id
    if data == "on":
        AUTO_TRADE_ENABLED = True
        await context.bot.send_message(chat_id, "✅ Auto-trade ON", reply_markup=btc_bot.get_main_keyboard())
    elif data == "off":
        AUTO_TRADE_ENABLED = False
        await context.bot.send_message(chat_id, "🔴 Auto-trade OFF", reply_markup=btc_bot.get_main_keyboard())
    elif data == "status":
        await context.bot.send_message(chat_id, btc_bot.get_status_text(), reply_markup=btc_bot.get_main_keyboard(), parse_mode='Markdown')
    elif data == "balance":
        balance = btc_bot.get_balance()
        await context.bot.send_message(chat_id, f"💰 Balance: ${balance:.2f}", reply_markup=btc_bot.get_main_keyboard())
    elif data == "price":
        price = btc_bot.get_price()
        await context.bot.send_message(chat_id, f"📈 BTC: ${price:.2f}", reply_markup=btc_bot.get_main_keyboard())
    elif data == "signal":
        if last_signal:
            action = last_signal.get('action', 'N/A')
            conf = last_signal.get('confidence', 0)
            await context.bot.send_message(chat_id, f"📡 Signal: {action} ({conf}%)", reply_markup=btc_bot.get_main_keyboard())
        else:
            await context.bot.send_message(chat_id, "⏳ No signal yet", reply_markup=btc_bot.get_main_keyboard())
    elif data == "technical":
        price = btc_bot.get_price()
        rsi = btc_bot.get_rsi()
        fg_val, fg_class = btc_bot.get_fear_greed()
        await context.bot.send_message(chat_id, f"📊 TECHNICAL\n━━━━━━━━━━\nRSI: {rsi:.1f}\nF&G: {fg_val} ({fg_class})\nPrice: ${price:.2f}", reply_markup=btc_bot.get_main_keyboard())
    elif data == "position":
        position_text = btc_bot.get_position_text()
        await context.bot.send_message(chat_id, position_text, reply_markup=btc_bot.get_main_keyboard(), parse_mode='Markdown')
    elif data == "news":
        news_text = btc_bot.get_news_text()
        await context.bot.send_message(chat_id, news_text, reply_markup=btc_bot.get_main_keyboard(), parse_mode='Markdown')
    elif data == "onchain":
        onchain_text = btc_bot.get_onchain_text()
        await context.bot.send_message(chat_id, onchain_text, reply_markup=btc_bot.get_main_keyboard(), parse_mode='Markdown')
    elif data == "refresh":
        await context.bot.send_message(chat_id, btc_bot.get_status_text(), reply_markup=btc_bot.get_main_keyboard(), parse_mode='Markdown')


def run_telegram():
    app = Application.builder().token(btc_bot.telegram_token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_callback))
    logger.info("Telegram started")
    app.run_polling()


if __name__ == "__main__":
    try:
        url = f"https://api.telegram.org/bot{btc_bot.telegram_token}/deleteWebhook"
        requests.get(url, timeout=5)
    except:
        pass
    t = threading.Thread(target=btc_bot.run, daemon=True)
    t.start()
    run_telegram()
