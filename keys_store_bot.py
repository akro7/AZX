#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
============================================================
   بوت متجر المفاتيح (Keys Store Bot) - Telegram
============================================================
بوت تيليجرام كامل لمتجر مفاتيح دخول (Keys) يعمل بنظام نقاط،
مع لوحة تحكم كاملة للمالك ونظام إحالة وهدايا وإذاعة وحماية
من الاستغلال. ملف واحد فقط، جاهز للتشغيل مباشرة بعد ضبط
الإعدادات أدناه.

المتطلبات:
    pip install "python-telegram-bot>=22.7"

التشغيل:
    python3 keys_store_bot.py
============================================================
"""

import sqlite3
import logging
import asyncio
import secrets
import string
import html
import re
import json
import os
import time
import atexit
from datetime import datetime, timedelta
from typing import Optional


# ============================================================
# Python-only Telegram compatibility layer
# Uses Telegram Bot API directly with the Python standard library.
# No external packages are required.
# ============================================================
import urllib.request as _urlreq
import urllib.parse as _urlparse
import threading as _threading

class TelegramError(Exception): pass
class Forbidden(TelegramError): pass
class BadRequest(TelegramError): pass
class NetworkError(TelegramError): pass
class TimedOut(NetworkError): pass
class RetryAfter(TelegramError):
    def __init__(self, retry_after=1, *a): self.retry_after=retry_after; super().__init__(*a)
class ApplicationHandlerStop(Exception): pass

class ParseMode:
    HTML = "HTML"
    MARKDOWN = "Markdown"
    MARKDOWN_V2 = "MarkdownV2"

class _Obj:
    def __init__(self, **kw): self.__dict__.update(kw)
    def __getattr__(self, n): return None

class User(_Obj): pass
class Chat(_Obj): pass
class Message(_Obj):
    @property
    def text_html(self):
        return self.text or ''
    async def reply_text(self, text, **kwargs):
        return await self._bot.send_message(chat_id=self.chat.id, text=text, **kwargs)
    async def delete(self): return await self._bot.delete_message(self.chat.id, self.message_id)

class CallbackQuery(_Obj):
    async def answer(self, text=None, show_alert=False, **kwargs):
        return await self._bot.answer_callback_query(self.id, text=text, show_alert=show_alert)
    async def edit_message_text(self, text, **kwargs):
        return await self._bot.edit_message_text(chat_id=self.message.chat.id, message_id=self.message.message_id, text=text, **kwargs)
    async def edit_message_reply_markup(self, reply_markup=None, **kwargs):
        return await self._bot.edit_message_reply_markup(chat_id=self.message.chat.id, message_id=self.message.message_id, reply_markup=reply_markup)
    async def delete_message(self): return await self._bot.delete_message(self.message.chat.id, self.message.message_id)

class Update(_Obj):
    ALL_TYPES = ()
    def __init__(self, **kw):
        super().__init__(**kw)
        self.effective_user = getattr(self, 'effective_user', None)
        self.effective_message = getattr(self, 'effective_message', None)

class InlineKeyboardButton:
    def __init__(self, text, callback_data=None, url=None, **kwargs):
        self.text=text; self.callback_data=callback_data; self.url=url
        for k,v in kwargs.items(): setattr(self,k,v)
class InlineKeyboardMarkup:
    def __init__(self, inline_keyboard): self.inline_keyboard=inline_keyboard
class ReplyKeyboardMarkup:
    def __init__(self, keyboard, **kwargs): self.keyboard=keyboard; self.kwargs=kwargs
class ReplyKeyboardRemove:
    def __init__(self, **kwargs): self.kwargs=kwargs
class KeyboardButton:
    def __init__(self, text, **kwargs): self.text=text; self.kwargs=kwargs
class InputMediaPhoto:
    def __init__(self, media, **kwargs): self.media=media; self.kwargs=kwargs
class MessageEntity:
    CUSTOM_EMOJI='custom_emoji'

class ContextTypes:
    DEFAULT_TYPE = object

class _Filter:
    def __init__(self, fn): self.fn=fn
    def __call__(self, update): return bool(self.fn(update))
    def __and__(self, other): return _Filter(lambda u:self(u) and other(u))
    def __or__(self, other): return _Filter(lambda u:self(u) or other(u))
    def __invert__(self): return _Filter(lambda u:not self(u))
filters = _Obj()
filters.TEXT = _Filter(lambda u: bool(getattr(getattr(u,'message',None),'text',None)))
filters.COMMAND = _Filter(lambda u: bool(getattr(getattr(u,'message',None),'text',None)) and getattr(u.message,'text','').startswith('/'))
filters.Document = _Obj(ALL=_Filter(lambda u: getattr(getattr(u,'message',None),'document',None) is not None))

class _Handler:
    def __init__(self, callback, **kw): self.callback=callback; self.group=kw.get('group',0)
    def check(self, update): return True
class CommandHandler(_Handler):
    def __init__(self, command, callback, **kw): self.command=command; super().__init__(callback,**kw)
    def check(self,u):
        m=getattr(u,'message',None); t=getattr(m,'text','') if m else ''
        return bool(t) and t.split()[0].split('@')[0][1:]==self.command
class MessageHandler(_Handler):
    def __init__(self, filt, callback, **kw): self.filt=filt; super().__init__(callback,**kw)
    def check(self,u): return self.filt(u)
class CallbackQueryHandler(_Handler):
    def __init__(self, callback, pattern=None, **kw): self.pattern=pattern; super().__init__(callback,**kw)
    def check(self,u):
        q=getattr(u,'callback_query',None); d=getattr(q,'data','') if q else ''
        return q is not None and (self.pattern is None or re.search(self.pattern,d))
class TypeHandler(_Handler):
    def __init__(self, typ, callback, **kw): self.typ=typ; super().__init__(callback,**kw)
    def check(self,u): return isinstance(u,self.typ)

class _Context:
    def __init__(self, bot, user_id=None, args=None):
        self.bot=bot; self.args=args or []; self.user_data=bot._user_data.setdefault(user_id or 0,{})
        self.error=None

class _JobQueue:
    def __init__(self, app):
        self.app = app
        self._jobs = []

    def run_repeating(self, callback, interval, first=0, name=None):
        # لا ننشئ asyncio.Task هنا لأن main() يُستدعى قبل تشغيل الـevent loop.
        # يتم تشغيل المهام لاحقًا من داخل Application.run().
        self._jobs.append({
            'callback': callback,
            'interval': interval,
            'first': first,
            'name': name,
        })
        return self._jobs[-1]

    async def _runner(self, job):
        await asyncio.sleep(max(0, job['first']))
        while True:
            try:
                await job['callback'](_Context(self.app.bot))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("job failed: %s", job.get('name') or 'unnamed')
            await asyncio.sleep(max(1, job['interval']))

    def start(self):
        return [asyncio.create_task(self._runner(job), name=job.get('name')) for job in self._jobs]

class Bot:
    def __init__(self, token):
        self.token=token; self.base=f'https://api.telegram.org/bot{token}'; self.id=None; self._user_data={}; self.lock=_threading.RLock()
    def _call(self, method, data=None):
        data={k:v for k,v in (data or {}).items() if v is not None}
        enc={}
        for k,v in data.items():
            if k=='reply_markup': enc[k]=json.dumps(_markup(v), ensure_ascii=False)
            elif isinstance(v,(dict,list,tuple)): enc[k]=json.dumps(v,ensure_ascii=False)
            else: enc[k]=str(v)
        try:
            req=_urlreq.Request(self.base+'/'+method, data=_urlparse.urlencode(enc).encode(), method='POST')
            with _urlreq.urlopen(req, timeout=45) as r: obj=json.loads(r.read().decode())
        except Exception as e: raise NetworkError(str(e))
        if not obj.get('ok'):
            desc=obj.get('description','Telegram API error')
            code=obj.get('error_code',0)
            if code==403: raise Forbidden(desc)
            if code==400: raise BadRequest(desc)
            raise TelegramError(desc)
        return obj.get('result')
    async def initialize(self):
        me=self._call('getMe'); self.id=me.get('id'); return me
    async def send_message(self, chat_id, text, **kwargs):
        r=self._call('sendMessage',{'chat_id':chat_id,'text':text,**kwargs}); return _message_from(r,self)
    async def send_document(self, chat_id, document, caption=None, **kwargs):
        r=self._call('sendDocument',{'chat_id':chat_id,'document':document,'caption':caption,**kwargs}); return _message_from(r,self)
    async def copy_message(self, chat_id, from_chat_id, message_id, **kwargs):
        return self._call('copyMessage',{'chat_id':chat_id,'from_chat_id':from_chat_id,'message_id':message_id,**kwargs})
    async def delete_message(self, chat_id, message_id): return self._call('deleteMessage',{'chat_id':chat_id,'message_id':message_id})
    async def edit_message_text(self, chat_id, message_id, text, **kwargs):
        r=self._call('editMessageText',{'chat_id':chat_id,'message_id':message_id,'text':text,**kwargs}); return _message_from(r,self) if isinstance(r,dict) else r
    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None, **kwargs):
        r=self._call('editMessageReplyMarkup',{'chat_id':chat_id,'message_id':message_id,'reply_markup':reply_markup}); return _message_from(r,self) if isinstance(r,dict) else r
    async def answer_callback_query(self, callback_query_id, text=None, show_alert=False, **kwargs): return self._call('answerCallbackQuery',{'callback_query_id':callback_query_id,'text':text,'show_alert':show_alert})
    async def get_chat(self, chat_id): return _chat_from(self._call('getChat',{'chat_id':chat_id}))
    async def get_chat_member(self, chat_id, user_id): return _Obj(**self._call('getChatMember',{'chat_id':chat_id,'user_id':user_id}))
    async def export_chat_invite_link(self, chat_id): return self._call('exportChatInviteLink',{'chat_id':chat_id})
    async def get_updates(self, offset=None, timeout=40): return self._call('getUpdates',{'offset':offset,'timeout':timeout,'allowed_updates':['message','callback_query']})

def _markup(m):
    if isinstance(m,InlineKeyboardMarkup):
        return {'inline_keyboard':[[{k:v for k,v in {'text':b.text,'callback_data':b.callback_data,'url':b.url}.items() if v is not None} for b in row] for row in m.inline_keyboard]}
    if isinstance(m,ReplyKeyboardMarkup): return {'keyboard':[[{'text':getattr(b,'text',str(b))} for b in row] for row in m.keyboard], **m.kwargs}
    if isinstance(m,ReplyKeyboardRemove): return {'remove_keyboard':True, **m.kwargs}
    return m

def _user_from(x): return User(id=x.get('id'),is_bot=x.get('is_bot',False),first_name=x.get('first_name',''),last_name=x.get('last_name'),username=x.get('username'),language_code=x.get('language_code'))
def _chat_from(x): return Chat(id=x.get('id'),type=x.get('type'),title=x.get('title'),username=x.get('username'),first_name=x.get('first_name'),last_name=x.get('last_name'),full_name=' '.join(filter(None,[x.get('first_name'),x.get('last_name')])),invite_link=x.get('invite_link'))
def _message_from(x,bot):
    if not x: return None
    u=_user_from(x.get('from',{})); c=_chat_from(x.get('chat',{})); doc=x.get('document')
    ents=[]
    for e in x.get('entities') or []: ents.append(MessageEntityObj(type=e.get('type'),custom_emoji_id=e.get('custom_emoji_id')))
    return Message(message_id=x.get('message_id'),date=x.get('date'),from_user=u,chat=c,text=x.get('text'),entities=ents,document=_Obj(**doc) if doc else None,_bot=bot)
class MessageEntityObj(_Obj): pass

def _update_from(x,bot):
    if 'message' in x:
        m=_message_from(x['message'],bot); return Update(update_id=x['update_id'],message=m,effective_message=m,effective_user=m.from_user,callback_query=None)
    if 'callback_query' in x:
        q=x['callback_query']; m=_message_from(q.get('message'),bot); cq=CallbackQuery(id=q.get('id'),data=q.get('data'),message=m,from_user=_user_from(q.get('from',{})),_bot=bot)
        return Update(update_id=x['update_id'],message=None,effective_message=m,effective_user=cq.from_user,callback_query=cq)
    return Update(update_id=x.get('update_id'),effective_user=None,effective_message=None,message=None,callback_query=None)

class Application:
    def __init__(self,token): self.bot=Bot(token); self.handlers=[]; self.error_handlers=[]; self.job_queue=_JobQueue(self); self._running=True
    def add_handler(self,h,group=0): h.group=group; self.handlers.append(h)
    def add_error_handler(self,h): self.error_handlers.append(h)
    async def _dispatch(self,u):
        uid=getattr(getattr(u,'effective_user',None),'id',0); args=[]
        if u.message and u.message.text and u.message.text.startswith('/'):
            args=u.message.text.split()[1:]
        ctx=_Context(self.bot,uid,args)
        for h in sorted(self.handlers,key=lambda x:x.group):
            if h.check(u):
                try: await h.callback(u,ctx)
                except ApplicationHandlerStop: return
                except Exception as e:
                    ctx.error=e
                    for eh in self.error_handlers:
                        try: await eh(u,ctx)
                        except Exception: pass
                    return
                if isinstance(h,CommandHandler): return
    async def run(self):
        await self.bot.initialize()
        job_tasks = self.job_queue.start()
        offset=None
        try:
            while self._running:
                try:
                    updates=await self.bot.get_updates(offset=offset,timeout=40)
                    for raw in updates:
                        offset=raw['update_id']+1
                        await self._dispatch(_update_from(raw,self.bot))
                except KeyboardInterrupt:
                    break
                except Exception:
                    logger.exception('Polling error'); await asyncio.sleep(2)
        finally:
            for task in job_tasks:
                task.cancel()
            if job_tasks:
                await asyncio.gather(*job_tasks, return_exceptions=True)
    def run_polling(self, **kwargs):
        asyncio.run(self.run())
class ApplicationBuilder:
    def __init__(self): self._token=None
    def token(self,t): self._token=t; return self
    def __getattr__(self,n):
        if n in {'concurrent_updates','connection_pool_size','pool_timeout','connect_timeout','read_timeout','write_timeout','get_updates_connection_pool_size','get_updates_read_timeout'}: return lambda *a,**k:self
        raise AttributeError(n)
    def build(self): return Application(self._token)
ApplicationHandlerStop=ApplicationHandlerStop



# ============================================================
#                      قسم الإعدادات (Settings)
# ============================================================
# عدّل القيم التالية قبل التشغيل. معظم الإعدادات المهمة يمكن
# تعديلها لاحقًا من لوحة تحكم المالك داخل البوت دون الحاجة
# لتعديل الكود.

BOT_TOKEN = os.getenv("BOT_TOKEN", "")                 # يوضع في Secrets على منصة الاستضافة
OWNER_ID = int(os.getenv("OWNER_ID", "7661681347"))                            # الآيدي الرقمي لمالك البوت
BOT_USERNAME = os.getenv("BOT_USERNAME", "@Volte_4_bot")                # يوزر البوت بدون @ (لرابط الدعوة)

DEFAULT_DAILY_POINTS = 10                       # نقاط المكافأة اليومية الافتراضية
DEFAULT_REFERRAL_POINTS = 5                     # نقاط الإحالة الافتراضية
DEFAULT_MIN_VIDEO_LIKES = 20                    # الحد الأدنى للإعجابات (يُعرض للمستخدم فقط)

DB_PATH = "keys_store_bot.db"                   # مسار قاعدة البيانات

# ============================================================


# ---------------------------- Logging ----------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("keys_store_bot")


# ============================================================
#                  🌐 نظام تعدد اللغات (i18n)
# ============================================================
# يدعم البوت 5 لغات لكل الواجهة التي يراها المستخدم العادي.
# يتم حفظ لغة كل مستخدم في قاعدة البيانات (عمود language)
# وتُستخدم تلقائيًا في كل رسالة لاحقة له.
# ملاحظة: لوحة تحكم المالك (Admin Panel) تبقى بالعربية لأنها
# مخصصة للمالك فقط، ولتفادي تضخيم الملف بشكل غير ضروري.

LANGS = ["ar", "en", "fr", "es", "tr"]

LANG_NAMES = {
    "ar": "🇸🇦 العربية",
    "en": "🇬🇧 English",
    "fr": "🇫🇷 Français",
    "es": "🇪🇸 Español",
    "tr": "🇹🇷 Türkçe",
}

TRANSLATIONS = {
    "ar": {
        "btn_store": "🛒 المتجر",
        "btn_version": "📱 النسخة",
        "btn_daily": "🎁 النقاط اليومية",
        "btn_account": "👤 حسابي",
        "btn_referral": "🔗 دعوة الأصدقاء",
        "btn_video": "🎥 كود بدون تجميع نقاط",
        "btn_language": "🌐 اللغة",
        "btn_support": "📩 الدعم",
        "btn_admin": "👑 لوحة التحكم",
        "welcome": (
            "👋 أهلًا بك <b>{name}</b> في متجر المفاتيح!\n\n"
            "🛒 يمكنك شراء المفاتيح بالنقاط.\n"
            "🎁 اجمع نقاطًا مجانية يوميًا.\n"
            "🔗 وادعُ أصدقاءك لتحصل على نقاط إضافية.\n\n"
            "اختر من القائمة أدناه 👇"
        ),
        "banned": "🚫 تم حظرك من استخدام هذا البوت.",
        "fallback_menu": "الرجاء استخدام الأزرار الظاهرة أدناه 👇",
        "admin_only": "⛔ هذه الميزة للمالك فقط.",
        "admin_panel_title": "👑 <b>لوحة تحكم المالك</b>\nاختر القسم الذي تريد إدارته:",
        "store_title": "🛒 <b>المتجر</b>\nاختر منتجًا لعرض التفاصيل والشراء 👇",
        "store_empty": "😔 لا توجد منتجات متاحة حاليًا. تابعنا لاحقًا!",
        "store_price_header": "💰 السعر",
        "store_name_header": "📦 الاسم",
        "btn_back": "🔙 رجوع",
        "point_word": "نقطة",
        "available_word": "متوفر",
        "unavailable_word": "غير متوفر",
        "product_not_available": "هذا المنتج غير متوفر.",
        "product_name_label": "📝 اسم السلعة",
        "product_desc_label": "📝 وصف السلعة",
        "product_desc_none": "لا يوجد",
        "product_price_label": "💲 السعر الحالي",
        "product_avail_label": "✅ حالة التوفر",
        "product_confirm": "❓ هل أنت متأكد من رغبتك في الشراء؟",
        "btn_buy_now": "✅ شراء الآن",
        "btn_back_products": "🔙 رجوع للمنتجات",
        "product_out_of_stock": "⚠️ نفدت المفاتيح لهذا المنتج حاليًا.",
        "buy_no_product": "المنتج غير موجود.",
        "buy_no_stock": "😔 نفدت المفاتيح لهذا المنتج.",
        "buy_insufficient": "❌ نقاطك غير كافية. تحتاج {price} نقطة وتملك {points}.",
        "buy_success_alert": "✅ تمت عملية الشراء بنجاح!",
        "receipt_title": "✅ <b>تمت عملية الشراء بنجاح</b>",
        "receipt_product": "📦 المنتج",
        "receipt_duration": "⏳ المدة",
        "receipt_price": "💰 السعر المدفوع",
        "receipt_key": "🔑 المفتاح",
        "receipt_balance": "💳 رصيدك الحالي",
        "receipt_date": "🕒 التاريخ",
        "daily_wait": "⏳ لقد حصلت على نقاطك اليومية مسبقًا.\nالوقت المتبقي: {h} ساعة و {m} دقيقة و {s} ثانية.",
        "daily_success": "🎉 مبروك! حصلت على <b>{points}</b> نقطة يومية.\n💳 رصيدك الحالي: {balance} نقطة.",
        "account_error": "حدث خطأ، أعد إرسال /start.",
        "account_title": "👤 <b>معلومات حسابك</b>",
        "account_name": "📛 الاسم",
        "account_username": "🔖 المعرف",
        "account_id": "🆔 آيدي تيليجرام",
        "account_points": "💰 النقاط الحالية",
        "account_total_points": "📈 إجمالي النقاط المكتسبة",
        "account_purchases": "🛍️ عدد المشتريات",
        "account_referrals": "👥 عدد الإحالات",
        "account_referral_points": "🎁 نقاط من الإحالات",
        "account_joined": "📅 تاريخ الانضمام",
        "account_last_active": "🕒 آخر نشاط",
        "referral_title": "🔗 <b>رابط الدعوة الخاص بك</b>",
        "referral_count": "👥 عدد من دعوتهم",
        "referral_points": "🎁 النقاط المكتسبة من الإحالات",
        "referral_note": "كل صديق جديد يدخل عبر رابطك يمنحك {points} نقطة.",
        "video_title": "🎥 <b>الحصول على مفتاح بدون تجميع نقاط</b>",
        "video_steps_label": "الخطوات:",
        "video_step1": "1️⃣ قم بتصوير فيديو على الهاك.",
        "video_step2": "2️⃣ انشر الفيديو على حسابك.",
        "video_step3": "3️⃣ يجب أن يحصل الفيديو على {likes} إعجابًا على الأقل.",
        "video_step4": "4️⃣ أرسل رابط الفيديو هنا في البوت.",
        "video_note": "بعد الإرسال سيتم تحويل طلبك إلى المالك للمراجعة، ولن يتم اعتماد الفيديو تلقائيًا؛ القرار النهائي للمالك.",
        "video_send_prompt": "📎 أرسل الآن رابط الفيديو، أو اضغط رجوع.",
        "video_invalid_link": "⚠️ الرجاء إرسال رابط صحيح يبدأ بـ http:// أو https://",
        "video_submitted": "✅ تم إرسال طلبك جاري المراجعة الرجاء الانتظار.",
        "lang_prompt": "🌐 اختر لغتك المفضلة:",
        "lang_saved": "✅ تم تغيير لغة البوت إلى: {lang_name}",
        "support_prompt": "✍️ أرسل رسالتك وسيتم إرسالها إلى مالك البوت.",
        "support_sent": "✅ تم إرسال رسالتك إلى مالك البوت بنجاح.",
        "version_not_available": "😔 لا تتوفر نسخة تطبيق حاليًا. حاول لاحقًا.",
        "version_sent": "✅ تم إرسال ملف النسخة إليك.",
        "version_send_failed": "❌ تعذر إرسال ملف النسخة، حاول مرة أخرى لاحقًا.",
        "account_transfer_btn": "🔄 تحويل النقاط",
        "xfer_ask_target": "✏️ أرسل يوزر (Username) أو آيدي تيليجرام للشخص الذي تريد تحويل النقاط إليه:",
        "xfer_target_not_found": "❌ لم يتم العثور على هذا المستخدم، أو أنه لم يستخدم البوت من قبل. حاول مجددًا:",
        "xfer_target_self": "⚠️ لا يمكنك تحويل النقاط لنفسك. أرسل يوزر أو آيدي شخص آخر:",
        "xfer_ask_amount": "✏️ أرسل عدد النقاط التي تريد تحويلها (رقم صحيح موجب):",
        "xfer_invalid_amount": "⚠️ الرجاء إرسال رقم صحيح موجب فقط (بدون كسور أو رموز).",
        "xfer_insufficient": "❌ رصيدك غير كافٍ لإتمام هذا التحويل.\nرصيدك الحالي: {balance} نقطة.",
        "xfer_confirm_title": "🔄 <b>تأكيد عملية التحويل</b>",
        "xfer_confirm_details": (
            "💳 قيمة التحويل: <b>{amount}</b> نقطة\n"
            "➖ العمولة (10%): <b>{commission}</b> نقطة\n"
            "📤 سيتم خصم: <b>{amount}</b> نقطة من رصيدك\n"
            "📥 سيصل للمستلم: <b>{net}</b> نقطة\n\n"
            "هل تريد تأكيد التحويل؟"
        ),
        "xfer_btn_confirm": "✅ تأكيد التحويل",
        "xfer_btn_cancel": "❌ إلغاء",
        "xfer_cancelled": "🚫 تم إلغاء عملية التحويل.",
        "xfer_no_pending": "⚠️ لا توجد عملية تحويل معلّقة.",
        "xfer_error": "❌ حدث خطأ أثناء تنفيذ التحويل. حاول مرة أخرى لاحقًا.",
        "xfer_success_sender": (
            "✅ تم تحويل <b>{net}</b> نقطة بنجاح.\n"
            "➖ العمولة: {commission} نقطة\n"
            "💳 رصيدك الحالي: <b>{balance}</b> نقطة."
        ),
        "xfer_success_receiver": (
            "🎉 استلمت <b>{net}</b> نقطة من {name}.\n"
            "💳 رصيدك الحالي: <b>{balance}</b> نقطة."
        ),
        "fsub_prompt_title": "🔒 <b>اشتراك إجباري</b>",
        "fsub_prompt_note": "للاستمرار في استخدام البوت، يجب عليك الاشتراك في القنوات/المجموعات التالية أولًا، ثم اضغط زر «تحقق من الاشتراك» 👇",
        "fsub_btn_check": "✅ تحقق من الاشتراك",
        "fsub_still_missing": "⚠️ لم تشترك بعد في جميع القنوات/المجموعات المطلوبة.",
        "fsub_all_done": "✅ تم التحقق من اشتراكك بنجاح!",
    },
    "en": {
        "btn_store": "🛒 Store",
        "btn_version": "📱 App Version",
        "btn_daily": "🎁 Daily Points",
        "btn_account": "👤 My Account",
        "btn_referral": "🔗 Invite Friends",
        "btn_video": "🎥 Free Key (Video)",
        "btn_language": "🌐 Language",
        "btn_support": "📩 Support",
        "btn_admin": "👑 Admin Panel",
        "welcome": (
            "👋 Welcome <b>{name}</b> to the Keys Store!\n\n"
            "🛒 Buy keys using points.\n"
            "🎁 Collect free points every day.\n"
            "🔗 Invite your friends to earn extra points.\n\n"
            "Choose an option below 👇"
        ),
        "banned": "🚫 You have been banned from using this bot.",
        "fallback_menu": "Please use the menu buttons shown below 👇",
        "admin_only": "⛔ This feature is for the owner only.",
        "admin_panel_title": "👑 <b>Owner Admin Panel</b>\nChoose a section to manage:",
        "store_title": "🛒 <b>Store</b>\nChoose a product to view details and buy 👇",
        "store_empty": "😔 No products available right now. Check back later!",
        "store_price_header": "💰 Price",
        "store_name_header": "📦 Name",
        "btn_back": "🔙 Back",
        "point_word": "points",
        "available_word": "available",
        "unavailable_word": "unavailable",
        "product_not_available": "This product is not available.",
        "product_name_label": "📝 Product name",
        "product_desc_label": "📝 Description",
        "product_desc_none": "None",
        "product_price_label": "💲 Current price",
        "product_avail_label": "✅ Availability",
        "product_confirm": "❓ Are you sure you want to buy this?",
        "btn_buy_now": "✅ Buy Now",
        "btn_back_products": "🔙 Back to products",
        "product_out_of_stock": "⚠️ This product is currently out of stock.",
        "buy_no_product": "Product not found.",
        "buy_no_stock": "😔 This product is out of stock.",
        "buy_insufficient": "❌ Insufficient points. You need {price} and have {points}.",
        "buy_success_alert": "✅ Purchase completed successfully!",
        "receipt_title": "✅ <b>Purchase completed successfully</b>",
        "receipt_product": "📦 Product",
        "receipt_duration": "⏳ Duration",
        "receipt_price": "💰 Price paid",
        "receipt_key": "🔑 Key",
        "receipt_balance": "💳 Your current balance",
        "receipt_date": "🕒 Date",
        "daily_wait": "⏳ You've already claimed your daily points.\nTime remaining: {h}h {m}m {s}s.",
        "daily_success": "🎉 Congrats! You earned <b>{points}</b> daily points.\n💳 Your balance: {balance} points.",
        "account_error": "An error occurred, please resend /start.",
        "account_title": "👤 <b>Your account info</b>",
        "account_name": "📛 Name",
        "account_username": "🔖 Username",
        "account_id": "🆔 Telegram ID",
        "account_points": "💰 Current points",
        "account_total_points": "📈 Total points earned",
        "account_purchases": "🛍️ Purchases count",
        "account_referrals": "👥 Referrals count",
        "account_referral_points": "🎁 Points from referrals",
        "account_joined": "📅 Joined on",
        "account_last_active": "🕒 Last active",
        "referral_title": "🔗 <b>Your invite link</b>",
        "referral_count": "👥 People invited",
        "referral_points": "🎁 Points earned from referrals",
        "referral_note": "Every new friend who joins via your link earns you {points} points.",
        "video_title": "🎥 <b>Get a free key without collecting points</b>",
        "video_steps_label": "Steps:",
        "video_step1": "1️⃣ Record a video explaining the app/version.",
        "video_step2": "2️⃣ Post the video on your account.",
        "video_step3": "3️⃣ The video must get at least {likes} likes.",
        "video_step4": "4️⃣ Send the video link here in the bot.",
        "video_note": "After sending, your request will be forwarded to the owner for review; it won't be approved automatically — the final decision is the owner's.",
        "video_send_prompt": "📎 Now send the video link, or press back.",
        "video_invalid_link": "⚠️ Please send a valid link starting with http:// or https://",
        "video_submitted": "✅ Your request was sent successfully, it will be reviewed by the owner soon.",
        "lang_prompt": "🌐 Choose your preferred language:",
        "lang_saved": "✅ Bot language changed to: {lang_name}",
        "support_prompt": "✍️ Send your message and it will be forwarded to the bot owner.",
        "support_sent": "✅ Your message has been sent to the bot owner successfully.",
        "version_not_available": "😔 No app version is available right now. Please try again later.",
        "version_sent": "✅ The app file has been sent to you.",
        "version_send_failed": "❌ Failed to send the app file, please try again later.",
        "account_transfer_btn": "🔄 Transfer Points",
        "xfer_ask_target": "✏️ Send the username or Telegram ID of the person you want to transfer points to:",
        "xfer_target_not_found": "❌ User not found, or they haven't used the bot before. Try again:",
        "xfer_target_self": "⚠️ You can't transfer points to yourself. Send someone else's username or ID:",
        "xfer_ask_amount": "✏️ Send the number of points to transfer (a positive whole number):",
        "xfer_invalid_amount": "⚠️ Please send a valid positive whole number only.",
        "xfer_insufficient": "❌ Insufficient balance for this transfer.\nYour current balance: {balance} points.",
        "xfer_confirm_title": "🔄 <b>Confirm Transfer</b>",
        "xfer_confirm_details": (
            "💳 Transfer amount: <b>{amount}</b> points\n"
            "➖ Commission (10%): <b>{commission}</b> points\n"
            "📤 Will be deducted: <b>{amount}</b> points from your balance\n"
            "📥 Recipient will receive: <b>{net}</b> points\n\n"
            "Do you want to confirm the transfer?"
        ),
        "xfer_btn_confirm": "✅ Confirm Transfer",
        "xfer_btn_cancel": "❌ Cancel",
        "xfer_cancelled": "🚫 Transfer cancelled.",
        "xfer_no_pending": "⚠️ There is no pending transfer.",
        "xfer_error": "❌ An error occurred while processing the transfer. Please try again later.",
        "xfer_success_sender": (
            "✅ Successfully transferred <b>{net}</b> points.\n"
            "➖ Commission: {commission} points\n"
            "💳 Your current balance: <b>{balance}</b> points."
        ),
        "xfer_success_receiver": (
            "🎉 You received <b>{net}</b> points from {name}.\n"
            "💳 Your current balance: <b>{balance}</b> points."
        ),
        "fsub_prompt_title": "🔒 <b>Mandatory Subscription</b>",
        "fsub_prompt_note": "To keep using the bot, you must first join the channels/groups below, then press the \"Check Subscription\" button 👇",
        "fsub_btn_check": "✅ Check Subscription",
        "fsub_still_missing": "⚠️ You haven't joined all the required channels/groups yet.",
        "fsub_all_done": "✅ Your subscription has been verified!",
    },
    "fr": {
        "btn_store": "🛒 Boutique",
        "btn_version": "📱 Version de l'app",
        "btn_daily": "🎁 Points quotidiens",
        "btn_account": "👤 Mon compte",
        "btn_referral": "🔗 Inviter des amis",
        "btn_video": "🎥 Clé gratuite (vidéo)",
        "btn_language": "🌐 Langue",
        "btn_support": "📩 Support",
        "btn_admin": "👑 Panneau admin",
        "welcome": (
            "👋 Bienvenue <b>{name}</b> dans la boutique de clés !\n\n"
            "🛒 Achetez des clés avec des points.\n"
            "🎁 Gagnez des points gratuits chaque jour.\n"
            "🔗 Invitez vos amis pour gagner des points supplémentaires.\n\n"
            "Choisissez une option ci-dessous 👇"
        ),
        "banned": "🚫 Vous avez été banni de ce bot.",
        "fallback_menu": "Merci d'utiliser les boutons du menu ci-dessous 👇",
        "admin_only": "⛔ Fonctionnalité réservée au propriétaire.",
        "admin_panel_title": "👑 <b>Panneau du propriétaire</b>\nChoisissez une section à gérer :",
        "store_title": "🛒 <b>Boutique</b>\nChoisissez un produit pour voir les détails et acheter 👇",
        "store_empty": "😔 Aucun produit disponible pour le moment. Revenez plus tard !",
        "store_price_header": "💰 Prix",
        "store_name_header": "📦 Nom",
        "btn_back": "🔙 Retour",
        "point_word": "points",
        "available_word": "disponible",
        "unavailable_word": "indisponible",
        "product_not_available": "Ce produit n'est pas disponible.",
        "product_name_label": "📝 Nom du produit",
        "product_desc_label": "📝 Description",
        "product_desc_none": "Aucune",
        "product_price_label": "💲 Prix actuel",
        "product_avail_label": "✅ Disponibilité",
        "product_confirm": "❓ Confirmez-vous vouloir acheter ce produit ?",
        "btn_buy_now": "✅ Acheter maintenant",
        "btn_back_products": "🔙 Retour aux produits",
        "product_out_of_stock": "⚠️ Ce produit est actuellement en rupture de stock.",
        "buy_no_product": "Produit introuvable.",
        "buy_no_stock": "😔 Ce produit est en rupture de stock.",
        "buy_insufficient": "❌ Points insuffisants. Il faut {price} et vous avez {points}.",
        "buy_success_alert": "✅ Achat effectué avec succès !",
        "receipt_title": "✅ <b>Achat effectué avec succès</b>",
        "receipt_product": "📦 Produit",
        "receipt_duration": "⏳ Durée",
        "receipt_price": "💰 Prix payé",
        "receipt_key": "🔑 Clé",
        "receipt_balance": "💳 Votre solde actuel",
        "receipt_date": "🕒 Date",
        "daily_wait": "⏳ Vous avez déjà réclamé vos points quotidiens.\nTemps restant : {h}h {m}m {s}s.",
        "daily_success": "🎉 Félicitations ! Vous avez gagné <b>{points}</b> points quotidiens.\n💳 Votre solde : {balance} points.",
        "account_error": "Une erreur est survenue, veuillez renvoyer /start.",
        "account_title": "👤 <b>Informations de votre compte</b>",
        "account_name": "📛 Nom",
        "account_username": "🔖 Nom d'utilisateur",
        "account_id": "🆔 ID Telegram",
        "account_points": "💰 Points actuels",
        "account_total_points": "📈 Total des points gagnés",
        "account_purchases": "🛍️ Nombre d'achats",
        "account_referrals": "👥 Nombre de filleuls",
        "account_referral_points": "🎁 Points issus des parrainages",
        "account_joined": "📅 Date d'inscription",
        "account_last_active": "🕒 Dernière activité",
        "referral_title": "🔗 <b>Votre lien d'invitation</b>",
        "referral_count": "👥 Personnes invitées",
        "referral_points": "🎁 Points gagnés grâce aux parrainages",
        "referral_note": "Chaque nouvel ami qui rejoint via votre lien vous rapporte {points} points.",
        "video_title": "🎥 <b>Obtenir une clé gratuite sans points</b>",
        "video_steps_label": "Étapes :",
        "video_step1": "1️⃣ Filmez une vidéo expliquant l'application/version.",
        "video_step2": "2️⃣ Publiez la vidéo sur votre compte.",
        "video_step3": "3️⃣ La vidéo doit obtenir au moins {likes} likes.",
        "video_step4": "4️⃣ Envoyez le lien de la vidéo ici dans le bot.",
        "video_note": "Après l'envoi, votre demande sera transmise au propriétaire pour examen ; elle ne sera pas approuvée automatiquement, la décision finale revient au propriétaire.",
        "video_send_prompt": "📎 Envoyez maintenant le lien de la vidéo, ou appuyez sur retour.",
        "video_invalid_link": "⚠️ Merci d'envoyer un lien valide commençant par http:// ou https://",
        "video_submitted": "✅ Votre demande a été envoyée avec succès, elle sera bientôt examinée par le propriétaire.",
        "lang_prompt": "🌐 Choisissez votre langue préférée :",
        "lang_saved": "✅ Langue du bot changée en : {lang_name}",
        "support_prompt": "✍️ Envoyez votre message, il sera transmis au propriétaire du bot.",
        "support_sent": "✅ Votre message a été envoyé avec succès au propriétaire du bot.",
        "version_not_available": "😔 Aucune version de l'application n'est disponible pour le moment. Réessayez plus tard.",
        "version_sent": "✅ Le fichier de l'application vous a été envoyé.",
        "version_send_failed": "❌ Échec de l'envoi du fichier, réessayez plus tard.",
        "account_transfer_btn": "🔄 Transférer des points",
        "xfer_ask_target": "✏️ Envoyez le nom d'utilisateur ou l'ID Telegram de la personne à qui transférer des points :",
        "xfer_target_not_found": "❌ Utilisateur introuvable, ou il n'a jamais utilisé le bot. Réessayez :",
        "xfer_target_self": "⚠️ Vous ne pouvez pas vous transférer des points à vous-même. Envoyez le nom d'utilisateur ou l'ID d'une autre personne :",
        "xfer_ask_amount": "✏️ Envoyez le nombre de points à transférer (un nombre entier positif) :",
        "xfer_invalid_amount": "⚠️ Merci d'envoyer uniquement un nombre entier positif valide.",
        "xfer_insufficient": "❌ Solde insuffisant pour ce transfert.\nVotre solde actuel : {balance} points.",
        "xfer_confirm_title": "🔄 <b>Confirmer le transfert</b>",
        "xfer_confirm_details": (
            "💳 Montant du transfert : <b>{amount}</b> points\n"
            "➖ Commission (10 %) : <b>{commission}</b> points\n"
            "📤 Sera déduit : <b>{amount}</b> points de votre solde\n"
            "📥 Le destinataire recevra : <b>{net}</b> points\n\n"
            "Voulez-vous confirmer le transfert ?"
        ),
        "xfer_btn_confirm": "✅ Confirmer le transfert",
        "xfer_btn_cancel": "❌ Annuler",
        "xfer_cancelled": "🚫 Transfert annulé.",
        "xfer_no_pending": "⚠️ Aucun transfert en attente.",
        "xfer_error": "❌ Une erreur est survenue lors du transfert. Réessayez plus tard.",
        "xfer_success_sender": (
            "✅ <b>{net}</b> points transférés avec succès.\n"
            "➖ Commission : {commission} points\n"
            "💳 Votre solde actuel : <b>{balance}</b> points."
        ),
        "xfer_success_receiver": (
            "🎉 Vous avez reçu <b>{net}</b> points de {name}.\n"
            "💳 Votre solde actuel : <b>{balance}</b> points."
        ),
        "fsub_prompt_title": "🔒 <b>Abonnement obligatoire</b>",
        "fsub_prompt_note": "Pour continuer à utiliser le bot, vous devez d'abord rejoindre les canaux/groupes ci-dessous, puis appuyer sur « Vérifier l'abonnement » 👇",
        "fsub_btn_check": "✅ Vérifier l'abonnement",
        "fsub_still_missing": "⚠️ Vous n'avez pas encore rejoint tous les canaux/groupes requis.",
        "fsub_all_done": "✅ Votre abonnement a été vérifié !",
    },
    "es": {
        "btn_store": "🛒 Tienda",
        "btn_version": "📱 Versión de la app",
        "btn_daily": "🎁 Puntos diarios",
        "btn_account": "👤 Mi cuenta",
        "btn_referral": "🔗 Invitar amigos",
        "btn_video": "🎥 Clave gratis (vídeo)",
        "btn_language": "🌐 Idioma",
        "btn_support": "📩 Soporte",
        "btn_admin": "👑 Panel de admin",
        "welcome": (
            "👋 Bienvenido/a <b>{name}</b> a la tienda de claves!\n\n"
            "🛒 Compra claves usando puntos.\n"
            "🎁 Reúne puntos gratis cada día.\n"
            "🔗 Invita a tus amigos para ganar puntos extra.\n\n"
            "Elige una opción abajo 👇"
        ),
        "banned": "🚫 Has sido bloqueado de este bot.",
        "fallback_menu": "Por favor usa los botones del menú de abajo 👇",
        "admin_only": "⛔ Esta función es solo para el propietario.",
        "admin_panel_title": "👑 <b>Panel del propietario</b>\nElige una sección para administrar:",
        "store_title": "🛒 <b>Tienda</b>\nElige un producto para ver detalles y comprar 👇",
        "store_empty": "😔 No hay productos disponibles ahora. ¡Vuelve más tarde!",
        "store_price_header": "💰 Precio",
        "store_name_header": "📦 Nombre",
        "btn_back": "🔙 Volver",
        "point_word": "puntos",
        "available_word": "disponible",
        "unavailable_word": "no disponible",
        "product_not_available": "Este producto no está disponible.",
        "product_name_label": "📝 Nombre del producto",
        "product_desc_label": "📝 Descripción",
        "product_desc_none": "Ninguna",
        "product_price_label": "💲 Precio actual",
        "product_avail_label": "✅ Disponibilidad",
        "product_confirm": "❓ ¿Confirmas que deseas comprarlo?",
        "btn_buy_now": "✅ Comprar ahora",
        "btn_back_products": "🔙 Volver a productos",
        "product_out_of_stock": "⚠️ Este producto está agotado actualmente.",
        "buy_no_product": "Producto no encontrado.",
        "buy_no_stock": "😔 Este producto está agotado.",
        "buy_insufficient": "❌ Puntos insuficientes. Necesitas {price} y tienes {points}.",
        "buy_success_alert": "✅ ¡Compra realizada con éxito!",
        "receipt_title": "✅ <b>Compra realizada con éxito</b>",
        "receipt_product": "📦 Producto",
        "receipt_duration": "⏳ Duración",
        "receipt_price": "💰 Precio pagado",
        "receipt_key": "🔑 Clave",
        "receipt_balance": "💳 Tu saldo actual",
        "receipt_date": "🕒 Fecha",
        "daily_wait": "⏳ Ya reclamaste tus puntos diarios.\nTiempo restante: {h}h {m}m {s}s.",
        "daily_success": "🎉 ¡Felicidades! Ganaste <b>{points}</b> puntos diarios.\n💳 Tu saldo: {balance} puntos.",
        "account_error": "Ocurrió un error, vuelve a enviar /start.",
        "account_title": "👤 <b>Información de tu cuenta</b>",
        "account_name": "📛 Nombre",
        "account_username": "🔖 Usuario",
        "account_id": "🆔 ID de Telegram",
        "account_points": "💰 Puntos actuales",
        "account_total_points": "📈 Total de puntos ganados",
        "account_purchases": "🛍️ Número de compras",
        "account_referrals": "👥 Número de referidos",
        "account_referral_points": "🎁 Puntos por referidos",
        "account_joined": "📅 Fecha de registro",
        "account_last_active": "🕒 Última actividad",
        "referral_title": "🔗 <b>Tu enlace de invitación</b>",
        "referral_count": "👥 Personas invitadas",
        "referral_points": "🎁 Puntos ganados por referidos",
        "referral_note": "Cada nuevo amigo que se una con tu enlace te da {points} puntos.",
        "video_title": "🎥 <b>Obtener una clave gratis sin acumular puntos</b>",
        "video_steps_label": "Pasos:",
        "video_step1": "1️⃣ Graba un vídeo explicando la app/versión.",
        "video_step2": "2️⃣ Publica el vídeo en tu cuenta.",
        "video_step3": "3️⃣ El vídeo debe tener al menos {likes} me gusta.",
        "video_step4": "4️⃣ Envía el enlace del vídeo aquí en el bot.",
        "video_note": "Tras el envío, tu solicitud se reenviará al propietario para revisión; no se aprobará automáticamente, la decisión final es del propietario.",
        "video_send_prompt": "📎 Envía ahora el enlace del vídeo, o pulsa volver.",
        "video_invalid_link": "⚠️ Envía un enlace válido que comience con http:// o https://",
        "video_submitted": "✅ Tu solicitud se envió con éxito, será revisada por el propietario pronto.",
        "lang_prompt": "🌐 Elige tu idioma preferido:",
        "lang_saved": "✅ Idioma del bot cambiado a: {lang_name}",
        "support_prompt": "✍️ Envía tu mensaje y será reenviado al propietario del bot.",
        "support_sent": "✅ Tu mensaje se envió correctamente al propietario del bot.",
        "version_not_available": "😔 No hay ninguna versión de la app disponible por ahora. Inténtalo más tarde.",
        "version_sent": "✅ Se te envió el archivo de la app.",
        "version_send_failed": "❌ No se pudo enviar el archivo, inténtalo de nuevo más tarde.",
        "account_transfer_btn": "🔄 Transferir puntos",
        "xfer_ask_target": "✏️ Envía el usuario o el ID de Telegram de la persona a la que quieres transferir puntos:",
        "xfer_target_not_found": "❌ Usuario no encontrado, o nunca ha usado el bot. Inténtalo de nuevo:",
        "xfer_target_self": "⚠️ No puedes transferirte puntos a ti mismo. Envía el usuario o ID de otra persona:",
        "xfer_ask_amount": "✏️ Envía la cantidad de puntos a transferir (un número entero positivo):",
        "xfer_invalid_amount": "⚠️ Por favor envía solo un número entero positivo válido.",
        "xfer_insufficient": "❌ Saldo insuficiente para esta transferencia.\nTu saldo actual: {balance} puntos.",
        "xfer_confirm_title": "🔄 <b>Confirmar transferencia</b>",
        "xfer_confirm_details": (
            "💳 Monto a transferir: <b>{amount}</b> puntos\n"
            "➖ Comisión (10%): <b>{commission}</b> puntos\n"
            "📤 Se descontará: <b>{amount}</b> puntos de tu saldo\n"
            "📥 El destinatario recibirá: <b>{net}</b> puntos\n\n"
            "¿Deseas confirmar la transferencia?"
        ),
        "xfer_btn_confirm": "✅ Confirmar transferencia",
        "xfer_btn_cancel": "❌ Cancelar",
        "xfer_cancelled": "🚫 Transferencia cancelada.",
        "xfer_no_pending": "⚠️ No hay ninguna transferencia pendiente.",
        "xfer_error": "❌ Ocurrió un error al procesar la transferencia. Inténtalo más tarde.",
        "xfer_success_sender": (
            "✅ Se transfirieron <b>{net}</b> puntos con éxito.\n"
            "➖ Comisión: {commission} puntos\n"
            "💳 Tu saldo actual: <b>{balance}</b> puntos."
        ),
        "xfer_success_receiver": (
            "🎉 Recibiste <b>{net}</b> puntos de {name}.\n"
            "💳 Tu saldo actual: <b>{balance}</b> puntos."
        ),
        "fsub_prompt_title": "🔒 <b>Suscripción obligatoria</b>",
        "fsub_prompt_note": "Para seguir usando el bot, primero debes unirte a los siguientes canales/grupos y luego pulsar «Verificar suscripción» 👇",
        "fsub_btn_check": "✅ Verificar suscripción",
        "fsub_still_missing": "⚠️ Aún no te has unido a todos los canales/grupos requeridos.",
        "fsub_all_done": "✅ ¡Tu suscripción ha sido verificada!",
    },
    "tr": {
        "btn_store": "🛒 Mağaza",
        "btn_daily": "🎁 Günlük Puanlar",
        "btn_account": "👤 Hesabım",
        "btn_referral": "🔗 Arkadaş Davet Et",
        "btn_video": "🎥 Puansız Ücretsiz Kod",
        "btn_language": "🌐 Dil",
        "btn_support": "📩 Destek",
        "btn_admin": "👑 Yönetim Paneli",
        "welcome": (
            "👋 Anahtar Mağazasına hoş geldin <b>{name}</b>!\n\n"
            "🛒 Puanlarla anahtar satın alabilirsin.\n"
            "🎁 Her gün ücretsiz puan topla.\n"
            "🔗 Arkadaşlarını davet ederek ekstra puan kazan.\n\n"
            "Aşağıdan bir seçenek seç 👇"
        ),
        "banned": "🚫 Bu botu kullanmaktan yasaklandın.",
        "fallback_menu": "Lütfen aşağıdaki menü düğmelerini kullan 👇",
        "admin_only": "⛔ Bu özellik yalnızca sahibi içindir.",
        "admin_panel_title": "👑 <b>Sahip Yönetim Paneli</b>\nYönetmek istediğin bölümü seç:",
        "store_title": "🛒 <b>Mağaza</b>\nDetayları görmek ve satın almak için bir ürün seç 👇",
        "store_empty": "😔 Şu anda mevcut ürün yok. Daha sonra tekrar bak!",
        "store_price_header": "💰 Fiyat",
        "store_name_header": "📦 Ad",
        "btn_back": "🔙 Geri",
        "point_word": "puan",
        "available_word": "mevcut",
        "unavailable_word": "mevcut değil",
        "product_not_available": "Bu ürün mevcut değil.",
        "product_name_label": "📝 Ürün adı",
        "product_desc_label": "📝 Ürün açıklaması",
        "product_desc_none": "Yok",
        "product_price_label": "💲 Güncel fiyat",
        "product_avail_label": "✅ Stok durumu",
        "product_confirm": "❓ Satın almak istediğinden emin misin?",
        "btn_buy_now": "✅ Şimdi Satın Al",
        "btn_back_products": "🔙 Ürünlere dön",
        "product_out_of_stock": "⚠️ Bu ürün şu anda tükendi.",
        "buy_no_product": "Ürün bulunamadı.",
        "buy_no_stock": "😔 Bu ürün stokta yok.",
        "buy_insufficient": "❌ Puanın yetersiz. {price} puan gerekiyor, {points} puanın var.",
        "buy_success_alert": "✅ Satın alma başarıyla tamamlandı!",
        "receipt_title": "✅ <b>Satın alma başarıyla tamamlandı</b>",
        "receipt_product": "📦 Ürün",
        "receipt_duration": "⏳ Süre",
        "receipt_price": "💰 Ödenen fiyat",
        "receipt_key": "🔑 Anahtar",
        "receipt_balance": "💳 Güncel bakiyen",
        "receipt_date": "🕒 Tarih",
        "daily_wait": "⏳ Günlük puanını zaten aldın.\nKalan süre: {h} saat {m} dakika {s} saniye.",
        "daily_success": "🎉 Tebrikler! <b>{points}</b> günlük puan kazandın.\n💳 Bakiyen: {balance} puan.",
        "account_error": "Bir hata oluştu, lütfen /start komutunu tekrar gönder.",
        "account_title": "👤 <b>Hesap bilgilerin</b>",
        "account_name": "📛 Ad",
        "account_username": "🔖 Kullanıcı adı",
        "account_id": "🆔 Telegram ID",
        "account_points": "💰 Güncel puan",
        "account_total_points": "📈 Toplam kazanılan puan",
        "account_purchases": "🛍️ Satın alma sayısı",
        "account_referrals": "👥 Davet sayısı",
        "account_referral_points": "🎁 Davetlerden kazanılan puan",
        "account_joined": "📅 Katılma tarihi",
        "account_last_active": "🕒 Son aktiflik",
        "referral_title": "🔗 <b>Davet linkin</b>",
        "referral_count": "👥 Davet ettiklerin",
        "referral_points": "🎁 Davetlerden kazanılan puan",
        "referral_note": "Linkinle katılan her yeni arkadaş sana {points} puan kazandırır.",
        "video_title": "🎥 <b>Puan toplamadan ücretsiz anahtar al</b>",
        "video_steps_label": "Adımlar:",
        "video_step1": "1️⃣ Uygulamayı/sürümü anlatan bir video çek.",
        "video_step2": "2️⃣ Videoyu hesabında paylaş.",
        "video_step3": "3️⃣ Video en az {likes} beğeni almalı.",
        "video_step4": "4️⃣ Video linkini buraya, bota gönder.",
        "video_note": "Gönderdikten sonra talebin incelenmek üzere sahibe iletilecek; otomatik onaylanmaz, son karar sahibine aittir.",
        "video_send_prompt": "📎 Şimdi video linkini gönder, ya da geri'ye bas.",
        "video_invalid_link": "⚠️ Lütfen http:// veya https:// ile başlayan geçerli bir link gönder.",
        "video_submitted": "✅ Talebin başarıyla gönderildi, sahibi tarafından yakında incelenecek.",
        "lang_prompt": "🌐 Tercih ettiğin dili seç:",
        "lang_saved": "✅ Bot dili şuna değiştirildi: {lang_name}",
        "support_prompt": "✍️ Mesajını gönder, bot sahibine iletilecek.",
        "support_sent": "✅ Mesajın bot sahibine başarıyla gönderildi.",
        "account_transfer_btn": "🔄 Puan Transferi",
        "xfer_ask_target": "✏️ Puan göndermek istediğin kişinin kullanıcı adını veya Telegram ID'sini gönder:",
        "xfer_target_not_found": "❌ Kullanıcı bulunamadı veya botu hiç kullanmamış. Tekrar dene:",
        "xfer_target_self": "⚠️ Kendine puan transfer edemezsin. Başka birinin kullanıcı adını veya ID'sini gönder:",
        "xfer_ask_amount": "✏️ Transfer etmek istediğin puan miktarını gönder (pozitif tam sayı):",
        "xfer_invalid_amount": "⚠️ Lütfen sadece geçerli, pozitif bir tam sayı gönder.",
        "xfer_insufficient": "❌ Bu transfer için bakiyen yetersiz.\nGüncel bakiyen: {balance} puan.",
        "xfer_confirm_title": "🔄 <b>Transferi Onayla</b>",
        "xfer_confirm_details": (
            "💳 Transfer tutarı: <b>{amount}</b> puan\n"
            "➖ Komisyon (%10): <b>{commission}</b> puan\n"
            "📤 Bakiyenden düşülecek: <b>{amount}</b> puan\n"
            "📥 Alıcıya ulaşacak: <b>{net}</b> puan\n\n"
            "Transferi onaylamak istiyor musun?"
        ),
        "xfer_btn_confirm": "✅ Transferi Onayla",
        "xfer_btn_cancel": "❌ İptal",
        "xfer_cancelled": "🚫 Transfer iptal edildi.",
        "xfer_no_pending": "⚠️ Bekleyen bir transfer yok.",
        "xfer_error": "❌ Transfer işlenirken bir hata oluştu. Lütfen daha sonra tekrar dene.",
        "xfer_success_sender": (
            "✅ <b>{net}</b> puan başarıyla transfer edildi.\n"
            "➖ Komisyon: {commission} puan\n"
            "💳 Güncel bakiyen: <b>{balance}</b> puan."
        ),
        "xfer_success_receiver": (
            "🎉 {name} kişisinden <b>{net}</b> puan aldın.\n"
            "💳 Güncel bakiyen: <b>{balance}</b> puan."
        ),
        "fsub_prompt_title": "🔒 <b>Zorunlu Abonelik</b>",
        "fsub_prompt_note": "Botu kullanmaya devam etmek için önce aşağıdaki kanallara/gruplara katılmalı, ardından \"Aboneliği Kontrol Et\" düğmesine basmalısın 👇",
        "fsub_btn_check": "✅ Aboneliği Kontrol Et",
        "fsub_still_missing": "⚠️ Gerekli kanal/grupların tümüne henüz katılmadın.",
        "fsub_all_done": "✅ Aboneliğin başarıyla doğrulandı!",
    },
}


def t(lang: str, key: str, **kwargs) -> str:
    """يعيد النص المترجم لمفتاح معيّن حسب اللغة، مع دعم placeholders."""
    lang = lang if lang in TRANSLATIONS else "ar"
    text = TRANSLATIONS[lang].get(key) or TRANSLATIONS["ar"].get(key, key)
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, IndexError):
            pass
    return text


def lang_cancel_kb(lang: str) -> InlineKeyboardMarkup:
    """زر الرجوع (يحل محل زر إلغاء العملية سابقًا)، أخضر 🟢 دائمًا."""
    return InlineKeyboardMarkup(
        [[back_btn(t(lang, "btn_back"), "flow:back")]]
    )


# ============================================================
#                       طبقة قاعدة البيانات
# ============================================================

def get_conn() -> sqlite3.Connection:
    """إنشاء اتصال جديد بقاعدة البيانات مع تفعيل المفاتيح الأجنبية.

    يُستخدَم وضع WAL (Write-Ahead Logging) بدل الوضع الافتراضي حتى تتمكن
    عمليات القراءة والكتابة من العمل في نفس الوقت دون أن تحظر بعضها،
    وهو ما يمنع أخطاء "database is locked" عند وجود عدد كبير من
    المستخدمين المتزامنين. busy_timeout يجعل أي اتصال ينتظر بدل أن
    يفشل فورًا إن كانت قاعدة البيانات مشغولة للحظة."""
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db() -> None:
    """إنشاء كل الجداول اللازمة إن لم تكن موجودة."""
    conn = get_conn()
    cur = conn.cursor()

    # تفعيل وضع WAL بشكل دائم على ملف قاعدة البيانات (يُخزَّن هذا الإعداد
    # داخل الملف نفسه ولا يحتاج إعادة ضبطه مستقبلًا)، مع synchronous=NORMAL
    # الذي يوفّر توازنًا ممتازًا بين الأمان والسرعة تحت ضغط عالٍ.
    try:
        cur.execute("PRAGMA journal_mode = WAL")
        cur.execute("PRAGMA synchronous = NORMAL")
    except sqlite3.OperationalError:
        logger.warning("⚠️ تعذّر تفعيل وضع WAL؛ سيعمل البوت بالوضع الافتراضي (قد يكون أبطأ تحت الضغط).")

    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            points INTEGER NOT NULL DEFAULT 0,
            total_points_earned INTEGER NOT NULL DEFAULT 0,
            purchases_count INTEGER NOT NULL DEFAULT 0,
            referred_by INTEGER,
            referrals_count INTEGER NOT NULL DEFAULT 0,
            referral_points_earned INTEGER NOT NULL DEFAULT 0,
            last_daily_claim TEXT,
            joined_at TEXT NOT NULL,
            last_active TEXT NOT NULL,
            is_banned INTEGER NOT NULL DEFAULT 0,
            language TEXT NOT NULL DEFAULT 'ar'
        );

        CREATE TABLE IF NOT EXISTS products (
            product_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            duration TEXT NOT NULL,
            price INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS keys_store (
            key_id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            key_value TEXT NOT NULL UNIQUE,
            is_sold INTEGER NOT NULL DEFAULT 0,
            sold_to INTEGER,
            sold_at TEXT,
            added_at TEXT NOT NULL,
            FOREIGN KEY (product_id) REFERENCES products(product_id)
        );

        CREATE TABLE IF NOT EXISTS purchases (
            purchase_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            key_id INTEGER NOT NULL,
            price_paid INTEGER NOT NULL,
            purchased_at TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'store'
        );

        CREATE TABLE IF NOT EXISTS referrals (
            referral_id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER NOT NULL,
            referred_id INTEGER NOT NULL UNIQUE,
            points_awarded INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS gift_links (
            gift_id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            points INTEGER NOT NULL,
            max_uses INTEGER NOT NULL,
            used_count INTEGER NOT NULL DEFAULT 0,
            expires_at TEXT,
            created_at TEXT NOT NULL,
            created_by INTEGER NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS gift_usage (
            usage_id INTEGER PRIMARY KEY AUTOINCREMENT,
            gift_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            used_at TEXT NOT NULL,
            UNIQUE(gift_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS daily_points_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            points INTEGER NOT NULL,
            claimed_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS video_requests (
            request_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            video_link TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            reject_reason TEXT,
            requested_at TEXT NOT NULL,
            reviewed_at TEXT,
            reward_note TEXT
        );

        CREATE TABLE IF NOT EXISTS banned_users (
            user_id INTEGER PRIMARY KEY,
            banned_at TEXT NOT NULL,
            reason TEXT,
            unbanned_at TEXT
        );

        CREATE TABLE IF NOT EXISTS transactions (
            transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            type TEXT NOT NULL,
            description TEXT,
            balance_after INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS button_colors (
            color_key TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            style TEXT NOT NULL DEFAULT 'none'
        );

        CREATE TABLE IF NOT EXISTS custom_emojis (
            color_key TEXT PRIMARY KEY,
            custom_emoji_id TEXT NOT NULL,
            default_emoji TEXT,
            set_at TEXT,
            emoji_position TEXT NOT NULL DEFAULT 'left'
        );

        CREATE TABLE IF NOT EXISTS force_sub_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL UNIQUE,
            title TEXT,
            username TEXT,
            invite_link TEXT,
            chat_type TEXT,
            added_at TEXT NOT NULL,
            added_by INTEGER
        );

        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            permissions TEXT NOT NULL DEFAULT '',
            added_at TEXT NOT NULL,
            added_by INTEGER
        );
        """
    )
    conn.commit()

    # إدراج الإعدادات الافتراضية إن لم تكن موجودة
    defaults = {
        "daily_points": str(DEFAULT_DAILY_POINTS),
        "referral_points": str(DEFAULT_REFERRAL_POINTS),
        "min_video_likes": str(DEFAULT_MIN_VIDEO_LIKES),
        "maintenance_mode": "0",
    }
    for k, v in defaults.items():
        cur.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v)
        )
    conn.commit()

    # ترحيل: إضافة عمود اللغة لقواعد بيانات قديمة لا تحتويه
    try:
        cols = [r["name"] for r in cur.execute("PRAGMA table_info(users)").fetchall()]
        if "language" not in cols:
            cur.execute("ALTER TABLE users ADD COLUMN language TEXT NOT NULL DEFAULT 'ar'")
            conn.commit()
    except sqlite3.OperationalError:
        pass

    # ترحيل آمن: موضع الإيموجي، دون حذف أو إعادة بناء أي جدول قديم.
    try:
        cols = [r["name"] for r in cur.execute("PRAGMA table_info(custom_emojis)").fetchall()]
        if "emoji_position" not in cols:
            cur.execute("ALTER TABLE custom_emojis ADD COLUMN emoji_position TEXT NOT NULL DEFAULT 'left'")
            conn.commit()
    except sqlite3.OperationalError as e:
        logger.warning("تعذّر ترحيل موضع الإيموجي: %s", e)

    # ترحيل: إضافة عمود لون زر المنتج لقواعد بيانات قديمة لا تحتويه
    try:
        cols = [r["name"] for r in cur.execute("PRAGMA table_info(products)").fetchall()]
        if "button_color" not in cols:
            cur.execute("ALTER TABLE products ADD COLUMN button_color TEXT NOT NULL DEFAULT 'danger'")
            conn.commit()
    except sqlite3.OperationalError:
        pass

    # ترحيل: التأكد من اكتمال أعمدة جدول admins (لو كان الجدول موجودًا
    # مسبقًا بأعمدة ناقصة لأي سبب، نُكمل الأعمدة الناقصة دون حذف أي بيانات)
    try:
        cols = [r["name"] for r in cur.execute("PRAGMA table_info(admins)").fetchall()]
        expected_cols = {"user_id", "permissions", "added_at", "added_by"}
        if not cols:
            # الجدول غير موجود إطلاقًا لسبب ما رغم CREATE TABLE أعلاه؛ ننشئه الآن
            cur.execute(
                """CREATE TABLE admins (
                    user_id INTEGER PRIMARY KEY,
                    permissions TEXT NOT NULL DEFAULT '',
                    added_at TEXT NOT NULL DEFAULT '',
                    added_by INTEGER
                )"""
            )
            conn.commit()
        elif set(cols) - expected_cols:
            # الجدول يحتوي على أعمدة قديمة زائدة (مثل created_at من إصدار سابق)
            # قد تكون NOT NULL بلا قيمة افتراضية، وهذا يتسبب بفشل عمليات
            # الإدراج الحالية (INSERT INTO admins ...) لأنها لا تُزوّد قيمة
            # لتلك الأعمدة. نعيد بناء الجدول بالمخطط الصحيح مع نقل البيانات
            # الحالية دون فقدان أي شيء.
            select_permissions = "permissions" if "permissions" in cols else "''"
            if "added_at" in cols:
                select_added_at = "added_at"
            elif "created_at" in cols:
                select_added_at = "created_at"
            else:
                select_added_at = "''"
            select_added_by = "added_by" if "added_by" in cols else "NULL"
            cur.execute(
                """CREATE TABLE admins_new (
                    user_id INTEGER PRIMARY KEY,
                    permissions TEXT NOT NULL DEFAULT '',
                    added_at TEXT NOT NULL DEFAULT '',
                    added_by INTEGER
                )"""
            )
            cur.execute(
                f"INSERT INTO admins_new (user_id, permissions, added_at, added_by) "
                f"SELECT user_id, COALESCE({select_permissions}, ''), "
                f"COALESCE({select_added_at}, ''), {select_added_by} FROM admins"
            )
            cur.execute("DROP TABLE admins")
            cur.execute("ALTER TABLE admins_new RENAME TO admins")
            conn.commit()
            logger.warning(
                "⚠️ تم ترحيل جدول admins: إزالة أعمدة قديمة زائدة (%s) كانت تسبب فشل رفع الأدمن.",
                ", ".join(sorted(set(cols) - expected_cols)),
            )
        else:
            if "permissions" not in cols:
                cur.execute("ALTER TABLE admins ADD COLUMN permissions TEXT NOT NULL DEFAULT ''")
                conn.commit()
            if "added_at" not in cols:
                cur.execute("ALTER TABLE admins ADD COLUMN added_at TEXT NOT NULL DEFAULT ''")
                conn.commit()
            if "added_by" not in cols:
                cur.execute("ALTER TABLE admins ADD COLUMN added_by INTEGER")
                conn.commit()
    except sqlite3.OperationalError as e:
        logger.warning("⚠️ تعذّر التحقق من/ترحيل جدول admins: %s", e)

    conn.close()
    load_button_color_registry()
    load_custom_emoji_registry()
    logger.info("تم تهيئة قاعدة البيانات بنجاح.")


def now_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------- إعدادات (Settings helpers) ----------------------

def get_setting(key: str, default: str = "") -> str:
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


# ============================================================
#     🎨 نظام ألوان الأزرار الكامل (قابل للتخصيص من لوحة المالك)
# ============================================================
# كل زر في البوت (الحالي والمستقبلي) يُعرَّف بمفتاح لون فريد (color_key)
# مع تسمية عربية واضحة (label) ولون افتراضي مبدئي (default). أول مرة
# يُعرَض فيها الزر يتم تسجيله تلقائيًا في جدول button_colors، فيظهر
# فورًا داخل قسم "🎨 ألوان الأزرار" بلوحة تحكم المالك ليتمكن من تغيير
# لونه لأي زر بشكل مستقل تمامًا عن بقية الأزرار — دون أي استثناء.
#
# الألوان المتاحة هي فقط الثلاثة التي تدعمها تيليجرام فعليًا عبر
# معامل style (Bot API 9.4): primary (أزرق) / success (أخضر) /
# danger (أحمر)، بالإضافة لخيار "بدون لون" (الشكل الافتراضي لتيليجرام).
# هذا سقف تيليجرام نفسها وليس قيدًا من الكود — لا توجد ألوان Hex حرة
# مدعومة رسميًا لأزرار Inline حتى تاريخ كتابة هذا الكود.

BUTTON_STYLE_CHOICES = {
    "primary": "🔵 أزرق",
    "success": "🟢 أخضر",
    "danger": "🔴 أحمر",
    "none": "⚪ بدون لون (افتراضي)",
}

_BUTTON_COLOR_REGISTRY: dict[str, dict] = {}
_STYLE_WARNED = {"done": False}


def load_button_color_registry() -> None:
    """تحميل كل ألوان الأزرار المسجَّلة من قاعدة البيانات إلى الذاكرة
    عند بدء تشغيل البوت، لتفادي أي استعلام لقاعدة البيانات عند كل
    عرض لقائمة أزرار (وهو ما يحدث مرات كثيرة جدًا لكل مستخدم)."""
    conn = get_conn()
    rows = conn.execute("SELECT color_key, label, style FROM button_colors").fetchall()
    conn.close()
    _BUTTON_COLOR_REGISTRY.clear()
    for r in rows:
        _BUTTON_COLOR_REGISTRY[r["color_key"]] = {"label": r["label"], "style": r["style"]}


def _register_color_key_if_new(color_key: str, label: str, default: str) -> None:
    if color_key in _BUTTON_COLOR_REGISTRY:
        return
    try:
        conn = get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO button_colors (color_key, label, style) VALUES (?, ?, ?)",
            (color_key, label, default),
        )
        conn.commit()
        row = conn.execute(
            "SELECT label, style FROM button_colors WHERE color_key=?", (color_key,)
        ).fetchone()
        conn.close()
        _BUTTON_COLOR_REGISTRY[color_key] = {"label": row["label"], "style": row["style"]}
    except sqlite3.OperationalError:
        # طبقة أمان إضافية: لو استُدعي هذا قبل init_db() لأي سبب (مثلًا
        # كود مستقبلي يبني لوحة أزرار عند استيراد الملف)، لا نُسقِط
        # البوت بالكامل، بل نستخدم اللون الافتراضي مؤقتًا في الذاكرة
        # فقط دون تسجيله في القائمة الدائمة، وسيتم تسجيله تلقائيًا وبأمان
        # عند أول استدعاء له بعد اكتمال init_db().
        logger.warning(
            "⚠️ تعذّر تسجيل لون الزر '%s' لأن قاعدة البيانات لم تُهيَّأ بعد؛ "
            "سيُستخدم اللون الافتراضي مؤقتًا.", color_key
        )
        _BUTTON_COLOR_REGISTRY[color_key] = {"label": label, "style": default}


def set_button_color(color_key: str, style: str) -> None:
    """يغيّر لون زر واحد بعينه بشكل مستقل عن كل الأزرار الأخرى."""
    conn = get_conn()
    conn.execute("UPDATE button_colors SET style=? WHERE color_key=?", (style, color_key))
    conn.commit()
    conn.close()
    if color_key in _BUTTON_COLOR_REGISTRY:
        _BUTTON_COLOR_REGISTRY[color_key]["style"] = style


def list_button_colors() -> list:
    """قائمة كل الأزرار المسجّلة (الحالية وأي زر ظهر مستقبلًا) مرتبة
    أبجديًا حسب التسمية العربية، لعرضها في لوحة تحكم المالك."""
    return sorted(_BUTTON_COLOR_REGISTRY.items(), key=lambda kv: kv[1]["label"])


def styled_button(
    text: str, callback_data: str, color_key: str, label: Optional[str] = None, default: str = "danger"
) -> InlineKeyboardButton:
    """ينشئ زر Inline بلون وإيموجي مميز (أيقونة) قابلَين للتخصيص الكامل
    من لوحة تحكم المالك، عبر معاملَي style و icon_custom_emoji_id
    الرسميَين من تيليجرام (كلاهما أُضيف في Bot API 9.4 / PTB 22.7). كل
    زر مسجَّل بمفتاح فريد (color_key) مع تسمية عربية (label) تُستخدم في
    لوحتَي الألوان والإيموجي معًا.

    - style: يأتي من جدول button_colors (قسم "🎨 ألوان الأزرار").
    - icon_custom_emoji_id: يأتي من نفس السجلّ الذي يملؤه قسم "🎨 تغيير
      الإيموجي" (_CUSTOM_EMOJI_REGISTRY) — أي إيموجي مميز يحفظه المالك
      لهذا الزر يظهر تلقائيًا كأيقونة على الزر نفسه، بالإضافة إلى ظهوره
      داخل نصوص/رسائل البوت عبر emojify() (لا نظامان منفصلان، بل نفس
      الـ ID المحفوظ يُستخدم في المكانين).

    لا نُخفي أي عطل بصمت: لو رُفعت مكتبة python-telegram-bot قديمة لا
    تدعم هذين المعاملين، تُسجَّل رسالة تحذير واضحة في السجلات مرة واحدة
    فقط ويُعرض الزر بشكله الافتراضي (نص + بيانات فقط) دون توقف البوت.
    ملاحظة: ظهور الأيقونة فعليًا على عميل المستخدم يتطلب أن يكون مالك
    البوت مشتركًا في Telegram Premium أو أن يكون البوت اشترى يوزرات
    إضافية عبر Fragment — وهو شرط من تيليجرام نفسها، وليس قيدًا في هذا
    الكود."""
    _register_color_key_if_new(color_key, label or color_key, default)
    style = _BUTTON_COLOR_REGISTRY[color_key]["style"]
    icon_id = _CUSTOM_EMOJI_REGISTRY.get(color_key)
    emoji_position = _CUSTOM_EMOJI_POSITION.get(color_key, "left")
    if icon_id:
        # Telegram renders icon_custom_emoji_id as a separate icon. Remove the
        # old ordinary icon from button text so it cannot appear twice.
        old_emoji = extract_leading_emoji(text)
        if old_emoji:
            text = text[len(old_emoji):].lstrip()
    elif emoji_position == "right":
        old_emoji = extract_leading_emoji(text)
        if old_emoji:
            text = text[len(old_emoji):].strip() + " " + old_emoji

    kwargs = {}
    if style and style != "none":
        kwargs["style"] = style
    if icon_id:
        kwargs["icon_custom_emoji_id"] = icon_id

    if kwargs:
        try:
            return InlineKeyboardButton(text, callback_data=callback_data, **kwargs)
        except TypeError:
            if not _STYLE_WARNED["done"]:
                _STYLE_WARNED["done"] = True
                logger.warning(
                    "⚠️ مكتبة python-telegram-bot المثبَّتة حاليًا لا تدعم معاملي "
                    "style و icon_custom_emoji_id (تلوين الأزرار وأيقونة الإيموجي "
                    "المميز عليها). يلزم تثبيت الإصدار 22.7 أو أحدث: "
                    'pip install "python-telegram-bot>=22.7" --upgrade'
                )
            return InlineKeyboardButton(text, callback_data=callback_data)
    return InlineKeyboardButton(text, callback_data=callback_data)


def styled_button_for_style(text: str, callback_data: str, style: str) -> InlineKeyboardButton:
    """زر بلون مباشر (غير مسجَّل في لوحة الألوان) — يُستخدم فقط لأزرار
    مؤقتة/داخلية مثل أزرار اختيار اللون نفسها."""
    if style and style != "none":
        try:
            return InlineKeyboardButton(text, callback_data=callback_data, style=style)
        except TypeError:
            return InlineKeyboardButton(text, callback_data=callback_data)
    return InlineKeyboardButton(text, callback_data=callback_data)


def delivery_open_bot_button() -> InlineKeyboardButton:
    """زر رابط أخضر لفتح البوت، يُرفق أسفل إشعارات التسليم داخل قناة
    تسليم الطلبات. يحاول استخدام معامل style الرسمي (Bot API 9.4) مع
    تراجع آمن (بدون توقف البوت) إن كانت المكتبة المثبَّتة أقدم."""
    text = "افتح البوت ↗️"
    url = f"https://t.me/{BOT_USERNAME}"
    try:
        return InlineKeyboardButton(text, url=url, style="success")
    except TypeError:
        return InlineKeyboardButton(text, url=url)


def back_btn(text: str, callback_data: str) -> InlineKeyboardButton:
    """زر الرجوع الموحّد، قابل لتخصيص لونه أيضًا من لوحة تحكم المالك
    ككل زر آخر في النظام (افتراضيًا أخضر success)."""
    return styled_button(text, callback_data, "btn_back", "🔙 زر الرجوع (عام)", default="success")


# ============================================================
#   🎨 نظام تغيير الإيموجيات (Custom / Premium Emoji) الكامل
# ============================================================
# يعتمد هذا النظام على نفس سجلّ الأزرار المستخدم أصلًا في نظام "ألوان
# الأزرار" أعلاه (_BUTTON_COLOR_REGISTRY): كل زر في البوت مسجَّل بمفتاح
# فريد (color_key) مع تسمية عربية تبدأ دائمًا بإيموجي (مثل "👥 قسم
# المستخدمون"). هذا يعطينا تلقائيًا "قائمة كل الأزرار التي تحتوي على
# إيموجيات" دون الحاجة لبناء سجلّ منفصل أو تعديل كل زر يدويًا.
#
# عند اختيار المالك زرًا معينًا وإرساله إيموجي مميز (Custom/Premium
# Emoji)، نستخرج custom_emoji_id تلقائيًا من خصائص الرسالة (message
# entities) ونحفظه في جدول custom_emojis مرتبطًا بمفتاح الزر. لاحقًا:
#
#   1) أي رسالة HTML يرسلها البوت (سواء نص جديد أو تعديل رسالة قائمة)
#      تمر تلقائيًا عبر دالة emojify() التي تستبدل الإيموجي الافتراضي
#      بالإيموجي المميز المحفوظ داخل نص الرسالة، بصيغة
#      <tg-emoji emoji-id="..."> الرسمية من تيليجرام.
#
#   2) الزر نفسه (InlineKeyboardButton) يُنشأ عبر styled_button()، والتي
#      تمرر نفس custom_emoji_id إلى معامل icon_custom_emoji_id الرسمي
#      (أُضيف في Bot API 9.4)، فيظهر الإيموجي المميز كأيقونة قبل نص
#      الزر مباشرةً — وليس فقط داخل نصوص/رسائل البوت.
#
# ملاحظة تقنية مهمة من توثيق تيليجرام: نص الزر (text) نفسه يبقى حقلًا
# نصيًا عاديًا لا يدعم التنسيق أو "الكيانات" (entities) بداخله — هذا
# قيد رسمي من تيليجرام. لكن حقل icon_custom_emoji_id منفصل تمامًا عن
# النص، وهو مخصص لعرض إيموجي مميز كأيقونة على الزر. Telegram يشترط أن
# يكون مالك البوت مشتركًا في Telegram Premium (أو أن يكون البوت قد
# اشترى يوزرات إضافية عبر Fragment) حتى تظهر هذه الأيقونة فعليًا على
# عميل تيليجرام؛ إن لم يتحقق هذا الشرط، سيظهر الزر بشكله العادي دون
# الأيقونة (يُتجاهل الحقل بصمت من طرف تيليجرام، وليس خطأً في الكود).

_CUSTOM_EMOJI_REGISTRY: dict[str, str] = {}
_CUSTOM_EMOJI_POSITION: dict[str, str] = {}


def load_custom_emoji_registry() -> None:
    """تحميل كل الإيموجيات المميزة المحفوظة من قاعدة البيانات إلى الذاكرة
    عند بدء تشغيل البوت، بحيث تبقى محفوظة بعد أي إعادة تشغيل."""
    conn = get_conn()
    try:
        rows = conn.execute("SELECT color_key, custom_emoji_id, emoji_position FROM custom_emojis").fetchall()
    except sqlite3.OperationalError:
        rows = conn.execute("SELECT color_key, custom_emoji_id FROM custom_emojis").fetchall()
    conn.close()
    _CUSTOM_EMOJI_REGISTRY.clear()
    _CUSTOM_EMOJI_POSITION.clear()
    for r in rows:
        _CUSTOM_EMOJI_REGISTRY[r["color_key"]] = r["custom_emoji_id"]
        _CUSTOM_EMOJI_POSITION[r["color_key"]] = (r["emoji_position"] if "emoji_position" in r.keys() else "left") or "left"


def set_custom_emoji_override(color_key: str, custom_emoji_id: str, default_emoji: str, position: str = "left") -> None:
    """يحفظ إيموجي مميز جديد لزر معيّن بشكل دائم في قاعدة البيانات."""
    conn = get_conn()
    conn.execute(
        "INSERT INTO custom_emojis (color_key, custom_emoji_id, default_emoji, set_at, emoji_position) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(color_key) DO UPDATE SET "
        "custom_emoji_id=excluded.custom_emoji_id, default_emoji=excluded.default_emoji, set_at=excluded.set_at, emoji_position=excluded.emoji_position",
        (color_key, custom_emoji_id, default_emoji, now_str(), position if position in ("left", "right") else "left"),
    )
    conn.commit()
    conn.close()
    _CUSTOM_EMOJI_REGISTRY[color_key] = custom_emoji_id
    _CUSTOM_EMOJI_POSITION[color_key] = position if position in ("left", "right") else "left"


def set_custom_emoji_position(color_key: str, position: str) -> None:
    position = position if position in ("left", "right") else "left"
    conn = get_conn()
    conn.execute("UPDATE custom_emojis SET emoji_position=? WHERE color_key=?", (position, color_key))
    conn.commit()
    conn.close()
    if color_key in _CUSTOM_EMOJI_REGISTRY:
        _CUSTOM_EMOJI_POSITION[color_key] = position


def clear_custom_emoji_override(color_key: str) -> None:
    """يعيد زرًا معيّنًا إلى إيموجيه الافتراضي (يحذف التخصيص)."""
    conn = get_conn()
    conn.execute("DELETE FROM custom_emojis WHERE color_key=?", (color_key,))
    conn.commit()
    conn.close()
    _CUSTOM_EMOJI_REGISTRY.pop(color_key, None)
    _CUSTOM_EMOJI_POSITION.pop(color_key, None)


def extract_leading_emoji(label: str) -> str:
    """يستخرج الإيموجي الأول من تسمية الزر (دائمًا أول كلمة في التسمية
    حسب اتفاقية التسميات المستخدمة في كل أزرار هذا البوت)."""
    if not label:
        return ""
    return label.split(" ", 1)[0].strip()


def list_custom_emoji_candidates() -> list:
    """كل الأزرار المسجَّلة في النظام (الحالية وأي زر يظهر مستقبلًا)
    والتي تحتوي تسميتها على إيموجي بادئ — هذه هي القائمة التي تُعرض
    للمالك عند الدخول لقسم «🎨 تغيير الإيموجي»."""
    out = []
    for color_key, info in _BUTTON_COLOR_REGISTRY.items():
        emoji = extract_leading_emoji(info["label"])
        if emoji:
            out.append((color_key, info["label"], emoji))
    return sorted(out, key=lambda x: x[1])


def emojify(text: Optional[str]) -> Optional[str]:
    """تُستدعى تلقائيًا (عبر تصحيح داخلي على مستوى مكتبة تيليجرام أدناه)
    على كل نص HTML صادر من البوت. تستبدل أي إيموجي افتراضي له بديل مميز
    محفوظ بصيغة <tg-emoji emoji-id="..."> الرسمية، فيظهر الإيموجي الجديد
    تلقائيًا في كل مكان يظهر فيه دون الحاجة لتعديل أي سطر كود يدويًا."""
    if not text or not _CUSTOM_EMOJI_REGISTRY:
        return text
    try:
        for color_key, custom_id in list(_CUSTOM_EMOJI_REGISTRY.items()):
            entry = _BUTTON_COLOR_REGISTRY.get(color_key)
            if not entry:
                continue
            default_emoji = extract_leading_emoji(entry["label"])
            if not default_emoji or default_emoji not in text:
                continue
            tag_open = f'<tg-emoji emoji-id="{custom_id}">'
            if tag_open in text:
                continue  # مُطبَّق بالفعل على هذا النص
            text = text.replace(default_emoji, f"{tag_open}{default_emoji}</tg-emoji>")
    except Exception:
        logger.exception(
            "⚠️ خطأ غير متوقع أثناء تطبيق الإيموجي المخصص على نص صادر؛ "
            "سيُرسل النص الأصلي دون تغيير حتى لا يتوقف إرسال الرسالة."
        )
    return text


def _install_custom_emoji_patches() -> None:
    """تُفعّل تطبيق emojify() تلقائيًا على كل رسالة HTML يرسلها البوت،
    بغض النظر عن مكان بنائها في الكود (send_message / edit_message_text
    / reply_text وما يعتمد عليها مثل reply_html) — هذا ما يجعل النظام
    ديناميكيًا بالكامل: لا حاجة لتعديل عشرات الأماكن التي تبني نصوص
    الرسائل، ولا لوضع أي معرف إيموجي بشكل ثابت داخل الكود. أي خطأ داخل
    هذا التصحيح يُعزل بأمان بحيث لا يوقف إرسال أي رسالة على الإطلاق."""
    if getattr(Application, "_custom_emoji_patch_installed", False):
        return

    _BotCls, _CQCls, _MsgCls = Bot, CallbackQuery, Message

    def _apply_emojify(args: tuple, kwargs: dict, text_kw: str, text_pos: int) -> tuple:
        try:
            if kwargs.get("parse_mode") == ParseMode.HTML:
                if kwargs.get(text_kw):
                    kwargs[text_kw] = emojify(kwargs[text_kw])
                elif len(args) > text_pos and args[text_pos]:
                    args = list(args)
                    args[text_pos] = emojify(args[text_pos])
        except Exception:
            logger.exception("⚠️ خطأ أثناء محاولة تطبيق الإيموجي المخصص قبل الإرسال.")
        return args, kwargs

    _orig_send_message = _BotCls.send_message

    async def _patched_send_message(self, *args, **kwargs):
        args, kwargs = _apply_emojify(args, kwargs, "text", 1)
        return await _orig_send_message(self, *args, **kwargs)

    _orig_edit_text = _CQCls.edit_message_text

    async def _patched_edit_message_text(self, *args, **kwargs):
        args, kwargs = _apply_emojify(args, kwargs, "text", 0)
        return await _orig_edit_text(self, *args, **kwargs)

    _orig_reply_text = _MsgCls.reply_text

    async def _patched_reply_text(self, *args, **kwargs):
        args, kwargs = _apply_emojify(args, kwargs, "text", 0)
        return await _orig_reply_text(self, *args, **kwargs)

    _BotCls.send_message = _patched_send_message
    _CQCls.edit_message_text = _patched_edit_message_text
    _MsgCls.reply_text = _patched_reply_text
    Application._custom_emoji_patch_installed = True


_install_custom_emoji_patches()


def get_daily_points() -> int:
    return int(get_setting("daily_points", str(DEFAULT_DAILY_POINTS)))


def get_referral_points() -> int:
    return int(get_setting("referral_points", str(DEFAULT_REFERRAL_POINTS)))


def get_min_video_likes() -> int:
    return int(get_setting("min_video_likes", str(DEFAULT_MIN_VIDEO_LIKES)))


# ---------------------- 🔧 وضع الصيانة ----------------------

def is_maintenance_mode() -> bool:
    """محفوظة في جدول settings، لذا تبقى الحالة كما هي حتى بعد إعادة
    تشغيل البوت."""
    return get_setting("maintenance_mode", "0") == "1"


def set_maintenance_mode(enabled: bool) -> None:
    set_setting("maintenance_mode", "1" if enabled else "0")


# ---------------------- 📣 قناة تسليم الطلبات ----------------------

def get_delivery_channel_id() -> Optional[str]:
    """يعيد آيدي قناة تسليم الطلبات المحفوظة، أو None إن لم تُحدَّد بعد."""
    val = get_setting("delivery_channel_id", "")
    return val or None


def get_delivery_channel_title() -> str:
    return get_setting("delivery_channel_title", "")


def set_delivery_channel(chat_id, title: str) -> None:
    set_setting("delivery_channel_id", str(chat_id))
    set_setting("delivery_channel_title", title or "")


def clear_delivery_channel() -> None:
    set_setting("delivery_channel_id", "")
    set_setting("delivery_channel_title", "")


# ---------------------- 📱 نسخة التطبيق (APK) ----------------------

def get_app_apk_file_id() -> Optional[str]:
    """يعيد file_id لملف الـ APK الحالي الذي حدّده المالك، أو None إن لم
    يُرفع أي ملف بعد."""
    val = get_setting("app_apk_file_id", "")
    return val or None


def get_app_apk_file_name() -> str:
    return get_setting("app_apk_file_name", "")


def get_app_apk_caption() -> str:
    """الوصف الاختياري الذي يضيفه المالك للنسخة، ويظهر للمستخدمين أسفل
    ملف الـ APK عند استلامهم له."""
    return get_setting("app_apk_caption", "")


def set_app_apk(file_id: str, file_name: str) -> None:
    set_setting("app_apk_file_id", file_id)
    set_setting("app_apk_file_name", file_name or "")


def set_app_apk_caption(text: str) -> None:
    set_setting("app_apk_caption", text or "")


def clear_app_apk() -> None:
    set_setting("app_apk_file_id", "")
    set_setting("app_apk_file_name", "")
    set_setting("app_apk_caption", "")


def build_app_apk_caption() -> str:
    """يبني نص التسمية التوضيحية (Caption) الذي يُرفق مع ملف الـ APK عند
    إرساله للمستخدم: عنوان أنيق + وصف النسخة (إن أضافه المالك) + اسم الملف،
    مع حماية من تجاوز الحد الأقصى المسموح به لتيليجرام (1024 حرفًا)."""
    file_name = get_app_apk_file_name()
    description = get_app_apk_caption()

    caption = "📱 <b>نسخة التطبيق</b>"
    if description:
        caption += f"\n\n{esc(description)}"
    if file_name:
        caption += f"\n\n📦 <b>الملف:</b> {esc(file_name)}"

    if len(caption) > 1024:
        caption = caption[:1021] + "..."
    return caption


def _delivery_chat_ref(channel_id: str):
    """يحوّل آيدي القناة المحفوظ (نص) لصيغة مناسبة لإرسال الرسائل به:
    رقم صحيح إن كان آيدي رقمي (مثل -1001234567890)، أو نص كما هو (يوزر)."""
    if channel_id and channel_id.lstrip("-").isdigit():
        return int(channel_id)
    return channel_id


# ---------------------- المستخدمون (Users helpers) ----------------------

def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


# ============================================================
#          👮 نظام الأدمن (صلاحيات منفصلة لكل أدمن)
# ============================================================
# المالك يستطيع رفع أي مستخدم كأدمن ومنحه صلاحيات محددة بشكل مستقل عن
# بقية الأدمنية. الصلاحيات تُخزَّن كسلسلة نصية مفصولة بفواصل داخل عمود
# permissions بجدول admins (مثال: "points,broadcast,ban").

ADMIN_PERM_BUY_FREE = "buy_free"
ADMIN_PERM_POINTS = "points"
ADMIN_PERM_BROADCAST = "broadcast"
ADMIN_PERM_APPROVE_FREE = "approve_free"
ADMIN_PERM_BAN = "ban"
ADMIN_PERM_GIFT = "gift"

ADMIN_PERMISSIONS_LABELS = {
    ADMIN_PERM_BUY_FREE: "1️⃣ شراء أكواد بدون خصم نقاط",
    ADMIN_PERM_POINTS: "2️⃣ شحن أو خصم نقاط المستخدمين",
    ADMIN_PERM_BROADCAST: "3️⃣ إرسال إذاعة",
    ADMIN_PERM_APPROVE_FREE: "4️⃣ الموافقة على طلبات الأكواد المجانية",
    ADMIN_PERM_BAN: "5️⃣ حظر المستخدمين",
    ADMIN_PERM_GIFT: "6️⃣ إنشاء روابط هدايا",
}
ADMIN_PERMISSIONS_ORDER = list(ADMIN_PERMISSIONS_LABELS.keys())


def get_admin_row(user_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM admins WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row


def is_admin(user_id: int) -> bool:
    """يعيد True إن كان المستخدم أدمن مرفوعًا من المالك (وليس المالك نفسه)."""
    return get_admin_row(user_id) is not None


def is_owner_or_admin(user_id: int) -> bool:
    return is_owner(user_id) or is_admin(user_id)


def get_admin_permissions(user_id: int) -> set:
    row = get_admin_row(user_id)
    if not row or not row["permissions"]:
        return set()
    return set(p for p in row["permissions"].split(",") if p)


def has_perm(user_id: int, perm: str) -> bool:
    """المالك يملك كل الصلاحيات دومًا؛ الأدمن فقط الصلاحيات الممنوحة له."""
    if is_owner(user_id):
        return True
    return perm in get_admin_permissions(user_id)


def set_admin_permissions(user_id: int, perms: set, added_by: int) -> None:
    perms_str = ",".join(p for p in ADMIN_PERMISSIONS_ORDER if p in perms)
    conn = get_conn()
    conn.execute(
        "INSERT INTO admins (user_id, permissions, added_at, added_by) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET permissions=excluded.permissions",
        (user_id, perms_str, now_str(), added_by),
    )
    conn.commit()
    conn.close()


def remove_admin(user_id: int) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def list_admins() -> list:
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM admins ORDER BY added_at DESC").fetchall()
    except sqlite3.OperationalError:
        # طبقة أمان إضافية في حال كان عمود added_at غير موجود لأي سبب غير
        # متوقع (مثلًا قاعدة بيانات قديمة لم تُرحَّل بعد)؛ لا نُسقط لوحة
        # التحكم بالكامل، بل نعرض القائمة بترتيب افتراضي بدل ذلك.
        rows = conn.execute("SELECT * FROM admins").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def admin_permissions_display(perms: set) -> str:
    if not perms:
        return "لا توجد صلاحيات محددة"
    return "\n".join(
        f"• {ADMIN_PERMISSIONS_LABELS[p]}" for p in ADMIN_PERMISSIONS_ORDER if p in perms
    )


def admin_perm_toggle_keyboard(target_id: int, perms: set) -> InlineKeyboardMarkup:
    """لوحة اختيار الصلاحيات: كل صلاحية زر مستقل يتبدّل ✅/⬜ عند الضغط.
    تُستخدم لكل من رفع أدمن جديد وتعديل صلاحيات أدمن موجود (الذي يظهر له
    أيضًا زر إزالة الأدمن نهائيًا)."""
    rows = []
    for perm in ADMIN_PERMISSIONS_ORDER:
        mark = "✅" if perm in perms else "⬜"
        rows.append(
            [
                styled_button(
                    f"{mark} {ADMIN_PERMISSIONS_LABELS[perm]}",
                    f"adm:ad:toggle:{perm}",
                    f"adm_ad_perm_{perm}",
                    f"👮 زر صلاحية أدمن: {ADMIN_PERMISSIONS_LABELS[perm]}",
                    "primary",
                )
            ]
        )
    if is_admin(target_id):
        rows.append(
            [styled_button("🗑️ إزالة الأدمن نهائيًا", f"adm:ad:remove:{target_id}", "adm_ad_remove", "🗑️ زر إزالة أدمن", "danger")]
        )
    rows.append(
        [styled_button("💾 رفع أدمن / حفظ الصلاحيات", "adm:ad:save", "adm_ad_save", "💾 زر حفظ صلاحيات الأدمن", "success")]
    )
    rows.append([styled_button("❌ إلغاء", "adm:ad:cancel", "adm_ad_cancel", "❌ زر إلغاء إدارة الأدمن", "danger")])
    return InlineKeyboardMarkup(rows)


def get_user(user_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row


def get_user_lang(user_id: int) -> str:
    """يعيد كود لغة المستخدم المحفوظة، أو 'ar' كافتراضي."""
    conn = get_conn()
    row = conn.execute("SELECT language FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    lang = (row["language"] if row and row["language"] else "ar")
    return lang if lang in LANGS else "ar"


def set_user_lang(user_id: int, lang: str) -> None:
    if lang not in LANGS:
        return
    conn = get_conn()
    conn.execute("UPDATE users SET language=? WHERE user_id=?", (lang, user_id))
    conn.commit()
    conn.close()


def get_user_by_username(username: str) -> Optional[sqlite3.Row]:
    username = username.lstrip("@")
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,)
    ).fetchone()
    conn.close()
    return row


def ensure_user(tg_user, referred_by: Optional[int] = None) -> sqlite3.Row:
    """التأكد من وجود المستخدم في قاعدة البيانات، وإنشاؤه إن لم يكن موجودًا."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM users WHERE user_id=?", (tg_user.id,)
    ).fetchone()

    if row is None:
        valid_referrer = None
        if referred_by and referred_by != tg_user.id:
            ref_row = conn.execute(
                "SELECT user_id FROM users WHERE user_id=?", (referred_by,)
            ).fetchone()
            if ref_row:
                valid_referrer = referred_by

        conn.execute(
            """INSERT INTO users
               (user_id, username, first_name, points, total_points_earned,
                purchases_count, referred_by, referrals_count,
                referral_points_earned, last_daily_claim, joined_at, last_active,
                is_banned)
               VALUES (?, ?, ?, 0, 0, 0, ?, 0, 0, NULL, ?, ?, 0)""",
            (
                tg_user.id,
                tg_user.username or "",
                tg_user.first_name or "",
                valid_referrer,
                now_str(),
                now_str(),
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM users WHERE user_id=?", (tg_user.id,)
        ).fetchone()
        conn.close()
        # ملاحظة: منح نقاط الإحالة الفعلي يتم داخل start_command عبر _apply_referral
        # (يُستدعى هناك تحت قفل referral_lock لمنع أي تسابق أو تكرار).
        return row
    else:
        # تحديث بيانات أساسية + آخر نشاط
        conn.execute(
            "UPDATE users SET username=?, first_name=?, last_active=? WHERE user_id=?",
            (tg_user.username or "", tg_user.first_name or "", now_str(), tg_user.id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM users WHERE user_id=?", (tg_user.id,)
        ).fetchone()
        conn.close()
        return row


def touch_last_active(user_id: int) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE users SET last_active=? WHERE user_id=?", (now_str(), user_id)
    )
    conn.commit()
    conn.close()


def is_banned(user_id: int) -> bool:
    conn = get_conn()
    row = conn.execute(
        "SELECT is_banned FROM users WHERE user_id=?", (user_id,)
    ).fetchone()
    conn.close()
    return bool(row and row["is_banned"])


def record_transaction(
    conn: sqlite3.Connection, user_id: int, amount: int, ttype: str, description: str
) -> int:
    """يسجل عملية نقاط ويعيد الرصيد الحالي للمستخدم.

    ⚠️ مهم جدًا: يجب استدعاء هذه الدالة دائمًا بعد تنفيذ UPDATE الذي يغيّر
    رصيد المستخدم (وليس قبله) وضمن نفس الاتصال المفتوح. هذه الدالة لا
    تضيف/تخصم amount من الرصيد بنفسها؛ فهي فقط تقرأ الرصيد الحالي (بعد
    التحديث الذي نفّذه المستدعي) وتسجّله كما هو في سجل العمليات.

    (سابقًا كانت هذه الدالة تُضيف amount مرة أخرى فوق الرصيد المحدَّث
    مسبقًا، مما كان يُسبب مضاعفة الرصيد المعروض/المسجَّل بشكل خاطئ —
    مثال: شحن 100 نقطة لمستخدم رصيده 0 كان يُظهر 200 بدل 100. تم إصلاح
    هذا نهائيًا هنا في نقطة واحدة تُستخدم من كل عمليات الشحن/الخصم/التحويل/
    الهدايا/الإحالة/النقاط اليومية.)
    """
    cur = conn.execute("SELECT points FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    balance_after = row["points"] if row else 0
    conn.execute(
        """INSERT INTO transactions (user_id, amount, type, description, balance_after, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (user_id, amount, ttype, description, balance_after, now_str()),
    )
    return balance_after


# ============================================================
#                    أقفال التزامن (Concurrency Locks)
# ============================================================
# نستخدم أقفال asyncio لمنع أي تسابق (race condition) عند شراء
# مفتاح، أو أخذ نقاط يومية، أو استخدام رابط هدية، بما أن كل
# التعامل يتم داخل نفس الحلقة (event loop).

purchase_lock = asyncio.Lock()
daily_lock = asyncio.Lock()
gift_lock = asyncio.Lock()
referral_lock = asyncio.Lock()
transfer_lock = asyncio.Lock()


# ============================================================
#        🛡️ الحماية من السبام والفيضان (Anti-Flood / Anti-Spam)
# ============================================================
# طبقة وسطى عامة (Middleware) تعمل قبل أي معالج آخر (group=-1) لكل
# التحديثات الواردة. تمنع نفس المستخدم من إرسال طلبات أسرع من الحد
# المسموح، وتوقف مؤقتًا أي مستخدم يكرر المحاولة بشكل عدواني (تشبه
# هجمات Flood)، دون التأثير إطلاقًا على بقية المستخدمين (لكل مستخدم
# عدّاده الخاص، فلا يوجد قفل عام يُبطئ البوت بالكامل).

FLOOD_MIN_INTERVAL = 0.5          # أقل فاصل زمني مسموح بين طلبين لنفس المستخدم (ثانية)
FLOOD_STRIKES_LIMIT = 6           # عدد المخالفات المتتالية قبل الحظر المؤقت
FLOOD_TEMP_BLOCK_SECONDS = 30     # مدة الحظر المؤقت بعد تجاوز الحد
FLOOD_STATE_MAX_AGE = 6 * 3600    # عمر أقصى لأي بيانات مخزّنة قبل تنظيفها تلقائيًا (منع تسرب الذاكرة)

_flood_last_ts: dict[int, float] = {}
_flood_strikes: dict[int, int] = {}
_flood_blocked_until: dict[int, float] = {}


async def flood_guard_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None:
        return
    uid = user.id
    now = time.monotonic()

    blocked_until = _flood_blocked_until.get(uid)
    if blocked_until and now < blocked_until:
        if update.callback_query:
            try:
                await update.callback_query.answer(
                    "⏳ الرجاء الانتظار قليلًا قبل المحاولة مجددًا.", show_alert=False
                )
            except TelegramError:
                pass
        raise ApplicationHandlerStop

    last = _flood_last_ts.get(uid, 0.0)
    _flood_last_ts[uid] = now

    if now - last < FLOOD_MIN_INTERVAL:
        strikes = _flood_strikes.get(uid, 0) + 1
        _flood_strikes[uid] = strikes
        if strikes >= FLOOD_STRIKES_LIMIT:
            _flood_blocked_until[uid] = now + FLOOD_TEMP_BLOCK_SECONDS
            _flood_strikes[uid] = 0
            logger.warning("🚫 تم تقييد المستخدم %s مؤقتًا بسبب نشاط يشبه السبام/الفيضان.", uid)
        if update.callback_query:
            try:
                await update.callback_query.answer()
            except TelegramError:
                pass
        raise ApplicationHandlerStop

    # طلب طبيعي: إعادة ضبط عدّاد المخالفات تدريجيًا
    if _flood_strikes.get(uid):
        _flood_strikes[uid] = max(0, _flood_strikes[uid] - 1)

    # وضع الصيانة: يُحجب المستخدم العادي فورًا (المالك والأدمن يستمران
    # بالوصول الكامل للوحات التحكم أثناء الصيانة).
    await maintenance_gate(update, context)

    # آخر خطوة: التحقق من الاشتراك الإجباري (إن كان مفعّلًا) قبل السماح
    # بمرور التحديث لبقية المعالجات.
    await force_sub_gate(update, context)


# ============================================================
#              🔧 نظام وضع الصيانة (Maintenance Mode)
# ============================================================
# عند تفعيله من لوحة المالك ← الإعدادات، يُمنع أي مستخدم عادي من استخدام
# البوت (بما في ذلك /start) وتُعرض له رسالة توضيحية، بينما يستمر المالك
# وكل أدمن مرفوع بالوصول الكامل للوحات التحكم دون أي قيد. حالة التفعيل
# محفوظة في جدول settings وتبقى كما هي حتى بعد إعادة تشغيل البوت.

async def maintenance_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None:
        return
    if is_owner_or_admin(user.id):
        return
    if not is_maintenance_mode():
        return

    text = "🔧 حاليًا البوت في وضع الصيانة، يرجى المحاولة لاحقًا."
    try:
        if update.callback_query:
            await update.callback_query.answer(text, show_alert=True)
        elif update.message:
            await update.message.reply_text(text)
    except TelegramError:
        pass
    raise ApplicationHandlerStop


# ============================================================
#          🔒 نظام الاشتراك الإجباري (Force Subscription)
# ============================================================
# يسمح للمالك بإضافة قنوات/مجموعات يجب على كل عضو الاشتراك بها قبل
# استخدام أي وظيفة في البوت. يعمل كطبقة تحقق عامة قبل كل التحديثات
# (باستثناء المالك نفسه، وأمر /start و/cancel، وزر التحقق من الاشتراك
# نفسه)، ويتعامل بأمان مع حالات تعذر التحقق (مثل عدم كون البوت مشرفًا
# في القناة) دون حظر المستخدم ظلمًا بسبب خطأ في الإعداد.

def get_force_sub_channels() -> list:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM force_sub_channels ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


async def check_force_subscription(bot, user_id: int) -> tuple:
    """يتحقق من اشتراك المستخدم في كل القنوات/المجموعات المطلوبة.
    يعيد (True, []) إن كان مشتركًا في الجميع (أو لا توجد قنوات أصلًا)،
    وإلا (False, [قنوات غير مشترك بها])."""
    channels = get_force_sub_channels()
    if not channels:
        return True, []
    missing = []
    for ch in channels:
        try:
            chat_ref = ch["chat_id"]
            try:
                chat_ref = int(chat_ref)
            except (TypeError, ValueError):
                pass
            member = await bot.get_chat_member(chat_id=chat_ref, user_id=user_id)
            if member.status in ("left", "kicked"):
                missing.append(ch)
        except Forbidden:
            logger.warning(
                "⚠️ تعذر التحقق من الاشتراك الإجباري في %s (البوت غير مشرف/غير عضو هناك).",
                ch["chat_id"],
            )
            continue
        except BadRequest as e:
            logger.warning("⚠️ تعذر التحقق من اشتراك %s في %s: %s", user_id, ch["chat_id"], e)
            continue
        except TelegramError as e:
            logger.warning("⚠️ خطأ غير متوقع أثناء التحقق من الاشتراك الإجباري: %s", e)
            continue
    return (len(missing) == 0), missing


def build_force_sub_keyboard(channels: list, lang: str) -> InlineKeyboardMarkup:
    rows = []
    for ch in channels:
        url = ch.get("invite_link") or (
            f"https://t.me/{ch['username']}" if ch.get("username") else None
        )
        label = f"📢 {ch.get('title') or ch.get('username') or ch['chat_id']}"
        if url:
            rows.append([InlineKeyboardButton(label, url=url)])
        else:
            # تعذر بناء رابط لهذه القناة (لا يوزر ولا رابط دعوة) — تُعرض كتنبيه غير قابل للنقر
            rows.append([InlineKeyboardButton(label, callback_data="noop")])
    rows.append(
        [styled_button(t(lang, "fsub_btn_check"), "fsub:check", "fsub_check_btn", "✅ زر تحقق من الاشتراك", "success")]
    )
    return InlineKeyboardMarkup(rows)


async def force_sub_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None:
        return
    if is_owner(user.id):
        return

    # السماح دائمًا بمرور /start و/cancel حتى تعمل بشكل طبيعي (start_command
    # يتولى بنفسه عرض شاشة الاشتراك الإجباري إن لزم بعد تسجيل المستخدم).
    if update.message and update.message.text:
        txt = update.message.text.strip()
        if txt.startswith("/start") or txt.startswith("/cancel"):
            return

    # السماح بمرور زر "تحقق من الاشتراك" ليعالجه معالجه المخصص
    if update.callback_query and update.callback_query.data == "fsub:check":
        return

    # المستخدم المحظور: تُعرض له رسالة الحظر من المعالج المخصص بدل شاشة الاشتراك
    if is_banned(user.id):
        return

    channels = get_force_sub_channels()
    if not channels:
        return

    ok, missing = await check_force_subscription(context.bot, user.id)
    if ok:
        return

    lang = get_user_lang(user.id)
    kb = build_force_sub_keyboard(missing, lang)
    text = f"{t(lang, 'fsub_prompt_title')}\n\n{t(lang, 'fsub_prompt_note')}"
    try:
        if update.callback_query:
            try:
                await update.callback_query.answer()
            except TelegramError:
                pass
            await context.bot.send_message(
                chat_id=user.id, text=text, parse_mode=ParseMode.HTML, reply_markup=kb
            )
        elif update.message:
            await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except TelegramError:
        pass
    raise ApplicationHandlerStop


async def cb_forcesub_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """زر «تحقق من الاشتراك»: يعيد التحقق من اشتراك العضو مباشرة."""
    query = update.callback_query
    tg_user = update.effective_user
    lang = get_user_lang(tg_user.id)
    ensure_user(tg_user)

    ok, _missing = await check_force_subscription(context.bot, tg_user.id)
    if not ok:
        await query.answer(t(lang, "fsub_still_missing"), show_alert=True)
        return

    await query.answer(t(lang, "fsub_all_done"), show_alert=True)
    try:
        await query.edit_message_text(
            t(lang, "welcome", name=esc(tg_user.first_name)),
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_keyboard(tg_user.id, lang),
        )
        context.user_data["menu_msg_id"] = query.message.message_id
    except (BadRequest, TelegramError):
        await send_main_menu(context, context.bot, tg_user.id, lang, tg_user.first_name)


async def cleanup_flood_state_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """تنظيف دوري لبيانات الحماية من الفيضان حتى لا تتراكم في الذاكرة
    (Memory Leak) مع مرور الوقت وتزايد عدد المستخدمين."""
    cutoff = time.monotonic() - FLOOD_STATE_MAX_AGE
    stale_users = [uid for uid, ts in _flood_last_ts.items() if ts < cutoff]
    for uid in stale_users:
        _flood_last_ts.pop(uid, None)
        _flood_strikes.pop(uid, None)
        _flood_blocked_until.pop(uid, None)
    if stale_users:
        logger.info("🧹 تم تنظيف بيانات %d مستخدم غير نشط من ذاكرة الحماية من الفيضان.", len(stale_users))


# ============================================================
#                      لوحات المفاتيح (Keyboards)
# ============================================================

def video_request_kb(request_id: int) -> InlineKeyboardMarkup:
    """لوحة أزرار قبول/رفض طلب فيديو (موحّدة في كل الأماكن التي تستخدمها،
    ولون كل زر منها قابل للتخصيص من لوحة تحكم المالك)."""
    return InlineKeyboardMarkup(
        [
            [
                styled_button("✅ قبول", f"vr:accept:{request_id}", "vr_accept", "✅ زر قبول طلب الفيديو", "success"),
                styled_button("❌ رفض", f"vr:reject:{request_id}", "vr_reject", "❌ زر رفض طلب الفيديو", "danger"),
            ]
        ]
    )


def main_menu_keyboard(user_id: int, lang: Optional[str] = None) -> InlineKeyboardMarkup:
    """قائمة رئيسية كأزرار Inline مرفقة أسفل نص الرسالة مباشرة (وليس شريطًا
    ثابتًا أسفل الشاشة)، بنفس أسلوب أزرار المتجر. أهم زر (المتجر) بمفرده
    بالأعلى، ثم بقية الأزرار في صفوف من اثنين لترتيب احترافي."""
    lang = lang or get_user_lang(user_id)
    rows = [
        [styled_button(t(lang, "btn_store"), "menu:store", "btn_store", "🛒 زر المتجر (القائمة الرئيسية)", "danger")],
        [styled_button(t(lang, "btn_version"), "menu:version", "btn_version", "📱 زر النسخة (تحميل ملف APK)", "primary")],
        [
            styled_button(t(lang, "btn_daily"), "menu:daily", "btn_daily", "🎁 زر النقاط اليومية", "primary"),
            styled_button(t(lang, "btn_account"), "menu:account", "btn_account", "👤 زر حسابي", "primary"),
        ],
        [
            styled_button(t(lang, "btn_referral"), "menu:referral", "btn_referral", "🔗 زر دعوة الأصدقاء", "primary"),
            styled_button(t(lang, "btn_video"), "menu:video", "btn_video", "🎥 زر كود بدون تجميع نقاط", "primary"),
        ],
        [styled_button(t(lang, "btn_language"), "menu:lang", "btn_language", "🌐 زر اللغة", "danger")],
        [styled_button(t(lang, "btn_support"), "menu:support", "btn_support", "📩 زر الدعم", "danger")],
    ]
    if is_owner_or_admin(user_id):
        rows.append([styled_button(t(lang, "btn_admin"), "menu:admin", "btn_admin", "👑 زر لوحة التحكم", "primary")])
    return InlineKeyboardMarkup(rows)


def admin_menu_keyboard(user_id: Optional[int] = None) -> InlineKeyboardMarkup:
    """تُبنى لوحة تحكم المالك بالكامل عندما user_id هو المالك أو غير محدد
    (للاستخدام داخل مسارات مخصصة للمالك فقط أصلًا)، بينما يرى الأدمن فقط
    الأقسام التي منحه المالك صلاحية الوصول إليها."""
    owner_view = user_id is None or is_owner(user_id)
    perms = set() if owner_view else get_admin_permissions(user_id)

    rows = []

    row = []
    if owner_view:
        row.append(styled_button("👥 المستخدمون", "adm:users", "adm_users", "👥 قسم المستخدمون", "primary"))
    if owner_view or ADMIN_PERM_POINTS in perms:
        row.append(styled_button("💰 إدارة النقاط", "adm:points", "adm_points", "💰 قسم إدارة النقاط", "primary"))
    if row:
        rows.append(row)

    row = []
    if owner_view:
        row.append(styled_button("🔑 إدارة المنتجات", "adm:products", "adm_products", "🔑 قسم إدارة المنتجات", "primary"))
        row.append(styled_button("🎫 إدارة المفاتيح", "adm:keys", "adm_keys", "🎫 قسم إدارة المفاتيح", "primary"))
    if row:
        rows.append(row)

    row = []
    if owner_view or ADMIN_PERM_GIFT in perms:
        row.append(styled_button("🎁 روابط الهدايا", "adm:gift", "adm_gift", "🎁 قسم روابط الهدايا", "primary"))
    if owner_view or ADMIN_PERM_BROADCAST in perms:
        row.append(styled_button("📢 إذاعة", "adm:broadcast", "adm_broadcast", "📢 قسم الإذاعة", "primary"))
    if row:
        rows.append(row)

    row = []
    if owner_view or ADMIN_PERM_BAN in perms:
        row.append(styled_button("🚫 الحظر", "adm:ban", "adm_ban", "🚫 قسم الحظر", "primary"))
    if owner_view:
        row.append(styled_button("📊 الإحصائيات", "adm:stats", "adm_stats", "📊 قسم الإحصائيات", "primary"))
    if row:
        rows.append(row)

    row = []
    if owner_view or ADMIN_PERM_APPROVE_FREE in perms:
        row.append(styled_button("🎥 طلبات الفيديو", "adm:vr", "adm_vr", "🎥 قسم طلبات الفيديو", "primary"))
    if owner_view:
        row.append(styled_button("⚙️ الإعدادات", "adm:settings", "adm_settings", "⚙️ قسم الإعدادات", "primary"))
    if row:
        rows.append(row)

    if owner_view:
        rows.append([styled_button("🎨 ألوان الأزرار", "adm:colors", "adm_colors", "🎨 قسم ألوان الأزرار", "primary")])
        rows.append([styled_button("🎨 تغيير الإيموجي", "adm:emoji", "adm_emoji", "🎨 قسم تغيير الإيموجي", "primary")])
        rows.append([styled_button("🔒 الاشتراك الإجباري", "adm:fs", "adm_fs", "🔒 قسم الاشتراك الإجباري", "primary")])
        rows.append([styled_button("👮 إدارة الأدمن", "adm:admins", "adm_admins", "👮 قسم إدارة الأدمن", "primary")])

    rows.append([back_btn("🔙 رجوع", "menu:home")])
    return InlineKeyboardMarkup(rows)


def back_button(cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[back_btn("🔙 رجوع", cb)]])


def back_home_kb(lang: str) -> InlineKeyboardMarkup:
    """زر رجوع يعيد المستخدم للقائمة الرئيسية."""
    return InlineKeyboardMarkup(
        [[back_btn(t(lang, "btn_back"), "menu:home")]]
    )


def lang_back_kb(lang: str) -> InlineKeyboardMarkup:
    """زر رجوع داخل التدفقات: يُلغي التدفق ويعود للقائمة الرئيسية."""
    return InlineKeyboardMarkup(
        [[back_btn(t(lang, "btn_back"), "flow:back")]]
    )


async def _safe_delete(bot, chat_id: int, message_id: Optional[int]) -> None:
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except (BadRequest, TelegramError):
        pass


async def _clear_tracked_messages(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """يحذف آخر رسالة قائمة وآخر رسالة نتيجة متعقّبتين لمنع تراكم الرسائل."""
    menu_id = context.user_data.pop("menu_msg_id", None)
    action_id = context.user_data.pop("last_action_msg_id", None)
    await _safe_delete(context.bot, user_id, menu_id)
    if action_id and action_id != menu_id:
        await _safe_delete(context.bot, user_id, action_id)


async def reply_tracked(context: ContextTypes.DEFAULT_TYPE, target, text: str, **kwargs):
    """يرسل ردًا ويتعقّب رسالته لحذفها عند إجراءٍ لاحق."""
    msg = await target.reply_text(text, **kwargs)
    context.user_data["last_action_msg_id"] = msg.message_id
    return msg


async def send_main_menu(
    context: ContextTypes.DEFAULT_TYPE, bot, user_id: int, lang: str, name: str
) -> None:
    """يحذف القوائم والنتائج القديمة ويرسل قائمة رئيسية واحدة فقط."""
    await _clear_tracked_messages(context, user_id)
    msg = await bot.send_message(
        chat_id=user_id,
        text=t(lang, "welcome", name=esc(name)),
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(user_id, lang),
    )
    context.user_data["menu_msg_id"] = msg.message_id


def esc(text) -> str:
    """تهريب نص لاستخدامه بأمان مع HTML parse mode."""
    if text is None:
        return ""
    return html.escape(str(text))


# ============================================================
#                 حالة المحادثة المؤقتة (Flow State)
# ============================================================
# بدلًا من استخدام ConversationHandler متداخل لعشرات المسارات،
# نستخدم حالة بسيطة داخل user_data لتوجيه الرسالة النصية القادمة
# إلى المعالج المناسب، مع إمكانية الإلغاء والتنظيف دائمًا.

def set_flow(context: ContextTypes.DEFAULT_TYPE, name: str, **data) -> None:
    context.user_data["flow"] = {"name": name, "data": data}


def get_flow(context: ContextTypes.DEFAULT_TYPE) -> Optional[dict]:
    return context.user_data.get("flow")


def clear_flow(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("flow", None)


def cancel_kb() -> InlineKeyboardMarkup:
    """لوحة زر الإلغاء/الرجوع الموحّدة داخل التدفقات النصية.

    مهم: هذه دالة (وليست ثابتًا يُحسب مرة واحدة عند استيراد الملف)
    عمدًا، لأن حسابها يستدعي styled_button/back_btn التي تقرأ/تكتب في
    جدول button_colors — وهذا الجدول لا يكون موجودًا إلا بعد استدعاء
    init_db() داخل main(). لو كانت CANCEL_KB ثابتًا يُحسب عند الاستيراد
    (أي قبل main() وقبل init_db())، سيفشل البوت فورًا بخطأ
    "no such table: button_colors" في بعض بيئات التشغيل (مثل تطبيقات
    تشغيل بايثون على أندرويد) التي تُنفّذ الملف كسكربت مباشرة."""
    return InlineKeyboardMarkup([[back_btn("🔙 رجوع", "flow:back")]])


# ============================================================
#                     الحارس العام (Guards)
# ============================================================

async def guard_banned(update: Update) -> bool:
    """يعيد True إذا كان المستخدم محظورًا (ويرسل له رسالة)."""
    user = update.effective_user
    if user is None:
        return False
    if is_banned(user.id):
        text = t(get_user_lang(user.id), "banned")
        if update.callback_query:
            await update.callback_query.answer(text, show_alert=True)
        elif update.message:
            await update.message.reply_text(text)
        return True
    return False


async def guard_owner_callback(update: Update) -> bool:
    """يعيد True إذا لم يكن المستخدم هو المالك (ويرسل تنبيهًا)."""
    user = update.effective_user
    if not is_owner(user.id):
        msg = t(get_user_lang(user.id), "admin_only")
        if update.callback_query:
            await update.callback_query.answer(msg, show_alert=True)
        elif update.message:
            await update.message.reply_text(msg)
        return True
    return False


async def guard_admin_perm_callback(update: Update, perm: str) -> bool:
    """يعيد True إذا لم يملك المستخدم صلاحية الأدمن المطلوبة (ويرسل تنبيهًا).
    المالك يجتاز هذا الحارس دومًا لأنه يملك كل الصلاحيات."""
    user = update.effective_user
    if has_perm(user.id, perm):
        return False
    msg = t(get_user_lang(user.id), "admin_only")
    if update.callback_query:
        await update.callback_query.answer(msg, show_alert=True)
    elif update.message:
        await update.message.reply_text(msg)
    return True


# ============================================================
#                         /start
# ============================================================

async def notify_owner_new_member(context: ContextTypes.DEFAULT_TYPE, tg_user) -> None:
    """يرسل للمالك إشعارًا عند دخول عضو جديد للبوت لأول مرة فقط (وليس عند
    كل ضغطة /start متكررة). يُستدعى فقط بعد التأكد من أن المستخدم غير
    موجود مسبقًا في قاعدة البيانات."""
    try:
        conn = get_conn()
        total_users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        conn.close()

        username_display = f"@{tg_user.username}" if tg_user.username else "—"
        lang_display = tg_user.language_code or "—"

        text = (
            "👾 <b>شخص جديد دخل البوت</b>\n\n"
            "👤 <b>معلومات العضو الجديد:</b>\n"
            f"• الاسم: {esc(tg_user.first_name or '—')}\n"
            f"• المعرف: {esc(username_display)}\n"
            f"• الآيدي: <code>{tg_user.id}</code>\n"
            f"• 🌐 اللغة: {esc(lang_display)}\n\n"
            f"📊 إجمالي المستخدمين: {total_users}"
        )
        await context.bot.send_message(chat_id=OWNER_ID, text=text, parse_mode=ParseMode.HTML)
    except TelegramError as e:
        logger.warning("⚠️ تعذر إرسال إشعار العضو الجديد للمالك: %s", e)
    except Exception as e:  # لا يجب أن يوقف أي خطأ غير متوقع هنا البوت
        logger.error("خطأ غير متوقع أثناء إشعار العضو الجديد: %s", e, exc_info=e)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    args = context.args

    referred_by = None
    gift_code = None

    if args:
        param = args[0].strip()
        if param.startswith("gift_"):
            gift_code = param[len("gift_"):]
        else:
            try:
                candidate = int(param)
                if candidate != tg_user.id:
                    referred_by = candidate
            except ValueError:
                pass

    is_new = get_user(tg_user.id) is None
    ensure_user(tg_user, referred_by=referred_by)
    lang = get_user_lang(tg_user.id)

    if is_new:
        asyncio.create_task(notify_owner_new_member(context, tg_user))

    if is_banned(tg_user.id):
        await update.message.reply_text(t(lang, "banned"))
        return

    # منح مكافأة الإحالة لأول دخول فقط، مع قفل لمنع التسابق والتكرار
    if is_new and referred_by:
        async with referral_lock:
            await _apply_referral(referred_by, tg_user.id)

    # استخدام رابط هدية إن وُجد ضمن رابط الدخول
    if gift_code:
        async with gift_lock:
            await _apply_gift_code(tg_user.id, gift_code, context)

    # إزالة أي لوحة أزرار ثابتة قديمة أسفل الشاشة (من نسخة سابقة من البوت)،
    # بما أن القائمة أصبحت أزرار Inline مرفقة أسفل الرسالة مباشرة.
    # نرسل رسالة صغيرة بلوحة فارغة (ReplyKeyboardRemove) ثم نحذفها فورًا
    # حتى لا تترك أثرًا مرئيًا في المحادثة.
    try:
        clear_msg = await context.bot.send_message(
            chat_id=tg_user.id, text="🔄", reply_markup=ReplyKeyboardRemove()
        )
        await clear_msg.delete()
    except TelegramError:
        pass

    # التحقق من الاشتراك الإجباري (إن كان مفعّلًا) قبل عرض القائمة الرئيسية،
    # إلا إذا كان المستخدم هو المالك.
    if not is_owner(tg_user.id):
        ok, missing = await check_force_subscription(context.bot, tg_user.id)
        if not ok:
            await _clear_tracked_messages(context, tg_user.id)
            kb = build_force_sub_keyboard(missing, lang)
            text = f"{t(lang, 'fsub_prompt_title')}\n\n{t(lang, 'fsub_prompt_note')}"
            msg = await context.bot.send_message(
                chat_id=tg_user.id, text=text, parse_mode=ParseMode.HTML, reply_markup=kb
            )
            context.user_data["menu_msg_id"] = msg.message_id
            return

    # /start المتكرر: حذف القائمة/النتائج القديمة وإبقاء قائمة واحدة فقط
    await send_main_menu(context, context.bot, tg_user.id, lang, tg_user.first_name)


async def _apply_referral(referrer_id: int, referred_id: int) -> None:
    """يمنح نقاط الإحالة للمُحيل، مع حماية من التكرار عبر UNIQUE(referred_id)."""
    conn = get_conn()
    try:
        existing = conn.execute(
            "SELECT 1 FROM referrals WHERE referred_id=?", (referred_id,)
        ).fetchone()
        if existing:
            conn.close()
            return

        referrer = conn.execute(
            "SELECT user_id FROM users WHERE user_id=?", (referrer_id,)
        ).fetchone()
        if not referrer:
            conn.close()
            return

        points = get_referral_points()
        conn.execute(
            "INSERT INTO referrals (referrer_id, referred_id, points_awarded, created_at) "
            "VALUES (?, ?, ?, ?)",
            (referrer_id, referred_id, points, now_str()),
        )
        conn.execute(
            """UPDATE users SET points = points + ?, total_points_earned = total_points_earned + ?,
               referrals_count = referrals_count + 1, referral_points_earned = referral_points_earned + ?
               WHERE user_id=?""",
            (points, points, points, referrer_id),
        )
        record_transaction(conn, referrer_id, points, "referral", f"إحالة مستخدم جديد ({referred_id})")
        conn.commit()
    finally:
        conn.close()


# ============================================================
#            القائمة الرئيسية: المعالجات النصية للأزرار
# ============================================================

async def handle_main_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or update.message is None:
        return

    tg_user = update.effective_user
    ensure_user(tg_user)

    if await guard_banned(update):
        return

    # إن كان هناك تدفق (flow) نشط، مرر النص لمعالج التدفق أولًا
    flow = get_flow(context)
    if flow:
        await route_flow_text(update, context, flow)
        return

    lang = get_user_lang(tg_user.id)
    await _clear_tracked_messages(context, tg_user.id)
    msg = await update.message.reply_text(
        t(lang, "fallback_menu"),
        reply_markup=main_menu_keyboard(tg_user.id, lang),
    )
    context.user_data["menu_msg_id"] = msg.message_id


async def handle_admin_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """معالج مخصص لاستقبال الملفات (Document) المرسلة أثناء تدفقات لوحة
    تحكم المالك التي تحتاج رفع ملف (حاليًا: تعيين ملف APK للنسخة). لا
    يتعارض مع معالج النصوص العام (handle_main_menu_text) لأنه يعمل فقط
    على رسائل من نوع مستند، بينما ذاك يعمل فقط على الرسائل النصية."""
    if update.effective_user is None or update.message is None:
        return

    tg_user = update.effective_user
    if not is_owner(tg_user.id):
        return

    flow = get_flow(context)
    if not flow or flow.get("name") != "adm_apk_upload":
        return

    await adm_apk_upload_input(update, context, flow)


# ============================================================
#              📋 موجّه أزرار القائمة الرئيسية (Inline)
# ============================================================

async def cb_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يوجّه ضغطات أزرار القائمة الرئيسية المرفقة أسفل الرسالة (menu:xxx)."""
    if await guard_banned(update):
        return
    query = update.callback_query
    tg_user = update.effective_user
    lang = get_user_lang(tg_user.id)
    action = query.data.split(":")[1]

    await query.answer()
    # حذف القائمة الرئيسية وأي نتيجة سابقة حتى لا تتراكم الرسائل
    await _clear_tracked_messages(context, tg_user.id)
    await _safe_delete(context.bot, tg_user.id, query.message.message_id)

    if action == "admin":
        if not is_owner_or_admin(tg_user.id):
            await query.answer(t(lang, "admin_only"), show_alert=True)
            return
        await reply_tracked(
            context,
            query.message,
            t(lang, "admin_panel_title"),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_menu_keyboard(tg_user.id),
        )
        return

    if action == "store":
        await show_products(update, context)
    elif action == "version":
        await send_app_version(update, context)
    elif action == "daily":
        await claim_daily_points(update, context)
    elif action == "account":
        await show_account(update, context)
    elif action == "referral":
        await show_referral(update, context)
    elif action == "video":
        await start_video_flow(update, context)
    elif action == "lang":
        await show_language_menu(update, context)
    elif action == "support":
        await start_support_flow(update, context)


# ============================================================
#                       🌐 زر اللغة
# ============================================================

def language_inline_keyboard() -> InlineKeyboardMarkup:
    rows = []
    row = []
    for code in LANGS:
        row.append(styled_button(LANG_NAMES[code], f"lang:set:{code}", "lang_pick", "🌐 أزرار اختيار اللغة", "primary"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([back_btn("🔙 رجوع", "menu:home")])
    return InlineKeyboardMarkup(rows)


async def show_language_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    lang = get_user_lang(tg_user.id)
    target = update.message or update.callback_query.message
    await reply_tracked(
        context,
        target,
        t(lang, "lang_prompt"),
        reply_markup=language_inline_keyboard(),
    )


async def cb_language_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    tg_user = update.effective_user
    new_lang = query.data.split(":")[2]
    if new_lang not in LANGS:
        await query.answer()
        return
    set_user_lang(tg_user.id, new_lang)
    await query.answer()
    await _clear_tracked_messages(context, tg_user.id)
    await _safe_delete(context.bot, tg_user.id, query.message.message_id)
    msg = await context.bot.send_message(
        chat_id=tg_user.id,
        text=t(new_lang, "lang_saved", lang_name=LANG_NAMES[new_lang]),
        reply_markup=main_menu_keyboard(tg_user.id, new_lang),
    )
    context.user_data["menu_msg_id"] = msg.message_id


async def cb_noop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()


# ============================================================
#                         🛒 المنتجات
# ============================================================

def list_active_products():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM products WHERE is_active=1 ORDER BY product_id"
    ).fetchall()
    conn.close()
    return rows


def count_available_keys(product_id: int) -> int:
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) c FROM keys_store WHERE product_id=? AND is_sold=0",
        (product_id,),
    ).fetchone()
    conn.close()
    return row["c"]


def count_sold_keys(product_id: int) -> int:
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) c FROM keys_store WHERE product_id=? AND is_sold=1",
        (product_id,),
    ).fetchone()
    conn.close()
    return row["c"]


def build_store_keyboard(products, lang: str) -> InlineKeyboardMarkup:
    """يبني شبكة عرض احترافية للمتجر: صف عناوين (السعر/الاسم) يليه صف
    لكل منتج بنفس التنسيق (سعر - اسم)، وكلا الزرين يفتحان صفحة المنتج.
    لون زر كل منتج مستقل تمامًا ويحدده المالك عند إضافة/تعديل المنتج."""
    rows = [
        [
            styled_button(t(lang, "store_price_header"), "noop", "store_price", "📌 عمود «السعر» في رأس المتجر", "success"),
            styled_button(t(lang, "store_name_header"), "noop", "store_name", "📌 عمود «الاسم» في رأس المتجر", "success"),
        ]
    ]
    for p in products:
        available = count_available_keys(p["product_id"])
        price_label = f"💠 {p['price']} {t(lang, 'point_word')}"
        name_label = f"🛍️ {p['name']}"
        if available <= 0:
            name_label += " ❌"
        product_style = p["button_color"] if "button_color" in p.keys() else "danger"
        rows.append(
            [
                styled_button_for_style(price_label, f"prod:view:{p['product_id']}", product_style),
                styled_button_for_style(name_label, f"prod:view:{p['product_id']}", product_style),
            ]
        )
    rows.append([back_btn(t(lang, "btn_back"), "menu:home")])
    return InlineKeyboardMarkup(rows)


async def show_products(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    lang = get_user_lang(tg_user.id)
    products = list_active_products()
    target = update.message or update.callback_query.message

    if not products:
        await reply_tracked(
            context, target, t(lang, "store_empty"), reply_markup=back_home_kb(lang)
        )
        return

    await reply_tracked(
        context,
        target,
        t(lang, "store_title"),
        parse_mode=ParseMode.HTML,
        reply_markup=build_store_keyboard(products, lang),
    )


# ============================================================
#                   📱 زر النسخة (تحميل APK)
# ============================================================

async def send_app_version(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يرسل للمستخدم ملف APK الذي حدّده المالك من لوحة التحكم، مع تسمية
    توضيحية أنيقة (تتضمن وصف النسخة الذي أضافه المالك إن وُجد) وزر رجوع
    مرفق مباشرة أسفل رسالة الملف نفسها — دون إرسال رسالة تأكيد منفصلة.
    إن لم يكن هناك ملف محدَّد بعد، يُعرض للمستخدم رسالة توضّح ذلك."""
    tg_user = update.effective_user
    lang = get_user_lang(tg_user.id)
    target = update.message or update.callback_query.message
    file_id = get_app_apk_file_id()

    if not file_id:
        await reply_tracked(
            context, target, t(lang, "version_not_available"), reply_markup=back_home_kb(lang)
        )
        return

    try:
        msg = await context.bot.send_document(
            chat_id=tg_user.id,
            document=file_id,
            caption=build_app_apk_caption(),
            parse_mode=ParseMode.HTML,
            reply_markup=back_home_kb(lang),
        )
    except (Forbidden, BadRequest, TelegramError) as e:
        logger.warning("⚠️ تعذر إرسال ملف النسخة (APK) للمستخدم %s: %s", tg_user.id, e)
        await reply_tracked(
            context, target, t(lang, "version_send_failed"), reply_markup=back_home_kb(lang)
        )
        return

    # نتعقّب رسالة الملف نفسها (بدل إرسال رسالة تأكيد منفصلة) حتى تُحذف
    # تلقائيًا كبقية الرسائل عند انتقال المستخدم لإجراء آخر.
    context.user_data["last_action_msg_id"] = msg.message_id


async def cb_menu_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """زر الرجوع للقائمة الرئيسية: يعدّل الرسالة الحالية لتصبح القائمة الرئيسية."""
    query = update.callback_query
    tg_user = update.effective_user
    lang = get_user_lang(tg_user.id)
    clear_flow(context)
    await query.answer()
    await _clear_tracked_messages(context, tg_user.id)
    try:
        await query.edit_message_text(
            t(lang, "welcome", name=esc(tg_user.first_name)),
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_keyboard(tg_user.id, lang),
        )
        context.user_data["menu_msg_id"] = query.message.message_id
    except (BadRequest, TelegramError):
        await send_main_menu(context, context.bot, tg_user.id, lang, tg_user.first_name)


async def cb_product_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if await guard_banned(update):
        return
    tg_user = update.effective_user
    lang = get_user_lang(tg_user.id)
    product_id = int(query.data.split(":")[2])

    conn = get_conn()
    p = conn.execute(
        "SELECT * FROM products WHERE product_id=?", (product_id,)
    ).fetchone()
    conn.close()

    if not p or not p["is_active"]:
        await query.answer(t(lang, "product_not_available"), show_alert=True)
        return

    available = count_available_keys(product_id)
    avail_text = f"✅ {t(lang, 'available_word')}" if available > 0 else f"❌ {t(lang, 'unavailable_word')}"
    text = (
        f"{t(lang, 'product_name_label')}: <b>{esc(p['name'])}</b>\n"
        f"{t(lang, 'product_desc_label')}: {esc(p['duration']) if p['duration'] else t(lang, 'product_desc_none')}\n\n"
        f"{t(lang, 'product_price_label')}: {p['price']} {t(lang, 'point_word')}\n"
        f"{t(lang, 'product_avail_label')}: {avail_text}\n"
    )
    kb_rows = []
    if available > 0:
        text += f"\n{t(lang, 'product_confirm')}"
        kb_rows.append(
            [styled_button(t(lang, "btn_buy_now"), f"prod:buy:{product_id}", "btn_buy_now", "✅ زر شراء الآن", "success")]
        )
    else:
        text += f"\n{t(lang, 'product_out_of_stock')}"
    kb_rows.append([back_btn(t(lang, "btn_back_products"), "prod:list")])

    await query.answer()
    await query.edit_message_text(
        text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb_rows)
    )


async def cb_product_list_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    tg_user = update.effective_user
    lang = get_user_lang(tg_user.id)
    products = list_active_products()
    if not products:
        await query.answer()
        try:
            await query.edit_message_text(t(lang, "store_empty"))
        except (BadRequest, TelegramError):
            pass
        return
    await query.answer()
    await query.edit_message_text(
        t(lang, "store_title"),
        parse_mode=ParseMode.HTML,
        reply_markup=build_store_keyboard(products, lang),
    )


async def post_delivery_channel_notice(
    context: ContextTypes.DEFAULT_TYPE,
    buyer_id: int,
    purchase_id: int,
    duration_text: str,
    points_used: int,
) -> None:
    """ينشر إشعار تسليم مفتاح ناجح في قناة تسليم الطلبات المحفوظة (إن وُجدت).
    لا يوقف تنفيذ عملية الشراء أبدًا؛ أي خطأ هنا يُسجَّل فقط في السجلات."""
    channel_id = get_delivery_channel_id()
    if not channel_id:
        return

    buyer = get_user(buyer_id)
    buyer_display = f"@{buyer['username']}" if buyer and buyer["username"] else str(buyer_id)

    text = (
        "📣 <b>تم تسليم طلب المفتاح</b> ✅\n\n"
        f"🔢 رقم الطلب: <code>{purchase_id}</code>\n"
        f"👤 معرف المشتري: {esc(buyer_display)}\n"
        f"🌿 نوع الطلب: {esc(duration_text)}\n"
        f"🪙 النقاط المستخدمة: {points_used}\n"
        f"⏰ التاريخ: {now_str()}"
    )
    kb = InlineKeyboardMarkup([[delivery_open_bot_button()]])
    try:
        await context.bot.send_message(
            chat_id=_delivery_chat_ref(channel_id),
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
    except TelegramError as e:
        logger.warning("⚠️ تعذر النشر في قناة تسليم الطلبات: %s", e)


async def cb_product_buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if await guard_banned(update):
        return
    user_id = update.effective_user.id
    lang = get_user_lang(user_id)
    product_id = int(query.data.split(":")[2])

    # صلاحية الأدمن رقم (1): شراء أكواد بدون خصم نقاط (المالك يملكها دومًا)
    free_purchase = has_perm(user_id, ADMIN_PERM_BUY_FREE)

    async with purchase_lock:
        result = await asyncio.to_thread(_execute_purchase, user_id, product_id, free_purchase)

    if result["status"] == "no_product":
        await query.answer(t(lang, "buy_no_product"), show_alert=True)
        return
    if result["status"] == "no_stock":
        await query.answer(t(lang, "buy_no_stock"), show_alert=True)
        return
    if result["status"] == "insufficient_points":
        await query.answer(
            t(lang, "buy_insufficient", price=result["price"], points=result["points"]),
            show_alert=True,
        )
        return

    await query.answer(t(lang, "buy_success_alert"), show_alert=True)
    p = result["product"]
    key_value = result["key_value"]
    new_balance = result["new_balance"]
    price_paid = result.get("price_paid", p["price"])

    receipt = (
        f"{t(lang, 'receipt_title')}\n\n"
        f"{t(lang, 'receipt_product')}: {esc(p['name'])}\n"
        f"{t(lang, 'receipt_duration')}: {esc(p['duration'])}\n"
        f"{t(lang, 'receipt_price')}: {price_paid} {t(lang, 'point_word')}\n"
        f"{t(lang, 'receipt_key')}: <code>{esc(key_value)}</code>\n"
        f"{t(lang, 'receipt_balance')}: {new_balance} {t(lang, 'point_word')}\n"
        f"{t(lang, 'receipt_date')}: {now_str()}"
    )
    sent = await context.bot.send_message(
        chat_id=user_id,
        text=receipt,
        parse_mode=ParseMode.HTML,
        reply_markup=back_home_kb(lang),
    )
    context.user_data["last_action_msg_id"] = sent.message_id

    try:
        await query.edit_message_text(
            receipt, parse_mode=ParseMode.HTML, reply_markup=back_home_kb(lang)
        )
    except (BadRequest, TelegramError):
        pass

    await post_delivery_channel_notice(
        context, user_id, result["purchase_id"], p["duration"], price_paid
    )


def _execute_purchase(user_id: int, product_id: int, free_purchase: bool = False) -> dict:
    """ينفذ عملية الشراء بشكل ذري (atomic) داخل معاملة واحدة.

    free_purchase=True: تُستخدم لصلاحية الأدمن رقم (1) «شراء أكواد بدون
    خصم نقاط» — يحصل على المفتاح دون أي خصم من رصيده ودون التحقق من
    كفاية النقاط."""
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        product = conn.execute(
            "SELECT * FROM products WHERE product_id=? AND is_active=1", (product_id,)
        ).fetchone()
        if not product:
            conn.execute("ROLLBACK")
            return {"status": "no_product"}

        user = conn.execute(
            "SELECT * FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if not user:
            conn.execute("ROLLBACK")
            return {"status": "insufficient_points", "price": product["price"], "points": 0}
        if not free_purchase and user["points"] < product["price"]:
            conn.execute("ROLLBACK")
            return {
                "status": "insufficient_points",
                "price": product["price"],
                "points": user["points"],
            }

        key_row = conn.execute(
            "SELECT * FROM keys_store WHERE product_id=? AND is_sold=0 "
            "ORDER BY key_id LIMIT 1",
            (product_id,),
        ).fetchone()
        if not key_row:
            conn.execute("ROLLBACK")
            return {"status": "no_stock"}

        # تعليم المفتاح كمباع بشكل ذري لمنع بيعه مرتين
        updated = conn.execute(
            "UPDATE keys_store SET is_sold=1, sold_to=?, sold_at=? "
            "WHERE key_id=? AND is_sold=0",
            (user_id, now_str(), key_row["key_id"]),
        )
        if updated.rowcount == 0:
            conn.execute("ROLLBACK")
            return {"status": "no_stock"}

        if free_purchase:
            conn.execute(
                "UPDATE users SET purchases_count = purchases_count + 1 WHERE user_id=?",
                (user_id,),
            )
            new_balance = user["points"]
            price_paid = 0
        else:
            conn.execute(
                "UPDATE users SET points = points - ?, purchases_count = purchases_count + 1 "
                "WHERE user_id=?",
                (product["price"], user_id),
            )
            new_balance = record_transaction(
                conn, user_id, -product["price"], "purchase", f"شراء مفتاح للمنتج {product['name']}"
            )
            price_paid = product["price"]

        purchase_cur = conn.execute(
            """INSERT INTO purchases (user_id, product_id, key_id, price_paid, purchased_at, source)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                user_id, product_id, key_row["key_id"], price_paid, now_str(),
                "store" if not free_purchase else "admin_free",
            ),
        )
        conn.commit()
        return {
            "status": "ok",
            "product": dict(product),
            "key_value": key_row["key_value"],
            "new_balance": new_balance,
            "price_paid": price_paid,
            "purchase_id": purchase_cur.lastrowid,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ============================================================
#                     🎁 النقاط اليومية
# ============================================================

async def claim_daily_points(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    lang = get_user_lang(user_id)
    target = update.message or update.callback_query.message

    async with daily_lock:
        result = await asyncio.to_thread(_execute_daily_claim, user_id)

    if result["status"] == "wait":
        remaining: timedelta = result["remaining"]
        hours, rem = divmod(int(remaining.total_seconds()), 3600)
        minutes, seconds = divmod(rem, 60)
        await reply_tracked(
            context,
            target,
            t(lang, "daily_wait", h=hours, m=minutes, s=seconds),
            reply_markup=back_home_kb(lang),
        )
        return

    await reply_tracked(
        context,
        target,
        t(lang, "daily_success", points=result["points"], balance=result["new_balance"]),
        parse_mode=ParseMode.HTML,
        reply_markup=back_home_kb(lang),
    )


def _execute_daily_claim(user_id: int) -> dict:
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        user = conn.execute(
            "SELECT * FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if not user:
            conn.execute("ROLLBACK")
            return {"status": "error"}

        now = datetime.utcnow()
        if user["last_daily_claim"]:
            last_claim = datetime.strptime(user["last_daily_claim"], "%Y-%m-%d %H:%M:%S")
            elapsed = now - last_claim
            if elapsed < timedelta(hours=24):
                conn.execute("ROLLBACK")
                return {"status": "wait", "remaining": timedelta(hours=24) - elapsed}

        points = get_daily_points()
        conn.execute(
            "UPDATE users SET points = points + ?, total_points_earned = total_points_earned + ?, "
            "last_daily_claim = ? WHERE user_id=?",
            (points, points, now_str(), user_id),
        )
        new_balance = record_transaction(conn, user_id, points, "daily", "مكافأة النقاط اليومية")
        conn.execute(
            "INSERT INTO daily_points_log (user_id, points, claimed_at) VALUES (?, ?, ?)",
            (user_id, points, now_str()),
        )
        conn.commit()
        return {"status": "ok", "points": points, "new_balance": new_balance}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ============================================================
#                          👤 الحساب
# ============================================================

async def show_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    lang = get_user_lang(tg_user.id)
    target = update.message or update.callback_query.message
    user = get_user(tg_user.id)
    if not user:
        await reply_tracked(
            context, target, t(lang, "account_error"), reply_markup=back_home_kb(lang)
        )
        return

    username_display = f"@{user['username']}" if user["username"] else "—"
    text = (
        f"{t(lang, 'account_title')}\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"{t(lang, 'account_name')}: <b>{esc(user['first_name'])}</b>\n"
        f"{t(lang, 'account_username')}: {esc(username_display)}\n"
        f"{t(lang, 'account_id')}: <code>{user['user_id']}</code>\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"{t(lang, 'account_points')}: <b>{user['points']}</b> {t(lang, 'point_word')}\n"
        f"{t(lang, 'account_total_points')}: {user['total_points_earned']}\n"
        f"{t(lang, 'account_purchases')}: {user['purchases_count']}\n"
        f"{t(lang, 'account_referrals')}: {user['referrals_count']}\n"
        f"{t(lang, 'account_referral_points')}: {user['referral_points_earned']}\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"{t(lang, 'account_joined')}: {user['joined_at']}\n"
        f"{t(lang, 'account_last_active')}: {user['last_active']}\n"
    )
    kb = InlineKeyboardMarkup(
        [
            [
                styled_button(
                    t(lang, "account_transfer_btn"),
                    "acct:transfer",
                    "btn_transfer",
                    "🔄 زر تحويل النقاط",
                    "primary",
                )
            ],
            [back_btn(t(lang, "btn_back"), "menu:home")],
        ]
    )
    await reply_tracked(context, target, text, parse_mode=ParseMode.HTML, reply_markup=kb)


# ============================================================
#              🔄 تحويل النقاط بين الأعضاء
# ============================================================
# يسمح لأي عضو بتحويل جزء من نقاطه لعضو آخر يستخدم البوت، مع عمولة
# 10% تُخصم إضافةً على المرسل (لا تُخصم من مبلغ المستلم). تُنفَّذ
# عملية الخصم والإضافة بشكل ذرّي بالكامل داخل معاملة قاعدة بيانات
# واحدة (BEGIN IMMEDIATE) لمنع أي تسابق أو ازدواجية، مع تنظيف حالة
# التدفق (flow) فور الضغط على زر التأكيد لمنع أي معالجة مزدوجة عند
# الضغط أكثر من مرة على نفس الزر.

async def cb_transfer_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_banned(update):
        return
    query = update.callback_query
    tg_user = update.effective_user
    lang = get_user_lang(tg_user.id)
    set_flow(context, "xfer_target")
    await query.answer()
    await query.message.reply_text(t(lang, "xfer_ask_target"), reply_markup=cancel_kb())


async def xfer_target_input(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    tg_user = update.effective_user
    lang = get_user_lang(tg_user.id)
    q = update.message.text.strip()
    recipient = find_user_by_query(q)
    if not recipient:
        await update.message.reply_text(t(lang, "xfer_target_not_found"), reply_markup=cancel_kb())
        return
    if recipient["user_id"] == tg_user.id:
        await update.message.reply_text(t(lang, "xfer_target_self"), reply_markup=cancel_kb())
        return
    set_flow(context, "xfer_amount", target_id=recipient["user_id"])
    await update.message.reply_text(
        f"👤 {esc(recipient['first_name'])} (<code>{recipient['user_id']}</code>)\n\n"
        f"{t(lang, 'xfer_ask_amount')}",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_kb(),
    )


async def xfer_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    tg_user = update.effective_user
    lang = get_user_lang(tg_user.id)
    text = update.message.text.strip()
    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text(t(lang, "xfer_invalid_amount"), reply_markup=cancel_kb())
        return
    amount = int(text)

    sender = get_user(tg_user.id)
    current_balance = sender["points"] if sender else 0
    if not sender or current_balance < amount:
        await update.message.reply_text(
            t(lang, "xfer_insufficient", balance=current_balance), reply_markup=cancel_kb()
        )
        return

    target_id = flow["data"]["target_id"]
    commission = amount // 10
    net = amount - commission

    set_flow(context, "xfer_confirm", target_id=target_id, amount=amount, commission=commission, net=net)
    kb = InlineKeyboardMarkup(
        [
            [
                styled_button(t(lang, "xfer_btn_confirm"), "xfer:confirm", "xfer_confirm_btn", "✅ زر تأكيد التحويل", "success"),
                styled_button(t(lang, "xfer_btn_cancel"), "xfer:cancel", "xfer_cancel_btn", "❌ زر إلغاء التحويل", "danger"),
            ]
        ]
    )
    await update.message.reply_text(
        f"{t(lang, 'xfer_confirm_title')}\n\n"
        f"{t(lang, 'xfer_confirm_details', amount=amount, commission=commission, net=net)}",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


async def cb_transfer_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_banned(update):
        return
    query = update.callback_query
    tg_user = update.effective_user
    lang = get_user_lang(tg_user.id)
    clear_flow(context)
    await query.answer()
    try:
        await query.edit_message_text(t(lang, "xfer_cancelled"))
    except (BadRequest, TelegramError):
        pass


def _execute_transfer(sender_id: int, recipient_id: int, amount: int) -> dict:
    """ينفذ عملية تحويل النقاط بشكل ذرّي (atomic) داخل معاملة واحدة.
    لا يتم خصم أو إضافة أي نقطة إلا إذا نجحت العملية بالكامل."""
    commission = amount // 10
    net = amount - commission

    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        sender = conn.execute("SELECT * FROM users WHERE user_id=?", (sender_id,)).fetchone()
        recipient = conn.execute("SELECT * FROM users WHERE user_id=?", (recipient_id,)).fetchone()

        if not recipient:
            conn.execute("ROLLBACK")
            return {"status": "not_found"}

        if not sender or sender["points"] < amount:
            conn.execute("ROLLBACK")
            return {"status": "insufficient", "balance": sender["points"] if sender else 0}

        conn.execute(
            "UPDATE users SET points = points - ? WHERE user_id=?", (amount, sender_id)
        )
        conn.execute(
            "UPDATE users SET points = points + ?, total_points_earned = total_points_earned + ? "
            "WHERE user_id=?",
            (net, net, recipient_id),
        )
        sender_balance = record_transaction(
            conn, sender_id, -amount, "transfer_out", f"تحويل نقاط إلى {recipient_id} (عمولة {commission})"
        )
        recipient_balance = record_transaction(
            conn, recipient_id, net, "transfer_in", f"استلام تحويل نقاط من {sender_id}"
        )
        conn.commit()
        return {
            "status": "ok",
            "commission": commission,
            "net": net,
            "sender_balance": sender_balance,
            "recipient_balance": recipient_balance,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def cb_transfer_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_banned(update):
        return
    query = update.callback_query
    tg_user = update.effective_user
    lang = get_user_lang(tg_user.id)

    flow = get_flow(context)
    if not flow or flow.get("name") != "xfer_confirm":
        await query.answer(t(lang, "xfer_no_pending"), show_alert=True)
        return

    data = flow["data"]
    # تنظيف حالة التدفق فورًا (قبل أي عملية غير متزامنة) لمنع أي معالجة
    # مزدوجة في حال الضغط على الزر أكثر من مرة بسرعة.
    clear_flow(context)
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except (BadRequest, TelegramError):
        pass

    async with transfer_lock:
        result = await asyncio.to_thread(
            _execute_transfer, tg_user.id, data["target_id"], data["amount"]
        )

    if result["status"] == "not_found":
        await query.message.reply_text(t(lang, "xfer_target_not_found"))
        return
    if result["status"] == "insufficient":
        await query.message.reply_text(t(lang, "xfer_insufficient", balance=result["balance"]))
        return
    if result["status"] != "ok":
        await query.message.reply_text(t(lang, "xfer_error"))
        return

    await query.message.reply_text(
        t(
            lang,
            "xfer_success_sender",
            net=result["net"],
            commission=result["commission"],
            balance=result["sender_balance"],
        ),
        parse_mode=ParseMode.HTML,
    )
    try:
        recv_lang = get_user_lang(data["target_id"])
        await context.bot.send_message(
            chat_id=data["target_id"],
            text=t(
                recv_lang,
                "xfer_success_receiver",
                net=result["net"],
                name=esc(tg_user.first_name),
                balance=result["recipient_balance"],
            ),
            parse_mode=ParseMode.HTML,
        )
    except (Forbidden, TelegramError):
        pass


# ============================================================
#              🎥 كود بدون تجميع نقاط (طلبات الفيديو)
# ============================================================

async def start_video_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    lang = get_user_lang(tg_user.id)
    target = update.message or update.callback_query.message
    min_likes = get_min_video_likes()
    text = (
        f"{t(lang, 'video_title')}\n\n"
        f"{t(lang, 'video_steps_label')}\n"
        f"{t(lang, 'video_step1')}\n"
        f"{t(lang, 'video_step2')}\n"
        f"{t(lang, 'video_step3', likes=min_likes)}\n"
        f"{t(lang, 'video_step4')}\n\n"
        f"{t(lang, 'video_note')}\n\n"
        f"{t(lang, 'video_send_prompt')}"
    )
    set_flow(context, "video_link")
    # زر رجوع بدل زر إلغاء العملية
    await reply_tracked(
        context, target, text, parse_mode=ParseMode.HTML, reply_markup=lang_back_kb(lang)
    )


async def process_video_link(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    tg_user = update.effective_user
    lang = get_user_lang(tg_user.id)
    link = update.message.text.strip()
    url_pattern = re.compile(r"^https?://\S+$")
    if not url_pattern.match(link):
        await reply_tracked(
            context,
            update.message,
            t(lang, "video_invalid_link"),
            reply_markup=lang_back_kb(lang),
        )
        return

    conn = get_conn()
    conn.execute(
        """INSERT INTO video_requests (user_id, username, video_link, status, requested_at)
           VALUES (?, ?, ?, 'pending', ?)""",
        (tg_user.id, tg_user.username or "", link, now_str()),
    )
    request_id = conn.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    conn.commit()
    conn.close()

    clear_flow(context)
    await _clear_tracked_messages(context, tg_user.id)
    await reply_tracked(
        context,
        update.message,
        t(lang, "video_submitted"),
        reply_markup=back_home_kb(lang),
    )

    owner_text = (
        "🎥 <b>طلب مراجعة فيديو جديد</b>\n\n"
        f"👤 الاسم: {esc(tg_user.first_name)}\n"
        f"🔖 المعرف: {esc('@' + tg_user.username) if tg_user.username else '—'}\n"
        f"🆔 آيدي: <code>{tg_user.id}</code>\n"
        f"🔗 رابط الفيديو: {esc(link)}\n"
        f"📅 التاريخ: {now_str()}\n"
        f"🏷️ حالة الطلب: قيد الانتظار"
    )
    kb = video_request_kb(request_id)
    try:
        await context.bot.send_message(
            chat_id=OWNER_ID, text=owner_text, parse_mode=ParseMode.HTML, reply_markup=kb
        )
    except TelegramError as e:
        logger.error("فشل إرسال طلب الفيديو للمالك: %s", e)

    # إشعار كل أدمن يملك صلاحية (4) الموافقة على طلبات الأكواد المجانية
    for a in list_admins():
        if ADMIN_PERM_APPROVE_FREE not in set(a.get("permissions", "").split(",")):
            continue
        try:
            await context.bot.send_message(
                chat_id=a["user_id"], text=owner_text, parse_mode=ParseMode.HTML, reply_markup=video_request_kb(request_id)
            )
        except TelegramError:
            pass


async def cb_video_accept(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if await guard_admin_perm_callback(update, ADMIN_PERM_APPROVE_FREE):
        return
    request_id = int(query.data.split(":")[2])

    conn = get_conn()
    req = conn.execute(
        "SELECT * FROM video_requests WHERE request_id=?", (request_id,)
    ).fetchone()
    conn.close()
    if not req or req["status"] != "pending":
        await query.answer("تم التعامل مع هذا الطلب مسبقًا.", show_alert=True)
        return

    products = list_active_products()
    if not products:
        await query.answer("لا توجد منتجات لمنح مفتاح منها. أضف منتجًا أولًا.", show_alert=True)
        return

    buttons = []
    for p in products:
        available = count_available_keys(p["product_id"])
        label = f"{p['name']} (متوفر: {available})"
        buttons.append(
            [styled_button(label, f"vr:grant:{request_id}:{p['product_id']}", "vr_grant", "🎁 زر منح منتج من طلب فيديو", "success")]
        )
    buttons.append([back_btn("🔙 رجوع", f"vr:back:{request_id}")])

    await query.answer()
    await query.edit_message_text(
        query.message.text_html + "\n\n👇 اختر المنتج الذي سيُمنح مفتاحه للمستخدم:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cb_video_grant(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if await guard_admin_perm_callback(update, ADMIN_PERM_APPROVE_FREE):
        return
    _, _, request_id_s, product_id_s = query.data.split(":")
    request_id, product_id = int(request_id_s), int(product_id_s)

    async with purchase_lock:
        result = await asyncio.to_thread(_grant_free_key, request_id, product_id)

    if result["status"] == "already_done":
        await query.answer("تم التعامل مع هذا الطلب مسبقًا.", show_alert=True)
        return
    if result["status"] == "no_stock":
        await query.answer("😔 لا توجد مفاتيح متوفرة لهذا المنتج.", show_alert=True)
        return

    user_id = result["user_id"]
    key_value = result["key_value"]
    product = result["product"]

    await query.answer("✅ تم القبول ومنح المفتاح.", show_alert=True)
    await query.edit_message_text(
        query.message.text + f"\n\n✅ تم القبول، وتم منح مفتاح المنتج «{product['name']}».",
        reply_markup=None,
    )

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "🎉 <b>تم قبول طلبك!</b>\n\n"
                f"📦 المنتج: {esc(product['name'])}\n"
                f"🔑 المفتاح: <code>{esc(key_value)}</code>\n"
                "شكرًا لدعمك ومشاركتك الفيديو 🌟"
            ),
            parse_mode=ParseMode.HTML,
        )
    except (Forbidden, TelegramError) as e:
        logger.warning("تعذر إرسال المفتاح للمستخدم %s: %s", user_id, e)

    await post_delivery_channel_notice(
        context, user_id, result["purchase_id"], product["duration"], 0
    )


def _grant_free_key(request_id: int, product_id: int) -> dict:
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        req = conn.execute(
            "SELECT * FROM video_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if not req or req["status"] != "pending":
            conn.execute("ROLLBACK")
            return {"status": "already_done"}

        product = conn.execute(
            "SELECT * FROM products WHERE product_id=?", (product_id,)
        ).fetchone()
        key_row = conn.execute(
            "SELECT * FROM keys_store WHERE product_id=? AND is_sold=0 ORDER BY key_id LIMIT 1",
            (product_id,),
        ).fetchone()
        if not key_row:
            conn.execute("ROLLBACK")
            return {"status": "no_stock"}

        conn.execute(
            "UPDATE keys_store SET is_sold=1, sold_to=?, sold_at=? WHERE key_id=? AND is_sold=0",
            (req["user_id"], now_str(), key_row["key_id"]),
        )
        conn.execute(
            "UPDATE users SET purchases_count = purchases_count + 1 WHERE user_id=?",
            (req["user_id"],),
        )
        purchase_cur = conn.execute(
            """INSERT INTO purchases (user_id, product_id, key_id, price_paid, purchased_at, source)
               VALUES (?, ?, ?, 0, ?, 'video_reward')""",
            (req["user_id"], product_id, key_row["key_id"], now_str()),
        )
        conn.execute(
            "UPDATE video_requests SET status='approved', reviewed_at=?, reward_note=? WHERE request_id=?",
            (now_str(), f"key:{key_row['key_value']}", request_id),
        )
        conn.commit()
        return {
            "status": "ok",
            "user_id": req["user_id"],
            "key_value": key_row["key_value"],
            "product": dict(product),
            "purchase_id": purchase_cur.lastrowid,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def cb_video_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if await guard_admin_perm_callback(update, ADMIN_PERM_APPROVE_FREE):
        return
    request_id = int(query.data.split(":")[2])
    conn = get_conn()
    req = conn.execute(
        "SELECT * FROM video_requests WHERE request_id=?", (request_id,)
    ).fetchone()
    conn.close()
    if not req:
        await query.answer()
        return
    text = (
        "🎥 <b>طلب مراجعة فيديو</b>\n\n"
        f"🆔 آيدي: <code>{req['user_id']}</code>\n"
        f"🔗 رابط الفيديو: {esc(req['video_link'])}\n"
        f"📅 التاريخ: {req['requested_at']}\n"
    )
    kb = video_request_kb(request_id)
    await query.answer()
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cb_video_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if await guard_admin_perm_callback(update, ADMIN_PERM_APPROVE_FREE):
        return
    request_id = int(query.data.split(":")[2])

    conn = get_conn()
    req = conn.execute(
        "SELECT * FROM video_requests WHERE request_id=?", (request_id,)
    ).fetchone()
    conn.close()
    if not req or req["status"] != "pending":
        await query.answer("تم التعامل مع هذا الطلب مسبقًا.", show_alert=True)
        return

    set_flow(context, "video_reject_reason", request_id=request_id)
    await query.answer()
    await query.message.reply_text(
        "✏️ أرسل سبب الرفض (اختياري)، أو أرسل «-» لتخطي كتابة السبب.",
        reply_markup=cancel_kb(),
    )


async def process_video_reject_reason(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    request_id = flow["data"]["request_id"]
    reason = update.message.text.strip()
    reason = None if reason == "-" else reason

    conn = get_conn()
    conn.execute("BEGIN IMMEDIATE")
    req = conn.execute(
        "SELECT * FROM video_requests WHERE request_id=?", (request_id,)
    ).fetchone()
    if not req or req["status"] != "pending":
        conn.execute("ROLLBACK")
        conn.close()
        clear_flow(context)
        await update.message.reply_text("تم التعامل مع هذا الطلب مسبقًا.")
        return

    conn.execute(
        "UPDATE video_requests SET status='rejected', reviewed_at=?, reject_reason=? WHERE request_id=?",
        (now_str(), reason, request_id),
    )
    conn.commit()
    conn.close()

    clear_flow(context)
    await update.message.reply_text("❌ تم رفض الطلب وإبلاغ المستخدم.")

    reason_text = f"\n📝 السبب: {esc(reason)}" if reason else ""
    try:
        await context.bot.send_message(
            chat_id=req["user_id"],
            text=f"❌ نأسف، تم رفض طلب الفيديو الخاص بك.{reason_text}",
            parse_mode=ParseMode.HTML,
        )
    except (Forbidden, TelegramError):
        pass


# ============================================================
#                       🔗 رابط الدعوة
# ============================================================

async def show_referral(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    lang = get_user_lang(tg_user.id)
    target = update.message or update.callback_query.message
    user = get_user(tg_user.id)
    link = f"https://t.me/{BOT_USERNAME}?start={tg_user.id}"
    text = (
        f"{t(lang, 'referral_title')}\n\n"
        f"{link}\n\n"
        f"{t(lang, 'referral_count')}: {user['referrals_count']}\n"
        f"{t(lang, 'referral_points')}: {user['referral_points_earned']}\n\n"
        f"{t(lang, 'referral_note', points=get_referral_points())}"
    )
    await reply_tracked(
        context, target, text, parse_mode=ParseMode.HTML, reply_markup=back_home_kb(lang)
    )


# ============================================================
#                       📩 الدعم (Support)
# ============================================================

async def start_support_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يبدأ تدفق رسالة الدعم: ينتظر رسالة نصية من المستخدم لإرسالها للمالك."""
    tg_user = update.effective_user
    lang = get_user_lang(tg_user.id)
    target = update.message or update.callback_query.message
    set_flow(context, "support_message")
    await reply_tracked(
        context,
        target,
        t(lang, "support_prompt"),
        reply_markup=lang_cancel_kb(lang),
    )


async def process_support_message(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    """يستقبل رسالة الدعم من المستخدم ويرسلها إلى مالك البوت مع بيانات المرسل."""
    tg_user = update.effective_user
    lang = get_user_lang(tg_user.id)

    message_text = (update.message.text or "").strip()
    if not message_text:
        await reply_tracked(
            context,
            update.message,
            t(lang, "support_prompt"),
            reply_markup=lang_cancel_kb(lang),
        )
        return

    clear_flow(context)

    username_display = f"@{tg_user.username}" if tg_user.username else esc(tg_user.first_name)
    support_text = (
        "📩 <b>رسالة دعم جديدة</b>\n\n"
        f"👤 <b>المستخدم:</b> {esc(username_display)}\n"
        f"🆔 <b>الآيدي:</b> <code>{tg_user.id}</code>\n"
        f"💬 <b>الرسالة:</b> {esc(message_text)}"
    )
    try:
        await context.bot.send_message(
            chat_id=OWNER_ID, text=support_text, parse_mode=ParseMode.HTML
        )
    except (Forbidden, TelegramError):
        logger.warning("تعذر إرسال رسالة الدعم إلى المالك (chat_id=%s).", OWNER_ID)

    await _clear_tracked_messages(context, tg_user.id)
    await reply_tracked(
        context,
        update.message,
        t(lang, "support_sent"),
        reply_markup=back_home_kb(lang),
    )


# ============================================================
#                 توجيه رسائل التدفقات النصية (Flow Router)
# ============================================================

async def route_flow_text(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    name = flow["name"]

    handlers_map = {
        "video_link": process_video_link,
        "video_reject_reason": process_video_reject_reason,
        "support_message": process_support_message,
        "adm_points_charge_target": adm_points_charge_target_input,
        "adm_points_charge_amount": adm_points_charge_amount_input,
        "adm_points_deduct_target": adm_points_deduct_target_input,
        "adm_points_deduct_amount": adm_points_deduct_amount_input,
        "adm_points_balance_target": adm_points_balance_target_input,
        "xfer_target": xfer_target_input,
        "xfer_amount": xfer_amount_input,
        "adm_fs_add": adm_fs_add_input,
        "adm_users_search": adm_users_search_input,
        "adm_ban_target": adm_ban_target_input,
        "adm_unban_target": adm_unban_target_input,
        "adm_product_add_name": adm_product_add_name_input,
        "adm_product_add_duration": adm_product_add_duration_input,
        "adm_product_add_price": adm_product_add_price_input,
        "adm_product_edit_name": adm_product_edit_name_input,
        "adm_product_edit_price": adm_product_edit_price_input,
        "adm_product_edit_duration": adm_product_edit_duration_input,
        "adm_keys_add_bulk": adm_keys_add_bulk_input,
        "adm_gift_points": adm_gift_points_input,
        "adm_gift_uses": adm_gift_uses_input,
        "adm_gift_expiry": adm_gift_expiry_input,
        "adm_broadcast_content": adm_broadcast_content_input,
        "adm_bc_one_target": adm_bc_one_target_input,
        "adm_bc_one_content": adm_bc_one_content_input,
        "adm_settings_daily": adm_settings_daily_input,
        "adm_settings_referral": adm_settings_referral_input,
        "adm_settings_min_likes": adm_settings_min_likes_input,
        "adm_delivery_channel_set": adm_delivery_channel_set_input,
        "adm_apk_upload": adm_apk_upload_wrong_type,
        "adm_apk_caption_set": adm_apk_caption_set_input,
        "adm_admin_target": adm_admin_target_input,
        "adm_emoji_set_custom": adm_emoji_set_custom_input,
        "adm_emoji_search": adm_emoji_search_input,
    }

    handler = handlers_map.get(name)
    if handler:
        await handler(update, context, flow)
    else:
        clear_flow(context)


async def cb_flow_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """زر رجوع داخل التدفقات: يُلغي التدفق ويعيد القائمة الرئيسية."""
    await cb_menu_home(update, context)


# ============================================================
#                    👑 لوحة تحكم المالك: الأقسام
# ============================================================

async def cb_admin_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """موجّه عام لكل أزرار لوحة التحكم التي تبدأ بـ adm: — يسمح بالدخول
    للمالك دومًا، وللأدمن فقط إن كان يملك صلاحية القسم المطلوب."""
    try:
        await _cb_admin_router_impl(update, context)
    except Exception as e:
        logger.error("خطأ داخل لوحة التحكم (cb_admin_router): %s", e, exc_info=e)
        try:
            await update.callback_query.answer(
                f"⚠️ حدث خطأ أثناء فتح هذا القسم: {esc(str(e))[:150]}", show_alert=True
            )
        except TelegramError:
            pass


async def _cb_admin_router_impl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_owner_or_admin(user_id):
        await guard_owner_callback(update)
        return

    query = update.callback_query
    data = query.data

    # الأقسام المقيّدة بصلاحية محددة للأدمن (المالك يجتازها دومًا)
    section_perm = {
        "adm:points": ADMIN_PERM_POINTS,
        "adm:gift": ADMIN_PERM_GIFT,
        "adm:broadcast": ADMIN_PERM_BROADCAST,
        "adm:ban": ADMIN_PERM_BAN,
        "adm:vr": ADMIN_PERM_APPROVE_FREE,
    }
    # الأقسام المقتصرة على المالك فقط
    owner_only_sections = {
        "adm:users", "adm:products", "adm:keys", "adm:stats",
        "adm:settings", "adm:colors", "adm:emoji", "adm:fs", "adm:st:delch", "adm:st:apk", "adm:admins",
    }

    if data in owner_only_sections and not is_owner(user_id):
        await query.answer(t(get_user_lang(user_id), "admin_only"), show_alert=True)
        return
    if data in section_perm and not has_perm(user_id, section_perm[data]):
        await query.answer(t(get_user_lang(user_id), "admin_only"), show_alert=True)
        return

    if data == "adm:menu":
        await query.answer()
        await query.edit_message_text(
            "👑 <b>لوحة التحكم</b>\nاختر القسم الذي تريد إدارته:",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_menu_keyboard(user_id),
        )
    elif data == "adm:close":
        await query.answer()
        try:
            await query.delete_message()
        except (BadRequest, TelegramError):
            pass
    elif data == "adm:users":
        await adm_show_users_menu(update, context)
    elif data == "adm:points":
        await adm_show_points_menu(update, context)
    elif data == "adm:products":
        await adm_show_products_menu(update, context)
    elif data == "adm:keys":
        await adm_show_keys_menu(update, context)
    elif data == "adm:gift":
        await adm_show_gift_menu(update, context)
    elif data == "adm:broadcast":
        await adm_show_broadcast_menu(update, context)
    elif data == "adm:ban":
        await adm_show_ban_menu(update, context)
    elif data == "adm:stats":
        await adm_show_stats(update, context)
    elif data == "adm:settings":
        await adm_show_settings_menu(update, context)
    elif data == "adm:vr":
        await adm_show_video_requests(update, context)
    elif data == "adm:colors":
        await adm_show_colors_menu(update, context, page=0)
    elif data == "adm:emoji":
        await adm_show_emoji_menu(update, context, page=0)
    elif data == "adm:fs":
        await adm_show_forcesub_menu(update, context)
    elif data == "adm:st:delch":
        await adm_show_delivery_channel_menu(update, context)
    elif data == "adm:st:apk":
        await adm_show_apk_menu(update, context)
    elif data == "adm:admins":
        await adm_show_admins_menu(update, context)
    else:
        await query.answer()


# --------------------- 👥 إدارة المستخدمين ---------------------

async def adm_show_users_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    banned = conn.execute("SELECT COUNT(*) c FROM users WHERE is_banned=1").fetchone()["c"]
    purchases = conn.execute("SELECT COUNT(*) c FROM purchases").fetchone()["c"]
    total_points = conn.execute("SELECT COALESCE(SUM(points),0) s FROM users").fetchone()["s"]
    referrals = conn.execute("SELECT COUNT(*) c FROM referrals").fetchone()["c"]
    active_24h = conn.execute(
        "SELECT COUNT(*) c FROM users WHERE last_active >= ?",
        ((datetime.utcnow() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S"),),
    ).fetchone()["c"]
    conn.close()

    text = (
        "👥 <b>إدارة المستخدمين</b>\n\n"
        f"إجمالي المستخدمين: {total}\n"
        f"نشطون خلال 24 ساعة: {active_24h}\n"
        f"إجمالي المشتريات: {purchases}\n"
        f"إجمالي النقاط المتداولة: {total_points}\n"
        f"إجمالي الإحالات: {referrals}\n"
        f"المستخدمون المحظورون: {banned}\n"
    )
    kb = InlineKeyboardMarkup(
        [
            [styled_button("🔎 بحث عن مستخدم", "adm:users:search", "adm_users_search", "🔎 زر بحث عن مستخدم", "primary")],
            [back_btn("🔙 رجوع", "adm:menu")],
        ]
    )
    await query.answer()
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cb_users_search_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    set_flow(context, "adm_users_search")
    await query.answer()
    await query.message.reply_text(
        "🔎 أرسل آيدي المستخدم أو Username (بدون أو مع @) للبحث:", reply_markup=cancel_kb()
    )


def format_user_details(user: sqlite3.Row) -> str:
    username_display = f"@{user['username']}" if user["username"] else "—"
    banned_display = "نعم 🚫" if user["is_banned"] else "لا ✅"
    return (
        "👤 <b>بيانات المستخدم</b>\n\n"
        f"📛 الاسم: {esc(user['first_name'])}\n"
        f"🔖 المعرف: {esc(username_display)}\n"
        f"🆔 آيدي: <code>{user['user_id']}</code>\n"
        f"💰 النقاط: {user['points']}\n"
        f"📈 إجمالي النقاط المكتسبة: {user['total_points_earned']}\n"
        f"🛍️ المشتريات: {user['purchases_count']}\n"
        f"👥 الإحالات: {user['referrals_count']}\n"
        f"📅 الانضمام: {user['joined_at']}\n"
        f"🕒 آخر نشاط: {user['last_active']}\n"
        f"🚫 محظور: {banned_display}\n"
    )


def find_user_by_query(q: str) -> Optional[sqlite3.Row]:
    q = q.strip()
    if q.startswith("@"):
        return get_user_by_username(q)
    if q.isdigit():
        return get_user(int(q))
    return get_user_by_username(q)


async def adm_users_search_input(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    q = update.message.text.strip()
    clear_flow(context)
    user = find_user_by_query(q)
    if not user:
        await update.message.reply_text("❌ لم يتم العثور على المستخدم.")
        return
    await update.message.reply_text(format_user_details(user), parse_mode=ParseMode.HTML)


# --------------------- 💰 إدارة النقاط ---------------------

async def adm_show_points_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    kb = InlineKeyboardMarkup(
        [
            [styled_button("➕ شحن نقاط", "adm:points:charge", "adm_points_charge", "➕ زر شحن نقاط", "success")],
            [styled_button("➖ خصم نقاط", "adm:points:deduct", "adm_points_deduct", "➖ زر خصم نقاط", "danger")],
            [styled_button("💳 معرفة رصيد مستخدم", "adm:points:balance", "adm_points_balance", "💳 زر معرفة رصيد مستخدم", "primary")],
            [styled_button("📜 سجل عمليات النقاط", "adm:points:log", "adm_points_log", "📜 زر سجل عمليات النقاط", "primary")],
            [back_btn("🔙 رجوع", "adm:menu")],
        ]
    )
    await query.answer()
    await query.edit_message_text(
        "💰 <b>إدارة النقاط</b>\nاختر العملية:", parse_mode=ParseMode.HTML, reply_markup=kb
    )


async def cb_points_charge_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_admin_perm_callback(update, ADMIN_PERM_POINTS):
        return
    query = update.callback_query
    set_flow(context, "adm_points_charge_target")
    await query.answer()
    await query.message.reply_text(
        "✏️ أرسل آيدي المستخدم أو Username لشحن النقاط له:", reply_markup=cancel_kb()
    )


async def adm_points_charge_target_input(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    q = update.message.text.strip()
    user = find_user_by_query(q)
    if not user:
        await update.message.reply_text("❌ لم يتم العثور على المستخدم. حاول مجددًا:", reply_markup=cancel_kb())
        return
    set_flow(context, "adm_points_charge_amount", target_id=user["user_id"])
    await update.message.reply_text(
        f"👤 المستخدم: {esc(user['first_name'])} (<code>{user['user_id']}</code>)\n"
        f"رصيده الحالي: {user['points']} نقطة\n\n"
        "✏️ أرسل عدد النقاط المراد شحنها:",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_kb(),
    )


async def adm_points_charge_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    text = update.message.text.strip()
    if not text.lstrip("-").isdigit() or int(text) <= 0:
        await update.message.reply_text("⚠️ الرجاء إرسال رقم صحيح موجب.", reply_markup=cancel_kb())
        return
    amount = int(text)
    target_id = flow["data"]["target_id"]

    conn = get_conn()
    conn.execute("BEGIN IMMEDIATE")
    user = conn.execute("SELECT * FROM users WHERE user_id=?", (target_id,)).fetchone()
    if not user:
        conn.execute("ROLLBACK")
        conn.close()
        clear_flow(context)
        await update.message.reply_text("❌ المستخدم غير موجود.")
        return
    conn.execute(
        "UPDATE users SET points = points + ?, total_points_earned = total_points_earned + ? WHERE user_id=?",
        (amount, amount, target_id),
    )
    new_balance = record_transaction(conn, target_id, amount, "admin_charge", "شحن نقاط من المالك")
    conn.commit()
    conn.close()

    clear_flow(context)
    await update.message.reply_text(
        f"✅ تم شحن {amount} نقطة للمستخدم <code>{target_id}</code>.\nرصيده الجديد: {new_balance} نقطة.",
        parse_mode=ParseMode.HTML,
    )
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=f"🎉 تم شحن {amount} نقطة لحسابك من الإدارة. رصيدك الحالي: {new_balance} نقطة.",
        )
    except (Forbidden, TelegramError):
        pass


async def cb_points_deduct_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_admin_perm_callback(update, ADMIN_PERM_POINTS):
        return
    query = update.callback_query
    set_flow(context, "adm_points_deduct_target")
    await query.answer()
    await query.message.reply_text(
        "✏️ أرسل آيدي المستخدم أو Username لخصم نقاط منه:", reply_markup=cancel_kb()
    )


async def adm_points_deduct_target_input(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    q = update.message.text.strip()
    user = find_user_by_query(q)
    if not user:
        await update.message.reply_text("❌ لم يتم العثور على المستخدم. حاول مجددًا:", reply_markup=cancel_kb())
        return
    set_flow(context, "adm_points_deduct_amount", target_id=user["user_id"])
    await update.message.reply_text(
        f"👤 المستخدم: {esc(user['first_name'])} (<code>{user['user_id']}</code>)\n"
        f"رصيده الحالي: {user['points']} نقطة\n\n"
        "✏️ أرسل عدد النقاط المراد خصمها:",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_kb(),
    )


async def adm_points_deduct_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    text = update.message.text.strip()
    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text("⚠️ الرجاء إرسال رقم صحيح موجب.", reply_markup=cancel_kb())
        return
    amount = int(text)
    target_id = flow["data"]["target_id"]

    conn = get_conn()
    conn.execute("BEGIN IMMEDIATE")
    user = conn.execute("SELECT * FROM users WHERE user_id=?", (target_id,)).fetchone()
    if not user:
        conn.execute("ROLLBACK")
        conn.close()
        clear_flow(context)
        await update.message.reply_text("❌ المستخدم غير موجود.")
        return
    new_points = max(0, user["points"] - amount)
    actual_deducted = user["points"] - new_points
    conn.execute("UPDATE users SET points = ? WHERE user_id=?", (new_points, target_id))
    new_balance = record_transaction(
        conn, target_id, -actual_deducted, "admin_deduct", "خصم نقاط من المالك"
    )
    conn.commit()
    conn.close()

    clear_flow(context)
    await update.message.reply_text(
        f"✅ تم خصم {actual_deducted} نقطة من المستخدم <code>{target_id}</code>.\n"
        f"رصيده الجديد: {new_balance} نقطة.",
        parse_mode=ParseMode.HTML,
    )
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=f"⚠️ تم خصم {actual_deducted} نقطة من حسابك من قِبل الإدارة. رصيدك الحالي: {new_balance} نقطة.",
        )
    except (Forbidden, TelegramError):
        pass


async def cb_points_balance_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_admin_perm_callback(update, ADMIN_PERM_POINTS):
        return
    query = update.callback_query
    set_flow(context, "adm_points_balance_target")
    await query.answer()
    await query.message.reply_text(
        "✏️ أرسل آيدي المستخدم أو Username لمعرفة رصيده:", reply_markup=cancel_kb()
    )


async def adm_points_balance_target_input(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    q = update.message.text.strip()
    clear_flow(context)
    user = find_user_by_query(q)
    if not user:
        await update.message.reply_text("❌ لم يتم العثور على المستخدم.")
        return
    await update.message.reply_text(
        f"💳 رصيد {esc(user['first_name'])} (<code>{user['user_id']}</code>): {user['points']} نقطة.",
        parse_mode=ParseMode.HTML,
    )


async def cb_points_log(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_admin_perm_callback(update, ADMIN_PERM_POINTS):
        return
    query = update.callback_query
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM transactions ORDER BY transaction_id DESC LIMIT 20"
    ).fetchall()
    conn.close()

    if not rows:
        await query.answer()
        await query.message.reply_text("لا توجد عمليات مسجلة بعد.")
        return

    lines = ["📜 <b>آخر 20 عملية نقاط</b>\n"]
    for r in rows:
        sign = "+" if r["amount"] >= 0 else ""
        lines.append(
            f"• <code>{r['user_id']}</code> | {sign}{r['amount']} | {esc(r['type'])} | {r['created_at']}"
        )
    await query.answer()
    await query.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# --------------------- 🔑 إدارة المنتجات ---------------------

async def adm_show_products_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, answer: bool = True) -> None:
    query = update.callback_query
    products = list_active_products()
    lines = ["🔑 <b>إدارة المنتجات</b>\n"]
    buttons = []
    if not products:
        lines.append("لا توجد منتجات بعد.")
    for p in products:
        available = count_available_keys(p["product_id"])
        sold = count_sold_keys(p["product_id"])
        lines.append(
            f"\n📦 <b>{esc(p['name'])}</b> | {esc(p['duration'])} | {p['price']} نقطة\n"
            f"متوفر: {available} | مباع: {sold}"
        )
        buttons.append(
            [styled_button(f"⚙️ إدارة: {p['name']}", f"adm:pr:manage:{p['product_id']}", "adm_pr_manage_row", "⚙️ زر إدارة منتج (في القائمة)", "primary")]
        )
    buttons.append([styled_button("➕ إضافة منتج جديد", "adm:pr:add", "adm_pr_add", "➕ زر إضافة منتج جديد", "success")])
    buttons.append([back_btn("🔙 رجوع", "adm:menu")])

    if answer:
        await query.answer()
    await query.edit_message_text(
        "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons)
    )


async def cb_product_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    set_flow(context, "adm_product_add_name")
    await query.answer()
    await query.message.reply_text("✏️ أرسل اسم المنتج الجديد:", reply_markup=cancel_kb())


async def adm_product_add_name_input(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("⚠️ الاسم لا يمكن أن يكون فارغًا.", reply_markup=cancel_kb())
        return
    set_flow(context, "adm_product_add_duration", product_name=name)
    await update.message.reply_text(
        "✏️ أرسل مدة صلاحية المفتاح (مثال: يوم / 3 أيام / أسبوع / شهر):", reply_markup=cancel_kb()
    )


async def adm_product_add_duration_input(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    duration = update.message.text.strip()
    if not duration:
        await update.message.reply_text("⚠️ المدة لا يمكن أن تكون فارغة.", reply_markup=cancel_kb())
        return
    flow["data"]["duration"] = duration
    set_flow(context, "adm_product_add_price", **flow["data"])
    await update.message.reply_text("✏️ أرسل سعر المنتج بالنقاط (رقم صحيح):", reply_markup=cancel_kb())


def color_choice_keyboard(prefix: str, back_callback: str) -> InlineKeyboardMarkup:
    """لوحة اختيار لون موحّدة (تُستخدم عند إضافة منتج، تعديل لون منتج،
    أو تخصيص لون أي زر عام). عند الضغط على أحد الخيارات تُرسَل
    callback_data بالشكل: <prefix>:<style>."""
    rows = [
        [
            styled_button_for_style(BUTTON_STYLE_CHOICES["primary"], f"{prefix}:primary", "primary"),
            styled_button_for_style(BUTTON_STYLE_CHOICES["success"], f"{prefix}:success", "success"),
        ],
        [
            styled_button_for_style(BUTTON_STYLE_CHOICES["danger"], f"{prefix}:danger", "danger"),
            styled_button_for_style(BUTTON_STYLE_CHOICES["none"], f"{prefix}:none", "none"),
        ],
        [back_btn("🔙 رجوع", back_callback)],
    ]
    return InlineKeyboardMarkup(rows)


async def adm_product_add_price_input(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    text = update.message.text.strip()
    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text("⚠️ الرجاء إرسال رقم صحيح موجب.", reply_markup=cancel_kb())
        return
    price = int(text)
    flow["data"]["price"] = price
    set_flow(context, "adm_product_add_color", **flow["data"])
    await update.message.reply_text(
        "🎨 اختر لون زر هذا المنتج كما سيظهر في المتجر:",
        reply_markup=color_choice_keyboard("adm:pr:addcolor", "flow:back"),
    )


async def cb_product_add_color(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يُستدعى بعد اختيار المالك للون زر المنتج الجديد؛ ينشئ المنتج فعليًا."""
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    flow = get_flow(context)
    if not flow or flow.get("name") != "adm_product_add_color":
        await query.answer("⚠️ انتهت صلاحية هذه الخطوة، ابدأ إضافة المنتج من جديد.", show_alert=True)
        return

    style = query.data.split(":")[3]
    name = flow["data"]["product_name"]
    duration = flow["data"]["duration"]
    price = flow["data"]["price"]

    conn = get_conn()
    conn.execute(
        "INSERT INTO products (name, duration, price, created_at, is_active, button_color) "
        "VALUES (?, ?, ?, ?, 1, ?)",
        (name, duration, price, now_str(), style),
    )
    conn.commit()
    conn.close()

    clear_flow(context)
    await query.answer("✅ تم إضافة المنتج بنجاح.")
    await query.edit_message_text(
        f"✅ تم إضافة المنتج «{esc(name)}» بسعر {price} نقطة ومدة {esc(duration)}، "
        f"بلون زر: {BUTTON_STYLE_CHOICES.get(style, style)}.\n"
        "لا تنسَ إضافة مفاتيح له من قسم «إدارة المفاتيح».",
        parse_mode=ParseMode.HTML,
    )


async def cb_product_manage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    product_id = int(query.data.split(":")[3])
    conn = get_conn()
    p = conn.execute("SELECT * FROM products WHERE product_id=?", (product_id,)).fetchone()
    conn.close()
    if not p:
        await query.answer("المنتج غير موجود.", show_alert=True)
        return

    available = count_available_keys(product_id)
    sold = count_sold_keys(product_id)
    color_label = BUTTON_STYLE_CHOICES.get(p["button_color"], p["button_color"])
    text = (
        f"📦 <b>{esc(p['name'])}</b>\n"
        f"⏳ المدة: {esc(p['duration'])}\n"
        f"💰 السعر: {p['price']} نقطة\n"
        f"🎨 لون زر المتجر: {color_label}\n"
        f"🔑 متوفر: {available} | مباع: {sold}"
    )
    kb = InlineKeyboardMarkup(
        [
            [styled_button("✏️ تعديل الاسم", f"adm:pr:ename:{product_id}", "adm_pr_ename", "✏️ زر تعديل اسم المنتج", "primary")],
            [styled_button("💰 تعديل السعر", f"adm:pr:eprice:{product_id}", "adm_pr_eprice", "💰 زر تعديل سعر المنتج", "primary")],
            [styled_button("⏳ تعديل المدة", f"adm:pr:edur:{product_id}", "adm_pr_edur", "⏳ زر تعديل مدة المنتج", "primary")],
            [styled_button("🎨 تغيير لون زر المتجر", f"adm:pr:ecolor:{product_id}", "adm_pr_ecolor", "🎨 زر تغيير لون زر المنتج", "primary")],
            [styled_button("🗑️ حذف المنتج", f"adm:pr:delc:{product_id}", "adm_pr_delc", "🗑️ زر حذف منتج", "danger")],
            [back_btn("🔙 رجوع", "adm:products")],
        ]
    )
    await query.answer()
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cb_product_edit_name_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    product_id = int(query.data.split(":")[3])
    set_flow(context, "adm_product_edit_name", product_id=product_id)
    await query.answer()
    await query.message.reply_text("✏️ أرسل الاسم الجديد للمنتج:", reply_markup=cancel_kb())


async def adm_product_edit_name_input(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    name = update.message.text.strip()
    product_id = flow["data"]["product_id"]
    conn = get_conn()
    conn.execute("UPDATE products SET name=? WHERE product_id=?", (name, product_id))
    conn.commit()
    conn.close()
    clear_flow(context)
    await update.message.reply_text(f"✅ تم تحديث اسم المنتج إلى «{esc(name)}».", parse_mode=ParseMode.HTML)


async def cb_product_edit_price_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    product_id = int(query.data.split(":")[3])
    set_flow(context, "adm_product_edit_price", product_id=product_id)
    await query.answer()
    await query.message.reply_text("✏️ أرسل السعر الجديد بالنقاط:", reply_markup=cancel_kb())


async def adm_product_edit_price_input(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    text = update.message.text.strip()
    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text("⚠️ الرجاء إرسال رقم صحيح موجب.", reply_markup=cancel_kb())
        return
    price = int(text)
    product_id = flow["data"]["product_id"]
    conn = get_conn()
    conn.execute("UPDATE products SET price=? WHERE product_id=?", (price, product_id))
    conn.commit()
    conn.close()
    clear_flow(context)
    await update.message.reply_text(f"✅ تم تحديث السعر إلى {price} نقطة.")


async def cb_product_edit_duration_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    product_id = int(query.data.split(":")[3])
    set_flow(context, "adm_product_edit_duration", product_id=product_id)
    await query.answer()
    await query.message.reply_text("✏️ أرسل مدة الصلاحية الجديدة:", reply_markup=cancel_kb())


async def adm_product_edit_duration_input(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    duration = update.message.text.strip()
    product_id = flow["data"]["product_id"]
    conn = get_conn()
    conn.execute("UPDATE products SET duration=? WHERE product_id=?", (duration, product_id))
    conn.commit()
    conn.close()
    clear_flow(context)
    await update.message.reply_text(f"✅ تم تحديث المدة إلى «{esc(duration)}».", parse_mode=ParseMode.HTML)


async def cb_product_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    product_id = int(query.data.split(":")[3])
    kb = InlineKeyboardMarkup(
        [
            [
                styled_button("✅ نعم، احذف", f"adm:pr:delok:{product_id}", "adm_pr_delok", "✅ زر تأكيد حذف المنتج", "danger"),
                styled_button("❌ إلغاء", f"adm:pr:manage:{product_id}", "adm_pr_delcancel", "❌ زر إلغاء حذف المنتج", "success"),
            ]
        ]
    )
    await query.answer()
    await query.edit_message_text(
        "⚠️ هل أنت متأكد من حذف هذا المنتج؟ سيتم إلغاء تفعيله (المفاتيح المباعة سابقًا تبقى مسجلة).",
        reply_markup=kb,
    )


async def cb_product_delete_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    product_id = int(query.data.split(":")[3])
    conn = get_conn()
    conn.execute("UPDATE products SET is_active=0 WHERE product_id=?", (product_id,))
    conn.commit()
    conn.close()
    await query.answer("تم حذف المنتج.", show_alert=True)
    await adm_show_products_menu(update, context, answer=False)


# --------------------- 🎫 إدارة المفاتيح ---------------------

async def adm_show_keys_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    products = list_active_products()
    buttons = []
    for p in products:
        available = count_available_keys(p["product_id"])
        sold = count_sold_keys(p["product_id"])
        buttons.append(
            [
                styled_button(
                    f"{p['name']} (متوفر {available} / مباع {sold})",
                    f"adm:ky:menu:{p['product_id']}",
                    "adm_ky_menu_row",
                    "🎫 زر اختيار منتج لإدارة مفاتيحه",
                    "primary",
                )
            ]
        )
    buttons.append([back_btn("🔙 رجوع", "adm:menu")])
    text = "🎫 <b>إدارة المفاتيح</b>\nاختر منتجًا لإدارة مفاتيحه:" if products else \
        "🎫 لا توجد منتجات بعد. أضف منتجًا أولًا من «إدارة المنتجات»."
    await query.answer()
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))


async def cb_keys_product_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    product_id = int(query.data.split(":")[3])
    conn = get_conn()
    p = conn.execute("SELECT * FROM products WHERE product_id=?", (product_id,)).fetchone()
    conn.close()
    if not p:
        await query.answer("المنتج غير موجود.", show_alert=True)
        return

    available = count_available_keys(product_id)
    sold = count_sold_keys(product_id)
    text = f"🎫 <b>مفاتيح: {esc(p['name'])}</b>\nمتوفر: {available} | مباع: {sold}"
    kb = InlineKeyboardMarkup(
        [
            [styled_button("➕ إضافة مفاتيح (دفعة)", f"adm:ky:add:{product_id}", "adm_ky_add", "➕ زر إضافة مفاتيح", "success")],
            [styled_button("📄 عرض المفاتيح المتاحة", f"adm:ky:avail:{product_id}", "adm_ky_avail", "📄 زر عرض المفاتيح المتاحة", "primary")],
            [styled_button("📄 عرض المفاتيح المباعة", f"adm:ky:sold:{product_id}", "adm_ky_sold", "📄 زر عرض المفاتيح المباعة", "primary")],
            [back_btn("🔙 رجوع", "adm:keys")],
        ]
    )
    await query.answer()
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cb_keys_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    product_id = int(query.data.split(":")[3])
    set_flow(context, "adm_keys_add_bulk", product_id=product_id)
    await query.answer()
    await query.message.reply_text(
        "✏️ أرسل المفاتيح، مفتاح واحد في كل سطر، مثال:\n\n"
        "<code>KEY-001\nKEY-002\nKEY-003</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_kb(),
    )


async def adm_keys_add_bulk_input(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    product_id = flow["data"]["product_id"]
    raw_lines = [line.strip() for line in update.message.text.splitlines()]
    key_values = [line for line in raw_lines if line]

    if not key_values:
        await update.message.reply_text("⚠️ لم يتم العثور على أي مفتاح صالح. حاول مجددًا:", reply_markup=cancel_kb())
        return

    conn = get_conn()
    added, duplicated = 0, 0
    for kv in key_values:
        try:
            conn.execute(
                "INSERT INTO keys_store (product_id, key_value, is_sold, added_at) VALUES (?, ?, 0, ?)",
                (product_id, kv, now_str()),
            )
            added += 1
        except sqlite3.IntegrityError:
            duplicated += 1
    conn.commit()
    conn.close()

    clear_flow(context)
    msg = f"✅ تمت إضافة {added} مفتاح بنجاح."
    if duplicated:
        msg += f"\n⚠️ تم تجاهل {duplicated} مفتاح مكرر (موجود مسبقًا)."
    await update.message.reply_text(msg)


async def cb_keys_available_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    product_id = int(query.data.split(":")[3])
    conn = get_conn()
    rows = conn.execute(
        "SELECT key_value FROM keys_store WHERE product_id=? AND is_sold=0 ORDER BY key_id LIMIT 50",
        (product_id,),
    ).fetchall()
    total = conn.execute(
        "SELECT COUNT(*) c FROM keys_store WHERE product_id=? AND is_sold=0", (product_id,)
    ).fetchone()["c"]
    conn.close()

    if not rows:
        await query.answer()
        await query.message.reply_text("لا توجد مفاتيح متاحة حاليًا لهذا المنتج.")
        return

    text = "📄 <b>المفاتيح المتاحة</b> (أول 50):\n\n" + "\n".join(
        f"<code>{esc(r['key_value'])}</code>" for r in rows
    )
    text += f"\n\nالإجمالي: {total} مفتاح متاح."
    await query.answer()
    await query.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cb_keys_sold_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    product_id = int(query.data.split(":")[3])
    conn = get_conn()
    rows = conn.execute(
        "SELECT key_value, sold_to, sold_at FROM keys_store WHERE product_id=? AND is_sold=1 "
        "ORDER BY sold_at DESC LIMIT 50",
        (product_id,),
    ).fetchall()
    total = conn.execute(
        "SELECT COUNT(*) c FROM keys_store WHERE product_id=? AND is_sold=1", (product_id,)
    ).fetchone()["c"]
    conn.close()

    if not rows:
        await query.answer()
        await query.message.reply_text("لا توجد مفاتيح مباعة بعد لهذا المنتج.")
        return

    lines = ["📄 <b>المفاتيح المباعة</b> (أحدث 50):\n"]
    for r in rows:
        lines.append(
            f"<code>{esc(r['key_value'])}</code> ← <code>{r['sold_to']}</code> | {r['sold_at']}"
        )
    lines.append(f"\nالإجمالي: {total} مفتاح مباع.")
    await query.answer()
    await query.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# --------------------- 🎁 روابط الهدايا ---------------------

async def adm_show_gift_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM gift_links ORDER BY gift_id DESC LIMIT 10"
    ).fetchall()
    conn.close()

    lines = ["🎁 <b>روابط الهدايا</b>\n"]
    if not rows:
        lines.append("لا توجد روابط هدايا بعد.")
    for g in rows:
        status = "نشط ✅" if g["is_active"] and g["used_count"] < g["max_uses"] else "منتهي ❌"
        expiry = g["expires_at"] or "بدون انتهاء"
        lines.append(
            f"\n🔸 <code>{esc(g['code'])}</code> | {g['points']} نقطة | "
            f"استخدامات: {g['used_count']}/{g['max_uses']} | انتهاء: {expiry} | {status}"
        )
    kb = InlineKeyboardMarkup(
        [
            [styled_button("➕ إنشاء رابط هدية", "adm:gf:create", "adm_gf_create", "➕ زر إنشاء رابط هدية", "success")],
            [back_btn("🔙 رجوع", "adm:menu")],
        ]
    )
    await query.answer()
    await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=kb)


async def cb_gift_create_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_admin_perm_callback(update, ADMIN_PERM_GIFT):
        return
    query = update.callback_query
    set_flow(context, "adm_gift_points")
    await query.answer()
    await query.message.reply_text("✏️ أرسل عدد النقاط التي يمنحها الرابط:", reply_markup=cancel_kb())


async def adm_gift_points_input(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    text = update.message.text.strip()
    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text("⚠️ الرجاء إرسال رقم صحيح موجب.", reply_markup=cancel_kb())
        return
    set_flow(context, "adm_gift_uses", points=int(text))
    await update.message.reply_text("✏️ أرسل عدد الأشخاص المسموح لهم باستخدام الرابط:", reply_markup=cancel_kb())


async def adm_gift_uses_input(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    text = update.message.text.strip()
    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text("⚠️ الرجاء إرسال رقم صحيح موجب.", reply_markup=cancel_kb())
        return
    flow["data"]["max_uses"] = int(text)
    set_flow(context, "adm_gift_expiry", **flow["data"])
    await update.message.reply_text(
        "✏️ أرسل مدة صلاحية الرابط بالأيام (مثال: 7)، أو أرسل «-» لرابط بدون انتهاء:",
        reply_markup=cancel_kb(),
    )


async def adm_gift_expiry_input(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    text = update.message.text.strip()
    expires_at = None
    if text != "-":
        if not text.isdigit() or int(text) <= 0:
            await update.message.reply_text(
                "⚠️ أرسل رقم أيام صحيح أو «-» بدون انتهاء.", reply_markup=cancel_kb()
            )
            return
        expires_at = (datetime.utcnow() + timedelta(days=int(text))).strftime("%Y-%m-%d %H:%M:%S")

    points = flow["data"]["points"]
    max_uses = flow["data"]["max_uses"]
    code = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))

    conn = get_conn()
    conn.execute(
        """INSERT INTO gift_links (code, points, max_uses, used_count, expires_at, created_at, created_by, is_active)
           VALUES (?, ?, ?, 0, ?, ?, ?, 1)""",
        (code, points, max_uses, expires_at, now_str(), update.effective_user.id),
    )
    conn.commit()
    conn.close()

    clear_flow(context)
    link = f"https://t.me/{BOT_USERNAME}?start=gift_{code}"
    await update.message.reply_text(
        "✅ <b>تم إنشاء رابط الهدية</b>\n\n"
        f"🔗 {link}\n"
        f"💰 النقاط: {points}\n"
        f"👥 الحد الأقصى للاستخدام: {max_uses}\n"
        f"⏳ الانتهاء: {expires_at or 'بدون انتهاء'}",
        parse_mode=ParseMode.HTML,
    )


async def _apply_gift_code(user_id: int, code: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        gift = conn.execute(
            "SELECT * FROM gift_links WHERE code=?", (code,)
        ).fetchone()
        if not gift or not gift["is_active"]:
            conn.execute("ROLLBACK")
            return
        if gift["expires_at"]:
            if datetime.utcnow() > datetime.strptime(gift["expires_at"], "%Y-%m-%d %H:%M:%S"):
                conn.execute("ROLLBACK")
                return
        if gift["used_count"] >= gift["max_uses"]:
            conn.execute("ROLLBACK")
            return

        already = conn.execute(
            "SELECT 1 FROM gift_usage WHERE gift_id=? AND user_id=?",
            (gift["gift_id"], user_id),
        ).fetchone()
        if already:
            conn.execute("ROLLBACK")
            return

        conn.execute(
            "INSERT INTO gift_usage (gift_id, user_id, used_at) VALUES (?, ?, ?)",
            (gift["gift_id"], user_id, now_str()),
        )
        conn.execute(
            "UPDATE gift_links SET used_count = used_count + 1 WHERE gift_id=?",
            (gift["gift_id"],),
        )
        conn.execute(
            "UPDATE users SET points = points + ?, total_points_earned = total_points_earned + ? WHERE user_id=?",
            (gift["points"], gift["points"], user_id),
        )
        record_transaction(conn, user_id, gift["points"], "gift_link", f"استخدام رابط هدية {code}")
        conn.commit()

        try:
            await context.bot.send_message(
                chat_id=user_id, text=f"🎁 حصلت على {gift['points']} نقطة من رابط الهدية!"
            )
        except (Forbidden, TelegramError):
            pass
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --------------------- 📢 الإذاعة (Broadcast) ---------------------

async def adm_show_broadcast_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """قسم الإذاعة: يعرض خيارين — إرسال للجميع (الإذاعة الاعتيادية)، أو
    إرسال رسالة لشخص واحد فقط."""
    query = update.callback_query
    kb = InlineKeyboardMarkup(
        [
            [styled_button("📢 إرسال للجميع", "adm:bc:all", "adm_bc_all", "📢 زر إرسال إذاعة للجميع", "primary")],
            [styled_button("👤 إرسال رسالة لشخص واحد", "adm:bc:one", "adm_bc_one", "👤 زر إرسال رسالة لشخص واحد", "primary")],
            [back_btn("🔙 رجوع", "adm:menu")],
        ]
    )
    await query.answer()
    await query.edit_message_text(
        "📢 <b>الإذاعة</b>\nاختر نوع الإرسال:",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


async def cb_broadcast_all_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_admin_perm_callback(update, ADMIN_PERM_BROADCAST):
        return
    query = update.callback_query
    set_flow(context, "adm_broadcast_content")
    kb = InlineKeyboardMarkup([[back_btn("🔙 رجوع", "flow:back")]])
    await query.answer()
    await query.edit_message_text(
        "📢 <b>إرسال للجميع</b>\n\n"
        "أرسل الآن الرسالة التي تريد إذاعتها لجميع المستخدمين.\n"
        "يمكن أن تكون نصًا، صورة، فيديو، أو ملفًا (مع تسمية توضيحية اختيارية).",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


async def adm_broadcast_content_input(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    msg = update.message
    context.user_data["broadcast_message_id"] = msg.message_id
    context.user_data["broadcast_chat_id"] = msg.chat_id
    clear_flow(context)

    kb = InlineKeyboardMarkup(
        [
            [
                styled_button("✅ إرسال للجميع", "adm:bc:send", "adm_bc_send", "✅ زر إرسال الإذاعة للجميع", "success"),
                styled_button("❌ إلغاء", "adm:bc:cancel", "adm_bc_cancel", "❌ زر إلغاء الإذاعة", "danger"),
            ]
        ]
    )
    await msg.reply_text("👆 هذه هي الرسالة التي ستُرسل لكل المستخدمين. تأكيد الإرسال؟", reply_markup=kb)


async def cb_broadcast_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_admin_perm_callback(update, ADMIN_PERM_BROADCAST):
        return
    query = update.callback_query
    context.user_data.pop("broadcast_message_id", None)
    context.user_data.pop("broadcast_chat_id", None)
    await query.answer("تم الإلغاء.")
    await query.edit_message_text("❌ تم إلغاء الإذاعة.")


async def cb_broadcast_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_admin_perm_callback(update, ADMIN_PERM_BROADCAST):
        return
    query = update.callback_query
    msg_id = context.user_data.pop("broadcast_message_id", None)
    chat_id = context.user_data.pop("broadcast_chat_id", None)
    if not msg_id:
        await query.answer("انتهت صلاحية الرسالة، أعد المحاولة.", show_alert=True)
        return

    await query.answer("🚀 جارٍ الإرسال...")
    await query.edit_message_text("🚀 جارٍ إرسال الإذاعة، سيصلك تقرير عند الانتهاء...")

    conn = get_conn()
    user_ids = [r["user_id"] for r in conn.execute("SELECT user_id FROM users").fetchall()]
    conn.close()

    success, failed = 0, 0
    for uid in user_ids:
        try:
            await context.bot.copy_message(chat_id=uid, from_chat_id=chat_id, message_id=msg_id)
            success += 1
        except (Forbidden, BadRequest):
            failed += 1
        except TelegramError as e:
            logger.warning("فشل إرسال الإذاعة للمستخدم %s: %s", uid, e)
            failed += 1
        await asyncio.sleep(0.05)  # تجنب تجاوز حدود Telegram للسرعة

    report = (
        "📊 <b>تقرير الإذاعة</b>\n\n"
        f"✅ نجح الإرسال إلى: {success}\n"
        f"❌ فشل الإرسال إلى: {failed}\n"
        f"👥 إجمالي المستخدمين: {len(user_ids)}"
    )
    await context.bot.send_message(chat_id=OWNER_ID, text=report, parse_mode=ParseMode.HTML)


# --------------------- 👤 إرسال رسالة لشخص واحد ---------------------

async def cb_broadcast_one_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_admin_perm_callback(update, ADMIN_PERM_BROADCAST):
        return
    query = update.callback_query
    set_flow(context, "adm_bc_one_target")
    await query.answer()
    await query.edit_message_text(
        "👤 <b>إرسال رسالة لشخص واحد</b>\n\n"
        "✏️ أرسل يوزر (Username) أو آيدي تيليجرام للشخص الذي تريد إرسال "
        "الرسالة إليه:",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_kb(),
    )


async def adm_bc_one_target_input(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    q = update.message.text.strip()
    user = find_user_by_query(q)
    if not user:
        await update.message.reply_text(
            "❌ لم يتم العثور على المستخدم. حاول مجددًا:", reply_markup=cancel_kb()
        )
        return
    set_flow(context, "adm_bc_one_content", target_id=user["user_id"])
    await update.message.reply_text(
        f"👤 المستخدم: {esc(user['first_name'])} (<code>{user['user_id']}</code>)\n\n"
        "✏️ الآن اكتب نص الرسالة التي تريد إرسالها إليه:",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_kb(),
    )


async def adm_bc_one_content_input(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("⚠️ الرجاء إرسال نص رسالة غير فارغ.", reply_markup=cancel_kb())
        return
    target_id = flow["data"]["target_id"]
    clear_flow(context)

    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=f"📩 <b>رسالة من الإدارة:</b>\n\n{esc(text)}",
            parse_mode=ParseMode.HTML,
        )
    except (Forbidden, BadRequest) as e:
        await update.message.reply_text(
            f"❌ تعذر إرسال الرسالة إلى <code>{target_id}</code> (ربما حظر البوت). "
            f"سبب الخطأ: {esc(str(e))}",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_menu_keyboard(),
        )
        return
    except TelegramError as e:
        logger.warning("فشل إرسال رسالة لشخص واحد إلى %s: %s", target_id, e)
        await update.message.reply_text(
            f"❌ حدث خطأ أثناء إرسال الرسالة: {esc(str(e))}",
            reply_markup=admin_menu_keyboard(),
        )
        return

    await update.message.reply_text(
        f"✅ تم إرسال الرسالة بنجاح إلى <code>{target_id}</code>.",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_menu_keyboard(),
    )


# --------------------- 🚫 الحظر ---------------------

async def adm_show_ban_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    conn = get_conn()
    banned_count = conn.execute("SELECT COUNT(*) c FROM users WHERE is_banned=1").fetchone()["c"]
    conn.close()
    kb = InlineKeyboardMarkup(
        [
            [styled_button("🚫 حظر مستخدم", "adm:bn:ban", "adm_bn_ban", "🚫 زر حظر مستخدم", "danger")],
            [styled_button("✅ فك حظر مستخدم", "adm:bn:unban", "adm_bn_unban", "✅ زر فك حظر مستخدم", "success")],
            [styled_button("📄 قائمة المحظورين", "adm:bn:list", "adm_bn_list", "📄 زر قائمة المحظورين", "primary")],
            [back_btn("🔙 رجوع", "adm:menu")],
        ]
    )
    await query.answer()
    await query.edit_message_text(
        f"🚫 <b>إدارة الحظر</b>\nعدد المحظورين حاليًا: {banned_count}",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


async def cb_ban_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_admin_perm_callback(update, ADMIN_PERM_BAN):
        return
    query = update.callback_query
    set_flow(context, "adm_ban_target")
    await query.answer()
    await query.message.reply_text("✏️ أرسل آيدي المستخدم أو Username لحظره:", reply_markup=cancel_kb())


async def adm_ban_target_input(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    q = update.message.text.strip()
    user = find_user_by_query(q)
    clear_flow(context)
    if not user:
        await update.message.reply_text("❌ لم يتم العثور على المستخدم.")
        return
    if user["user_id"] == OWNER_ID:
        await update.message.reply_text("⛔ لا يمكن حظر المالك.")
        return

    conn = get_conn()
    conn.execute("UPDATE users SET is_banned=1 WHERE user_id=?", (user["user_id"],))
    conn.execute(
        "INSERT INTO banned_users (user_id, banned_at, reason) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET banned_at=excluded.banned_at, unbanned_at=NULL",
        (user["user_id"], now_str(), "حظر يدوي من المالك"),
    )
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"🚫 تم حظر المستخدم {esc(user['first_name'])} (<code>{user['user_id']}</code>).",
        parse_mode=ParseMode.HTML,
    )


async def cb_unban_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_admin_perm_callback(update, ADMIN_PERM_BAN):
        return
    query = update.callback_query
    set_flow(context, "adm_unban_target")
    await query.answer()
    await query.message.reply_text("✏️ أرسل آيدي المستخدم أو Username لفك حظره:", reply_markup=cancel_kb())


async def adm_unban_target_input(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    q = update.message.text.strip()
    user = find_user_by_query(q)
    clear_flow(context)
    if not user:
        await update.message.reply_text("❌ لم يتم العثور على المستخدم.")
        return

    conn = get_conn()
    conn.execute("UPDATE users SET is_banned=0 WHERE user_id=?", (user["user_id"],))
    conn.execute(
        "UPDATE banned_users SET unbanned_at=? WHERE user_id=?", (now_str(), user["user_id"])
    )
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"✅ تم فك الحظر عن {esc(user['first_name'])} (<code>{user['user_id']}</code>).",
        parse_mode=ParseMode.HTML,
    )


async def cb_ban_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_admin_perm_callback(update, ADMIN_PERM_BAN):
        return
    query = update.callback_query
    conn = get_conn()
    rows = conn.execute(
        "SELECT user_id, username, first_name FROM users WHERE is_banned=1"
    ).fetchall()
    conn.close()

    if not rows:
        await query.answer()
        await query.message.reply_text("لا يوجد مستخدمون محظورون حاليًا.")
        return

    lines = ["🚫 <b>قائمة المحظورين</b>\n"]
    for r in rows:
        uname = f"@{r['username']}" if r["username"] else "—"
        lines.append(f"• {esc(r['first_name'])} ({esc(uname)}) — <code>{r['user_id']}</code>")
    await query.answer()
    await query.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# --------------------- 📊 الإحصائيات ---------------------

async def adm_show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    conn = get_conn()
    total_users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    total_products = conn.execute("SELECT COUNT(*) c FROM products WHERE is_active=1").fetchone()["c"]
    total_keys_available = conn.execute("SELECT COUNT(*) c FROM keys_store WHERE is_sold=0").fetchone()["c"]
    total_keys_sold = conn.execute("SELECT COUNT(*) c FROM keys_store WHERE is_sold=1").fetchone()["c"]
    total_purchases = conn.execute("SELECT COUNT(*) c FROM purchases").fetchone()["c"]
    total_points_circulating = conn.execute("SELECT COALESCE(SUM(points),0) s FROM users").fetchone()["s"]
    total_referrals = conn.execute("SELECT COUNT(*) c FROM referrals").fetchone()["c"]
    pending_video = conn.execute(
        "SELECT COUNT(*) c FROM video_requests WHERE status='pending'"
    ).fetchone()["c"]
    active_gifts = conn.execute(
        "SELECT COUNT(*) c FROM gift_links WHERE is_active=1 AND used_count < max_uses"
    ).fetchone()["c"]
    banned = conn.execute("SELECT COUNT(*) c FROM users WHERE is_banned=1").fetchone()["c"]
    conn.close()

    text = (
        "📊 <b>إحصائيات البوت</b>\n\n"
        f"👥 إجمالي المستخدمين: {total_users}\n"
        f"🚫 المحظورون: {banned}\n"
        f"📦 المنتجات الفعالة: {total_products}\n"
        f"🔑 مفاتيح متاحة: {total_keys_available}\n"
        f"🔑 مفاتيح مباعة: {total_keys_sold}\n"
        f"🛍️ إجمالي المشتريات: {total_purchases}\n"
        f"💰 إجمالي النقاط المتداولة: {total_points_circulating}\n"
        f"🔗 إجمالي الإحالات: {total_referrals}\n"
        f"🎁 روابط هدايا نشطة: {active_gifts}\n"
        f"🎥 طلبات فيديو قيد الانتظار: {pending_video}\n"
    )
    kb = back_button("adm:menu")
    await query.answer()
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


# --------------------- 🎥 طلبات الفيديو (قائمة المالك) ---------------------

async def adm_show_video_requests(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM video_requests WHERE status='pending' ORDER BY request_id DESC LIMIT 10"
    ).fetchall()
    conn.close()

    if not rows:
        await query.answer()
        await query.edit_message_text(
            "🎥 لا توجد طلبات فيديو قيد الانتظار حاليًا.", reply_markup=back_button("adm:menu")
        )
        return

    await query.answer()
    await query.edit_message_text(
        f"🎥 يوجد {len(rows)} طلب قيد الانتظار (سيتم إرسال كل طلب برسالة منفصلة):",
        reply_markup=back_button("adm:menu"),
    )
    for req in rows:
        text = (
            "🎥 <b>طلب مراجعة فيديو</b>\n\n"
            f"🆔 آيدي: <code>{req['user_id']}</code>\n"
            f"🔗 الرابط: {esc(req['video_link'])}\n"
            f"📅 التاريخ: {req['requested_at']}"
        )
        kb = video_request_kb(req["request_id"])
        await context.bot.send_message(
            chat_id=update.effective_user.id, text=text, parse_mode=ParseMode.HTML, reply_markup=kb
        )


# --------------------- 🎨 ألوان الأزرار ---------------------

COLORS_PER_PAGE = 8


async def adm_show_colors_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0, answer: bool = True) -> None:
    """يعرض قائمة بكل زر مسجَّل في النظام (حاليًا ومستقبلًا) مع لونه
    الحالي، ويتيح للمالك اختيار أي زر لتغيير لونه بشكل مستقل تمامًا."""
    query = update.callback_query
    entries = list_button_colors()
    total = len(entries)
    total_pages = max(1, (total + COLORS_PER_PAGE - 1) // COLORS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    context.user_data["colors_page"] = page

    start = page * COLORS_PER_PAGE
    page_entries = entries[start : start + COLORS_PER_PAGE]

    rows = []
    for color_key, info in page_entries:
        emoji = {"primary": "🔵", "success": "🟢", "danger": "🔴", "none": "⚪"}.get(info["style"], "⚪")
        rows.append(
            [
                InlineKeyboardButton(
                    f"{emoji} {info['label']}",
                    callback_data=f"adm:clr:pick:{color_key}",
                )
            ]
        )

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ السابق", callback_data=f"adm:clr:page:{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("التالي ▶️", callback_data=f"adm:clr:page:{page + 1}"))
    if nav_row:
        rows.append(nav_row)

    rows.append([back_btn("🔙 رجوع", "adm:menu")])

    text = (
        "🎨 <b>ألوان الأزرار</b>\n\n"
        f"يوجد {total} زر مسجَّل في النظام (يشمل كل الأزرار الحالية، وأي زر "
        "جديد سيُضاف تلقائيًا لهذه القائمة أول مرة يظهر فيها).\n"
        f"اضغط على أي زر لتغيير لونه بشكل مستقل — الصفحة {page + 1}/{total_pages}:"
    )
    if answer:
        await query.answer()
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))


async def cb_colors_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_owner_callback(update):
        return
    page = int(update.callback_query.data.split(":")[3])
    await adm_show_colors_menu(update, context, page=page)


async def cb_colors_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعرض خيارات الألوان الأربعة لزر واحد بعينه."""
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    color_key = query.data.split(":", 3)[3]
    entry = _BUTTON_COLOR_REGISTRY.get(color_key)
    if not entry:
        await query.answer("⚠️ هذا الزر غير مسجَّل حاليًا.", show_alert=True)
        return

    current = BUTTON_STYLE_CHOICES.get(entry["style"], entry["style"])
    text = (
        f"🎨 <b>{esc(entry['label'])}</b>\n\n"
        f"اللون الحالي: {current}\n"
        "اختر اللون الجديد لهذا الزر فقط (لن يتأثر أي زر آخر):"
    )
    await query.answer()
    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=color_choice_keyboard(f"adm:clr:set:{color_key}", "adm:colors"),
    )


async def cb_colors_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يطبّق اللون الجديد على زر واحد بعينه ويعيد المالك لقائمة الألوان."""
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    _, _, _, color_key, style = query.data.split(":", 4)
    if color_key not in _BUTTON_COLOR_REGISTRY:
        await query.answer("⚠️ هذا الزر غير مسجَّل حاليًا.", show_alert=True)
        return
    set_button_color(color_key, style)
    await query.answer("✅ تم تغيير اللون.")
    page = context.user_data.get("colors_page", 0)
    await adm_show_colors_menu(update, context, page=page, answer=False)


# --------------------- 🎨 تغيير الإيموجي (Custom Emoji) ---------------------

EMOJI_PER_PAGE = 8


def _emoji_menu_keyboard(page: int, search: str = "") -> tuple:
    entries = list_custom_emoji_candidates()
    query = (search or "").strip().casefold()
    if query:
        entries = [e for e in entries if query in e[0].casefold() or query in e[1].casefold()]
    total = len(entries)
    total_pages = max(1, (total + EMOJI_PER_PAGE - 1) // EMOJI_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    page_entries = entries[page * EMOJI_PER_PAGE:(page + 1) * EMOJI_PER_PAGE]
    rows = []
    for color_key, label, default_emoji in page_entries:
        current = "⭐️" if color_key in _CUSTOM_EMOJI_REGISTRY else default_emoji
        # Separate, compact rows: the item label is never mixed with action text.
        rows.append([InlineKeyboardButton(f"{current} {label}", callback_data=f"adm:emj:pick:{color_key}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ السابق", callback_data=f"adm:emj:page:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("التالي ▶️", callback_data=f"adm:emj:page:{page + 1}"))
    if nav: rows.append(nav)
    rows.append([styled_button("🔎 بحث عن زر أو نص", "adm:emj:search", "adm_emj_search", "🔎 بحث في أزرار الإيموجي", "primary")])
    if search:
        rows.append([InlineKeyboardButton("✖️ مسح البحث", callback_data="adm:emj:clear")])
    rows.append([back_btn("🔙 رجوع", "adm:menu")])
    return rows, total, page, total_pages


async def adm_show_emoji_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0, answer: bool = True) -> None:
    query = update.callback_query
    search = context.user_data.get("emoji_search", "")
    rows, total, page, total_pages = _emoji_menu_keyboard(page, search)
    context.user_data["emoji_page"] = page
    title = "🎨 <b>تغيير الإيموجي</b>\n\n"
    title += f"العناصر المطابقة: <b>{total}</b>\n"
    if search: title += f"🔎 البحث: <code>{esc(search)}</code>\n"
    title += "اختر عنصرًا لتغيير الإيموجي، أو استخدم البحث للوصول السريع.\n"
    title += f"الصفحة {page + 1}/{total_pages}"
    if answer: await query.answer()
    await query.edit_message_text(title, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))


async def cb_emoji_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_owner_callback(update): return
    await adm_show_emoji_menu(update, context, page=int(update.callback_query.data.split(":")[3]))


async def cb_emoji_search_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_owner_callback(update): return
    set_flow(context, "adm_emoji_search")
    await update.callback_query.answer()
    await update.callback_query.message.reply_text("✏️ أرسل جزءًا من اسم الزر أو النص للبحث:", reply_markup=cancel_kb())


async def adm_emoji_search_input(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    search = (update.message.text or "").strip()[:80]
    clear_flow(context)
    context.user_data["emoji_search"] = search
    await update.message.reply_text("✅ تم تطبيق البحث.")
    # The next callback interaction renders the filtered list; start from page 1.
    await update.message.reply_text("اختر العنصر المطلوب من النتائج:", reply_markup=InlineKeyboardMarkup(_emoji_menu_keyboard(0, search)[0]))


async def cb_emoji_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_owner_callback(update): return
    context.user_data.pop("emoji_search", None)
    await adm_show_emoji_menu(update, context, page=0)


async def cb_emoji_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_owner_callback(update): return
    query = update.callback_query
    color_key = query.data.split(":", 3)[3]
    entry = _BUTTON_COLOR_REGISTRY.get(color_key)
    if not entry:
        await query.answer("⚠️ هذا العنصر غير مسجَّل حاليًا.", show_alert=True); return
    default_emoji = extract_leading_emoji(entry["label"])
    custom_id = _CUSTOM_EMOJI_REGISTRY.get(color_key)
    position = _CUSTOM_EMOJI_POSITION.get(color_key, "left")
    set_flow(context, "adm_emoji_set_custom", color_key=color_key, default_emoji=default_emoji, position=position)
    status = "مخصص حاليًا" if custom_id else f"الافتراضي الحالي: {default_emoji}"
    kb = [[InlineKeyboardButton("⬅️ يسار النص", callback_data=f"adm:emj:pos:{color_key}:left"), InlineKeyboardButton("➡️ يمين النص", callback_data=f"adm:emj:pos:{color_key}:right")]]
    if custom_id: kb.append([styled_button("♻️ إعادة الإيموجي للوضع الافتراضي", f"adm:emj:reset:{color_key}", "adm_emj_reset", "♻️ إعادة الإيموجي", "danger")])
    kb.append([back_btn("🔙 رجوع", "adm:emoji")])
    await query.answer()
    await query.edit_message_text(f"🎨 <b>{esc(entry['label'])}</b>\n\n{status}\n\nاختر موضع الإيموجي ثم أرسل الإيموجي المميز الجديد:", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))


async def cb_emoji_position(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_owner_callback(update): return
    _, _, _, color_key, position = update.callback_query.data.split(":", 4)
    if color_key not in _BUTTON_COLOR_REGISTRY:
        await update.callback_query.answer("⚠️ العنصر غير موجود.", show_alert=True); return
    context.user_data.setdefault("flow", {}).setdefault("data", {})["position"] = position
    if color_key in _CUSTOM_EMOJI_REGISTRY: set_custom_emoji_position(color_key, position)
    await update.callback_query.answer("✅ تم حفظ الموضع. أرسل الإيموجي المميز الآن.")


async def cb_emoji_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يلغي تخصيص الإيموجي المميز لزر معيّن ويعيده لإيموجيه الافتراضي."""
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    color_key = query.data.split(":", 3)[3]
    if color_key not in _BUTTON_COLOR_REGISTRY:
        await query.answer("⚠️ هذا العنصر غير مسجَّل حاليًا.", show_alert=True)
        return
    clear_custom_emoji_override(color_key)
    await query.answer("✅ تم إرجاع الإيموجي الافتراضي.")
    page = context.user_data.get("emoji_page", 0)
    await adm_show_emoji_menu(update, context, page=page, answer=False)


async def adm_emoji_set_custom_input(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    """يستقبل رسالة المالك المفترَض أنها تحتوي إيموجي مميزًا (Custom
    Emoji)، يستخرج معرّفه تلقائيًا من كيانات الرسالة (message entities)
    دون أي إدخال يدوي، ثم يحفظه بشكل دائم مرتبطًا بالزر المحدد."""
    message = update.message
    color_key = flow["data"].get("color_key")
    default_emoji = flow["data"].get("default_emoji", "")

    if not message or not message.text:
        await reply_tracked(
            context, message,
            "⚠️ يرجى إرسال إيموجي مميز (Custom Emoji) كرسالة نصية.",
            parse_mode=ParseMode.HTML,
            reply_markup=cancel_kb(),
        )
        return

    custom_id = None
    for entity in message.entities or []:
        if entity.type == MessageEntity.CUSTOM_EMOJI:
            custom_id = entity.custom_emoji_id
            break

    if not custom_id:
        await reply_tracked(
            context, message,
            "⚠️ هذا ليس إيموجي مميزًا (Custom Emoji).\n"
            "يرجى إرسال إيموجي <b>مميز</b> فعلي من تيليجرام (وليس إيموجي عاديًا) "
            "حتى يتمكن البوت من استخراج معرّفه تلقائيًا.",
            parse_mode=ParseMode.HTML,
            reply_markup=cancel_kb(),
        )
        return  # يبقى التدفق مفتوحًا للسماح بالمحاولة مجددًا

    entry = _BUTTON_COLOR_REGISTRY.get(color_key)
    if not entry:
        clear_flow(context)
        await reply_tracked(context, message, "⚠️ حدث خطأ: هذا العنصر لم يعد مسجَّلاً.")
        return

    position = flow["data"].get("position", _CUSTOM_EMOJI_POSITION.get(color_key, "left"))
    set_custom_emoji_override(color_key, custom_id, default_emoji or extract_leading_emoji(entry["label"]), position)
    clear_flow(context)

    await reply_tracked(
        context, message,
        "✅ <b>تم تغيير الإيموجي بنجاح</b>\n"
        f"سيظهر الإيموجي الجديد تلقائيًا كأيقونة على زر «{esc(entry['label'])}» "
        "نفسه، وداخل رسائل البوت المرتبطة به، دون الحاجة لتعديل الكود أو إعادة "
        "تشغيل البوت.\n\n"
        "ℹ️ ظهور الأيقونة على الزر تحديدًا يتطلب أن يكون مالك البوت مشتركًا في "
        "Telegram Premium (أو أن يمتلك البوت يوزرًا إضافيًا عبر Fragment) — وهو "
        "شرط من تيليجرام نفسها.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[back_btn("🔙 رجوع لقائمة الإيموجي", "adm:emoji")]]),
    )


async def cb_product_edit_color_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعرض خيارات الألوان الأربعة لزر منتج محدد في المتجر."""
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    product_id = int(query.data.split(":")[3])
    conn = get_conn()
    p = conn.execute("SELECT * FROM products WHERE product_id=?", (product_id,)).fetchone()
    conn.close()
    if not p:
        await query.answer("⚠️ المنتج غير موجود.", show_alert=True)
        return
    current = BUTTON_STYLE_CHOICES.get(p["button_color"], p["button_color"])
    text = (
        f"🎨 لون زر المنتج «{esc(p['name'])}» في المتجر\n\n"
        f"اللون الحالي: {current}\n"
        "اختر اللون الجديد لهذا المنتج فقط (لن يتأثر أي منتج آخر):"
    )
    await query.answer()
    await query.edit_message_text(
        text,
        reply_markup=color_choice_keyboard(f"adm:pr:setcolor:{product_id}", f"adm:pr:manage:{product_id}"),
    )


async def cb_product_edit_color_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    _, _, _, product_id_str, style = query.data.split(":", 4)
    product_id = int(product_id_str)
    conn = get_conn()
    conn.execute("UPDATE products SET button_color=? WHERE product_id=?", (style, product_id))
    conn.commit()
    conn.close()
    await query.answer("✅ تم تغيير لون زر المنتج.")
    await cb_product_manage(update, context)


# --------------------- ⚙️ الإعدادات ---------------------

async def adm_show_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, answer: bool = True) -> None:
    query = update.callback_query
    channel_id = get_delivery_channel_id()
    channel_display = (get_delivery_channel_title() or channel_id) if channel_id else "غير محدد"
    maint_on = is_maintenance_mode()
    maint_display = "🔴 مفعّل" if maint_on else "🟢 متوقف"
    apk_display = get_app_apk_file_name() or ("محددة ✅" if get_app_apk_file_id() else "غير محددة ❌")
    text = (
        "⚙️ <b>الإعدادات</b>\n\n"
        f"🎁 النقاط اليومية: {get_daily_points()}\n"
        f"🔗 نقاط الإحالة: {get_referral_points()}\n"
        f"❤️ الحد الأدنى للإعجابات (عرض فقط): {get_min_video_likes()}\n"
        f"📣 قناة تسليم الطلبات: {esc(channel_display)}\n"
        f"📱 نسخة التطبيق (APK): {esc(apk_display)}\n"
        f"🔧 وضع الصيانة: {maint_display}\n"
    )
    maint_btn_text = "🟢 إيقاف وضع الصيانة" if maint_on else "🔴 تفعيل وضع الصيانة"
    kb = InlineKeyboardMarkup(
        [
            [styled_button("✏️ تعديل النقاط اليومية", "adm:st:daily", "adm_st_daily", "✏️ زر تعديل النقاط اليومية", "primary")],
            [styled_button("✏️ تعديل نقاط الإحالة", "adm:st:ref", "adm_st_ref", "✏️ زر تعديل نقاط الإحالة", "primary")],
            [styled_button("✏️ تعديل حد الإعجابات", "adm:st:likes", "adm_st_likes", "✏️ زر تعديل حد الإعجابات", "primary")],
            [styled_button("📣 قناة تسليم الطلبات", "adm:st:delch", "adm_st_delch", "📣 قسم قناة تسليم الطلبات", "primary")],
            [styled_button("📱 نسخة التطبيق (APK)", "adm:st:apk", "adm_st_apk", "📱 قسم نسخة التطبيق (APK)", "primary")],
            [styled_button(maint_btn_text, "adm:st:maint:toggle", "adm_st_maint", "🔧 زر تبديل وضع الصيانة", "danger")],
            [back_btn("🔙 رجوع", "adm:menu")],
        ]
    )
    # ملاحظة: عند الاستدعاء بعد أن يكون الزر قد أجاب على الاستعلام بالفعل
    # (مثل تبديل وضع الصيانة)، answer=False لتفادي محاولة الرد على نفس
    # callback_query مرتين (وهو أمر ترفضه تيليجرام ويسبب خطأ).
    if answer:
        await query.answer()
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cb_settings_maintenance_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_owner_callback(update):
        return
    set_maintenance_mode(not is_maintenance_mode())
    await update.callback_query.answer(
        "🔧 تم تفعيل وضع الصيانة." if is_maintenance_mode() else "✅ تم إيقاف وضع الصيانة.",
        show_alert=True,
    )
    await adm_show_settings_menu(update, context, answer=False)


# --------------------- 📣 إدارة قناة تسليم الطلبات ---------------------

async def adm_show_delivery_channel_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, answer: bool = True) -> None:
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    channel_id = get_delivery_channel_id()

    if channel_id:
        title = get_delivery_channel_title() or channel_id
        text = (
            "📣 <b>قناة تسليم الطلبات</b>\n\n"
            f"القناة الحالية: {esc(title)}\n"
            f"الآيدي: <code>{esc(channel_id)}</code>\n\n"
            "سيقوم البوت تلقائيًا بنشر إشعار في هذه القناة عند كل عملية "
            "تسليم مفتاح ناجحة."
        )
        kb_rows = [
            [styled_button("✏️ تغيير القناة", "adm:st:delch:set", "adm_st_delch_set", "✏️ زر تغيير قناة تسليم الطلبات", "primary")],
            [styled_button("🗑️ إزالة القناة", "adm:st:delch:remove", "adm_st_delch_del", "🗑️ زر إزالة قناة تسليم الطلبات", "danger")],
        ]
    else:
        text = (
            "📣 <b>قناة تسليم الطلبات</b>\n\n"
            "لم يتم تحديد أي قناة بعد.\n"
            "حدّد القناة التي سيتم فيها نشر إشعار عند كل تسليم مفتاح ناجح."
        )
        kb_rows = [
            [styled_button("➕ تعيين القناة", "adm:st:delch:set", "adm_st_delch_set", "✏️ زر تغيير قناة تسليم الطلبات", "success")],
        ]

    kb_rows.append([back_btn("🔙 رجوع", "adm:settings")])
    if answer:
        await query.answer()
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb_rows))


async def cb_delivery_channel_set_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    set_flow(context, "adm_delivery_channel_set")
    await query.answer()
    await query.message.reply_text(
        "✏️ أرسل يوزر القناة (مثل @channel) أو آيدي الشات الرقمي الخاص "
        "بقناة تسليم الطلبات.\n\n"
        "⚠️ مهم: يجب رفع هذا البوت كمشرف (Admin) داخل القناة حتى "
        "يستطيع نشر إشعارات التسليم فيها.\n\n"
        "مثال:\n@my_delivery_channel\nأو\n-1001234567890",
        reply_markup=cancel_kb(),
    )


async def adm_delivery_channel_set_input(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    clear_flow(context)
    token = update.message.text.strip().split()[0]

    try:
        if token.lstrip("-").isdigit():
            chat_ref = int(token)
        elif token.startswith("@"):
            chat_ref = token
        else:
            chat_ref = f"@{token}"
        chat = await context.bot.get_chat(chat_ref)
    except (BadRequest, Forbidden, TelegramError) as e:
        await update.message.reply_text(
            f"❌ تعذر الوصول إلى القناة: {esc(str(e))}", reply_markup=admin_menu_keyboard()
        )
        return

    is_admin_here = False
    try:
        bot_member = await context.bot.get_chat_member(chat.id, context.bot.id)
        is_admin_here = bot_member.status in ("administrator", "creator")
    except TelegramError:
        pass

    title = chat.title or chat.username or str(chat.id)
    set_delivery_channel(chat.id, title)

    warning = "" if is_admin_here else (
        "\n\n⚠️ تنبيه: لم يتم التأكد من أن البوت مشرف في هذه القناة. "
        "تأكد من رفعه كمشرف حتى تعمل الإشعارات بشكل صحيح."
    )
    await update.message.reply_text(
        f"✅ تم تعيين «{esc(title)}» كقناة لتسليم الطلبات.{warning}",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_menu_keyboard(),
    )


async def cb_delivery_channel_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    clear_delivery_channel()
    await query.answer("✅ تمت إزالة قناة تسليم الطلبات.", show_alert=True)
    await adm_show_delivery_channel_menu(update, context, answer=False)


# --------------------- 📱 إدارة نسخة التطبيق (APK) ---------------------

async def adm_show_apk_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, answer: bool = True) -> None:
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    file_id = get_app_apk_file_id()
    file_name = get_app_apk_file_name()
    description = get_app_apk_caption()

    if file_id:
        desc_display = esc(description) if description else "— لم تتم إضافة وصف بعد —"
        text = (
            "📱 <b>نسخة التطبيق (APK)</b>\n"
            "━━━━━━━━━━━━━━━\n\n"
            f"📦 <b>اسم الملف:</b> {esc(file_name) or '—'}\n\n"
            f"📝 <b>الوصف الحالي:</b>\n{desc_display}\n\n"
            "━━━━━━━━━━━━━━━\n"
            "عند ضغط أي مستخدم على زر «📱 النسخة» في القائمة الرئيسية، "
            "سيصله هذا الملف مع الوصف أعلاه، وزر رجوع مرفق أسفل الرسالة "
            "مباشرة."
        )
        kb_rows = [
            [styled_button("📝 إضافة / تعديل الوصف", "adm:st:apk:desc", "adm_st_apk_desc", "📝 زر تعديل وصف النسخة", "primary")],
            [styled_button("⬆️ رفع نسخة جديدة (استبدال)", "adm:st:apk:set", "adm_st_apk_set", "⬆️ زر رفع نسخة APK جديدة", "primary")],
            [styled_button("🗑️ حذف الملف الحالي", "adm:st:apk:remove", "adm_st_apk_del", "🗑️ زر حذف ملف APK الحالي", "danger")],
        ]
    else:
        text = (
            "📱 <b>نسخة التطبيق (APK)</b>\n"
            "━━━━━━━━━━━━━━━\n\n"
            "😔 لم يتم رفع أي ملف بعد.\n\n"
            "ارفع ملف APK ليتمكن المستخدمون من تحميله عبر زر «📱 النسخة» في "
            "القائمة الرئيسية، ويمكنك بعدها إضافة وصف مختصر للنسخة يظهر "
            "لهم مع الملف."
        )
        kb_rows = [
            [styled_button("⬆️ رفع ملف APK", "adm:st:apk:set", "adm_st_apk_set", "⬆️ زر رفع نسخة APK جديدة", "success")],
        ]

    kb_rows.append([back_btn("🔙 رجوع", "adm:settings")])
    if answer:
        await query.answer()
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb_rows))


async def cb_apk_set_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    set_flow(context, "adm_apk_upload")
    await query.answer()
    await query.message.reply_text(
        "📎 أرسل الآن ملف APK <b>كمستند (Document)</b> ليتم تعيينه كنسخة "
        "التطبيق الحالية التي يحصل عليها المستخدمون عند الضغط على زر «📱 النسخة».",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_kb(),
    )


async def adm_apk_upload_input(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    """يستقبل ملف الـ APK المرسل من المالك كمستند (Document) ويحفظ file_id
    الخاص به في الإعدادات."""
    doc = update.message.document
    if not doc:
        await update.message.reply_text(
            "⚠️ الرجاء إرسال الملف كمستند (Document) وليس كصورة أو نوع آخر.",
            reply_markup=cancel_kb(),
        )
        return

    file_name = doc.file_name or "app.apk"
    is_apk_ext = file_name.lower().endswith(".apk")
    is_apk_mime = doc.mime_type in (
        "application/vnd.android.package-archive",
        "application/octet-stream",
    )
    if not (is_apk_ext or is_apk_mime):
        await update.message.reply_text(
            "⚠️ الرجاء إرسال ملف بصيغة APK فقط.", reply_markup=cancel_kb()
        )
        return

    set_app_apk(doc.file_id, file_name)
    clear_flow(context)
    await update.message.reply_text(
        f"✅ تم تعيين ملف النسخة بنجاح.\n📦 اسم الملف: {esc(file_name)}\n\n"
        "💡 يمكنك الآن إضافة وصف للنسخة من قسم «📱 نسخة التطبيق (APK)» في "
        "الإعدادات.",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_menu_keyboard(),
    )


async def adm_apk_upload_wrong_type(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    """يُستدعى إن أرسل المالك نصًا بدل ملف أثناء انتظار رفع الـ APK."""
    await update.message.reply_text(
        "⚠️ الرجاء إرسال ملف APK كمستند (Document)، وليس نصًا.",
        reply_markup=cancel_kb(),
    )


async def cb_apk_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    clear_app_apk()
    await query.answer("✅ تم حذف ملف النسخة.", show_alert=True)
    await adm_show_apk_menu(update, context, answer=False)


async def cb_apk_desc_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يبدأ تدفق إضافة/تعديل وصف النسخة (يظهر للمستخدمين أسفل ملف الـ APK)."""
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    if not get_app_apk_file_id():
        await query.answer("⚠️ ارفع ملف APK أولًا قبل إضافة وصف له.", show_alert=True)
        return

    set_flow(context, "adm_apk_caption_set")
    current = get_app_apk_caption()
    hint = f"\n\n📝 <b>الوصف الحالي:</b>\n{esc(current)}" if current else ""
    await query.answer()
    await query.message.reply_text(
        "✏️ أرسل الآن الوصف الذي تريد إضافته أسفل النسخة (سيظهر للمستخدمين "
        "مع ملف الـ APK عند إرساله لهم).\n\n"
        "لحذف الوصف الحالي فقط دون إضافة وصف جديد، أرسل: -"
        + hint,
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_kb(),
    )


async def adm_apk_caption_set_input(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    text = update.message.text.strip()
    clear_flow(context)

    if text == "-":
        set_app_apk_caption("")
        await update.message.reply_text(
            "✅ تم حذف وصف النسخة.", reply_markup=admin_menu_keyboard()
        )
        return

    set_app_apk_caption(text)
    await update.message.reply_text(
        f"✅ تم حفظ وصف النسخة بنجاح.\n\n📝 <b>المعاينة:</b>\n\n{build_app_apk_caption()}",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_menu_keyboard(),
    )


async def cb_settings_daily_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    set_flow(context, "adm_settings_daily")
    await query.answer()
    await query.message.reply_text("✏️ أرسل القيمة الجديدة للنقاط اليومية:", reply_markup=cancel_kb())


async def adm_settings_daily_input(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    text = update.message.text.strip()
    if not text.isdigit() or int(text) < 0:
        await update.message.reply_text("⚠️ أرسل رقمًا صحيحًا غير سالب.", reply_markup=cancel_kb())
        return
    set_setting("daily_points", text)
    clear_flow(context)
    await update.message.reply_text(f"✅ تم تحديث النقاط اليومية إلى {text}.")


async def cb_settings_referral_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    set_flow(context, "adm_settings_referral")
    await query.answer()
    await query.message.reply_text("✏️ أرسل القيمة الجديدة لنقاط الإحالة:", reply_markup=cancel_kb())


async def adm_settings_referral_input(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    text = update.message.text.strip()
    if not text.isdigit() or int(text) < 0:
        await update.message.reply_text("⚠️ أرسل رقمًا صحيحًا غير سالب.", reply_markup=cancel_kb())
        return
    set_setting("referral_points", text)
    clear_flow(context)
    await update.message.reply_text(f"✅ تم تحديث نقاط الإحالة إلى {text}.")


async def cb_settings_likes_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    set_flow(context, "adm_settings_min_likes")
    await query.answer()
    await query.message.reply_text(
        "✏️ أرسل الحد الأدنى الجديد للإعجابات (يُعرض للمستخدم فقط ولا يتم التحقق منه آليًا):",
        reply_markup=cancel_kb(),
    )


async def adm_settings_min_likes_input(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    text = update.message.text.strip()
    if not text.isdigit() or int(text) < 0:
        await update.message.reply_text("⚠️ أرسل رقمًا صحيحًا غير سالب.", reply_markup=cancel_kb())
        return
    set_setting("min_video_likes", text)
    clear_flow(context)
    await update.message.reply_text(f"✅ تم تحديث الحد الأدنى للإعجابات إلى {text}.")


# --------------------- 🔒 إدارة الاشتراك الإجباري ---------------------

async def adm_show_forcesub_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, answer: bool = True) -> None:
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    channels = get_force_sub_channels()

    kb_rows = []
    if not channels:
        text = (
            "🔒 <b>الاشتراك الإجباري</b>\n\n"
            "لا توجد أي قناة أو مجموعة مضافة حاليًا.\n"
            "أضف قناة أو مجموعة ليُطلب من كل عضو الاشتراك بها قبل استخدام البوت."
        )
    else:
        lines = []
        for ch in channels:
            label = ch["title"] or ch["username"] or ch["chat_id"]
            lines.append(f"• {esc(label)} — <code>{esc(ch['chat_id'])}</code>")
            kb_rows.append(
                [
                    styled_button(
                        f"🗑️ حذف: {label[:24]}",
                        f"adm:fs:delc:{ch['id']}",
                        "adm_fs_del_btn",
                        "🗑️ زر حذف قناة اشتراك إجباري",
                        "danger",
                    )
                ]
            )
        text = (
            "🔒 <b>الاشتراك الإجباري</b>\n\n"
            "القنوات/المجموعات المطلوب الاشتراك بها حاليًا:\n" + "\n".join(lines)
        )

    kb_rows.append(
        [styled_button("➕ إضافة قناة/مجموعة", "adm:fs:add", "adm_fs_add_btn", "➕ زر إضافة اشتراك إجباري", "success")]
    )
    kb_rows.append([back_btn("🔙 رجوع", "adm:menu")])

    if answer:
        await query.answer()
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb_rows))
    except (BadRequest, TelegramError):
        await query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb_rows))


async def cb_forcesub_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    set_flow(context, "adm_fs_add")
    await query.answer()
    await query.message.reply_text(
        "✏️ أرسل يوزر القناة/المجموعة (مثل @channel) أو آيدي الشات الرقمي.\n"
        "يمكنك إضافة أكثر من قناة/مجموعة دفعة واحدة: أرسل كل واحدة في سطر منفصل.\n\n"
        "⚠️ مهم: يجب إضافة هذا البوت كمشرف (Admin) داخل القناة/المجموعة حتى يستطيع "
        "التحقق من اشتراك الأعضاء والحصول على رابط الدعوة تلقائيًا.\n\n"
        "مثال:\n@my_channel\n-1001234567890",
        reply_markup=cancel_kb(),
    )


async def adm_fs_add_input(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    clear_flow(context)
    raw_text = update.message.text.strip()
    lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
    if not lines:
        await update.message.reply_text("⚠️ لم يتم إرسال أي شيء صالح.", reply_markup=admin_menu_keyboard())
        return

    added, failed = [], []
    conn = get_conn()
    try:
        for line in lines:
            token = line.split()[0]
            try:
                if token.lstrip("-").isdigit():
                    chat_ref = int(token)
                elif token.startswith("@"):
                    chat_ref = token
                else:
                    chat_ref = f"@{token}"
                chat = await context.bot.get_chat(chat_ref)
            except (BadRequest, Forbidden, TelegramError) as e:
                failed.append(f"{esc(token)} — {esc(str(e))}")
                continue

            invite_link = None
            try:
                bot_member = await context.bot.get_chat_member(chat.id, context.bot.id)
                if bot_member.status in ("administrator", "creator"):
                    invite_link = getattr(chat, "invite_link", None)
                    if not invite_link:
                        try:
                            invite_link = await context.bot.export_chat_invite_link(chat.id)
                        except TelegramError:
                            invite_link = None
                else:
                    logger.warning(
                        "⚠️ البوت ليس مشرفًا في %s؛ لن يعمل التحقق من الاشتراك لهذه القناة بشكل موثوق.",
                        chat.id,
                    )
            except TelegramError:
                pass

            try:
                conn.execute(
                    "INSERT INTO force_sub_channels "
                    "(chat_id, title, username, invite_link, chat_type, added_at, added_by) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(chat_id) DO UPDATE SET "
                    "title=excluded.title, username=excluded.username, "
                    "invite_link=excluded.invite_link, chat_type=excluded.chat_type",
                    (
                        str(chat.id),
                        chat.title or (chat.full_name if hasattr(chat, "full_name") else "") or "",
                        chat.username,
                        invite_link,
                        chat.type,
                        now_str(),
                        update.effective_user.id,
                    ),
                )
                added.append(chat.title or chat.username or str(chat.id))
            except sqlite3.Error as e:
                failed.append(f"{esc(token)} — {esc(str(e))}")
        conn.commit()
    finally:
        conn.close()

    parts = []
    if added:
        parts.append("✅ <b>تمت الإضافة:</b>\n" + "\n".join(f"• {esc(a)}" for a in added))
    if failed:
        parts.append("❌ <b>فشلت الإضافة:</b>\n" + "\n".join(f"• {f}" for f in failed))
    result_text = "\n\n".join(parts) if parts else "لم تتم إضافة أي شيء."
    await update.message.reply_text(result_text, parse_mode=ParseMode.HTML, reply_markup=admin_menu_keyboard())


async def cb_forcesub_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    fs_id = int(query.data.split(":")[3])
    kb = InlineKeyboardMarkup(
        [
            [
                styled_button("✅ تأكيد الحذف", f"adm:fs:delok:{fs_id}", "adm_fs_delok_btn", "✅ زر تأكيد حذف قناة اشتراك إجباري", "danger"),
                back_btn("❌ إلغاء", "adm:fs"),
            ]
        ]
    )
    await query.answer()
    await query.edit_message_text(
        "⚠️ هل أنت متأكد من حذف هذه القناة/المجموعة من قائمة الاشتراك الإجباري؟",
        reply_markup=kb,
    )


async def cb_forcesub_delete_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    fs_id = int(query.data.split(":")[3])
    conn = get_conn()
    conn.execute("DELETE FROM force_sub_channels WHERE id=?", (fs_id,))
    conn.commit()
    conn.close()
    await query.answer("✅ تم الحذف بنجاح.", show_alert=True)
    await adm_show_forcesub_menu(update, context, answer=False)


# --------------------- 👮 إدارة الأدمن (رفع/تعديل/إزالة) ---------------------

async def adm_show_admins_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, answer: bool = True) -> None:
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    admins = list_admins()
    lines = ["👮 <b>إدارة الأدمن</b>\n"]
    rows = []
    if not admins:
        lines.append("لا يوجد أدمن مرفوع حاليًا.")
    else:
        for a in admins:
            u = get_user(a["user_id"])
            name = esc(u["first_name"]) if u else "—"
            perms_count = len([p for p in a.get("permissions", "").split(",") if p])
            lines.append(f"\n🔸 {name} — <code>{a['user_id']}</code> ({perms_count} صلاحية)")
            rows.append(
                [
                    styled_button(
                        f"✏️ {name} ({a['user_id']})",
                        f"adm:ad:manage:{a['user_id']}",
                        "adm_ad_manage",
                        "✏️ زر تعديل صلاحيات أدمن",
                        "primary",
                    )
                ]
            )
    rows.append([styled_button("➕ رفع أدمن جديد", "adm:ad:add", "adm_ad_add", "➕ زر رفع أدمن جديد", "success")])
    rows.append([back_btn("🔙 رجوع", "adm:menu")])
    # ملاحظة: عند الاستدعاء بعد أن يكون الزر قد أجاب على الاستعلام بالفعل
    # (مثل الحفظ/الإلغاء/الإزالة)، answer=False لتفادي محاولة الرد على
    # نفس callback_query مرتين (وهو أمر ترفضه تيليجرام ويسبب خطأ).
    if answer:
        await query.answer()
    await query.edit_message_text(
        "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows)
    )


async def cb_admin_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    set_flow(context, "adm_admin_target")
    await query.answer()
    await query.message.reply_text(
        "✏️ أرسل آيدي المستخدم أو Username لرفعه كأدمن.\n"
        "⚠️ يجب أن يكون قد استخدم البوت من قبل (ضغط /start) حتى يكون بياناته مسجّلة.",
        reply_markup=cancel_kb(),
    )


async def adm_admin_target_input(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    q = update.message.text.strip()
    clear_flow(context)
    user = find_user_by_query(q)
    if not user:
        await update.message.reply_text("❌ لم يتم العثور على المستخدم. تأكد أنه استخدم البوت من قبل.")
        return
    if user["user_id"] == OWNER_ID:
        await update.message.reply_text("⛔ المالك يملك كل الصلاحيات أصلًا ولا حاجة لرفعه كأدمن.")
        return

    target_id = user["user_id"]
    existing_perms = get_admin_permissions(target_id)
    context.user_data["pending_admin_target"] = target_id
    context.user_data["pending_admin_perms"] = set(existing_perms)

    await update.message.reply_text(
        f"👤 المستخدم: {esc(user['first_name'])} (<code>{target_id}</code>)\n\n"
        "اختر الصلاحيات التي تريد منحها له، ثم اضغط «💾 رفع أدمن / حفظ الصلاحيات»:",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_perm_toggle_keyboard(target_id, existing_perms),
    )


async def cb_admin_manage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    target_id = int(query.data.split(":")[3])
    user = get_user(target_id)
    if not user:
        await query.answer("❌ المستخدم غير موجود.", show_alert=True)
        return

    existing_perms = get_admin_permissions(target_id)
    context.user_data["pending_admin_target"] = target_id
    context.user_data["pending_admin_perms"] = set(existing_perms)

    await query.answer()
    await query.edit_message_text(
        f"👤 المستخدم: {esc(user['first_name'])} (<code>{target_id}</code>)\n\n"
        "اضغط على أي صلاحية لتبديلها، ثم اضغط «💾 حفظ»، أو أزل الأدمن نهائيًا:",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_perm_toggle_keyboard(target_id, existing_perms),
    )


async def cb_admin_toggle_perm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    target_id = context.user_data.get("pending_admin_target")
    if target_id is None:
        await query.answer("⚠️ انتهت الجلسة، ابدأ من جديد من قسم إدارة الأدمن.", show_alert=True)
        return

    perm = query.data.split(":")[3]
    perms = context.user_data.get("pending_admin_perms", set())
    if perm in perms:
        perms.discard(perm)
    else:
        perms.add(perm)
    context.user_data["pending_admin_perms"] = perms

    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=admin_perm_toggle_keyboard(target_id, perms))
    except (BadRequest, TelegramError):
        pass


async def cb_admin_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    target_id = context.user_data.pop("pending_admin_target", None)
    perms = context.user_data.pop("pending_admin_perms", set())
    if target_id is None:
        await query.answer("⚠️ لا توجد عملية جارية.", show_alert=True)
        return

    was_admin = is_admin(target_id)
    if not perms:
        if was_admin:
            remove_admin(target_id)
            await query.answer("🗑️ لم يتم اختيار أي صلاحية، فتم إزالة الأدمن.", show_alert=True)
        else:
            await query.answer("⚠️ اختر صلاحية واحدة على الأقل قبل الحفظ.", show_alert=True)
            context.user_data["pending_admin_target"] = target_id
            context.user_data["pending_admin_perms"] = perms
            return
    else:
        set_admin_permissions(target_id, perms, added_by=update.effective_user.id)
        await query.answer("✅ تم حفظ صلاحيات الأدمن بنجاح.", show_alert=True)

        # إشعار الشخص المرفوع، وإظهار زر لوحة الأدمن له تلقائيًا (main_menu_keyboard
        # يتحقق من is_owner_or_admin في كل مرة، فلا حاجة لأي إجراء إضافي غير الإشعار)
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=(
                    "🛡️ <b>تمت ترقيتك إلى أدمن في البوت!</b>\n\n"
                    "الصلاحيات الممنوحة لك:\n" + admin_permissions_display(perms) + "\n\n"
                    "استخدم /admin أو زر 👑 لوحة التحكم من القائمة الرئيسية للوصول إليها."
                ),
                parse_mode=ParseMode.HTML,
            )
        except (Forbidden, TelegramError):
            pass

    await adm_show_admins_menu(update, context, answer=False)


async def cb_admin_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_owner_callback(update):
        return
    context.user_data.pop("pending_admin_target", None)
    context.user_data.pop("pending_admin_perms", None)
    query = update.callback_query
    await query.answer("تم الإلغاء.")
    await adm_show_admins_menu(update, context, answer=False)


async def cb_admin_remove_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    target_id = int(query.data.split(":")[3])
    user = get_user(target_id)
    name = esc(user["first_name"]) if user else str(target_id)
    kb = InlineKeyboardMarkup(
        [
            [
                styled_button("✅ نعم، إزالة", f"adm:ad:removeok:{target_id}", "adm_ad_removeok", "✅ تأكيد إزالة أدمن", "danger"),
                back_btn("🔙 رجوع", "adm:admins"),
            ]
        ]
    )
    await query.answer()
    await query.edit_message_text(
        f"⚠️ هل أنت متأكد من إزالة {name} (<code>{target_id}</code>) من الأدمنية نهائيًا؟",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


async def cb_admin_remove_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard_owner_callback(update):
        return
    query = update.callback_query
    target_id = int(query.data.split(":")[3])
    remove_admin(target_id)
    context.user_data.pop("pending_admin_target", None)
    context.user_data.pop("pending_admin_perms", None)
    await query.answer("✅ تمت إزالة الأدمن بنجاح.", show_alert=True)
    try:
        await context.bot.send_message(chat_id=target_id, text="ℹ️ تم إلغاء صلاحياتك كأدمن في البوت.")
    except (Forbidden, TelegramError):
        pass
    await adm_show_admins_menu(update, context, answer=False)


# ============================================================
#                  أوامر إضافية ومعالجة الأخطاء
# ============================================================

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر /admin كطريقة بديلة للوصول للوحة التحكم (للمالك وللأدمن)."""
    user_id = update.effective_user.id
    if not is_owner_or_admin(user_id):
        await update.message.reply_text("⛔ هذا الأمر مخصص للمالك والأدمن فقط.")
        return
    await update.message.reply_text(
        "👑 <b>لوحة التحكم</b>\nاختر القسم الذي تريد إدارته:",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_menu_keyboard(user_id),
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_flow(context)
    await update.message.reply_text(
        "تم إلغاء أي عملية جارية.", reply_markup=main_menu_keyboard(update.effective_user.id)
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """معالج أخطاء عام يمنع توقف البوت عند حدوث استثناء غير متوقع.

    يُصنّف الأخطاء الشائعة (شبكة/حدود إرسال/حظر من المستخدم) بشكل خاص
    حتى لا تُسجَّل كأخطاء حرجة ولا تُرسِل رسالة إزعاج للمستخدم، بينما
    يتم تسجيل أي خطأ برمجي غير متوقع بالتفصيل الكامل (traceback) لتسهيل
    تتبعه لاحقًا دون أن يتسبب أبدًا في توقف عملية البوت الرئيسية."""
    err = context.error

    if isinstance(err, RetryAfter):
        logger.warning("⏳ تجاوز حد الإرسال المسموح من تيليجرام، الانتظار %s ثانية.", err.retry_after)
        return
    if isinstance(err, (NetworkError, TimedOut)):
        logger.warning("🌐 خطأ شبكة مؤقت أثناء التواصل مع تيليجرام: %s", err)
        return
    if isinstance(err, Forbidden):
        # المستخدم حظر البوت أو غادر المحادثة؛ لا داعي لأي إجراء إضافي
        logger.info("🚫 المستخدم حظر البوت أو المحادثة غير متاحة: %s", err)
        return

    logger.error("حدث خطأ أثناء معالجة تحديث: %s", err, exc_info=err)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ حدث خطأ غير متوقع، تم إبلاغ المطور. حاول مجددًا لاحقًا."
            )
    except TelegramError:
        pass


# ============================================================
#                            main()
# ============================================================

def build_callback_handlers(app: Application) -> None:
    # المنتجات (مستخدم)
    app.add_handler(CallbackQueryHandler(cb_product_view, pattern=r"^prod:view:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_product_buy, pattern=r"^prod:buy:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_product_list_back, pattern=r"^prod:list$"))
    app.add_handler(CallbackQueryHandler(cb_menu_home, pattern=r"^menu:home$"))

    # 🌐 اللغة + أزرار غير فعالة (عناوين الأعمدة في المتجر)
    app.add_handler(CallbackQueryHandler(cb_language_set, pattern=r"^lang:set:[a-z]{2}$"))
    app.add_handler(CallbackQueryHandler(cb_noop, pattern=r"^noop$"))

    # 📋 أزرار القائمة الرئيسية (Inline) بدل الكيبورد الثابت أسفل الشاشة
    app.add_handler(
        CallbackQueryHandler(
            cb_main_menu, pattern=r"^menu:(store|version|daily|account|referral|video|lang|admin|support)$"
        )
    )

    # طلبات الفيديو (مالك)
    app.add_handler(CallbackQueryHandler(cb_video_accept, pattern=r"^vr:accept:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_video_reject, pattern=r"^vr:reject:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_video_grant, pattern=r"^vr:grant:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_video_back, pattern=r"^vr:back:\d+$"))

    # زر الرجوع (يحل محل زر إلغاء العملية سابقًا)
    app.add_handler(CallbackQueryHandler(cb_flow_back, pattern=r"^flow:back$"))

    # لوحة المالك: القائمة الرئيسية للأقسام
    app.add_handler(
        CallbackQueryHandler(
            cb_admin_router,
            pattern=r"^adm:(menu|close|users|points|products|keys|gift|broadcast|ban|stats|settings|vr|colors|emoji|fs|st:delch|st:apk|admins)$",
        )
    )

    # 🔒 الاشتراك الإجباري
    app.add_handler(CallbackQueryHandler(cb_forcesub_check, pattern=r"^fsub:check$"))
    app.add_handler(CallbackQueryHandler(cb_forcesub_add_start, pattern=r"^adm:fs:add$"))
    app.add_handler(CallbackQueryHandler(cb_forcesub_delete_confirm, pattern=r"^adm:fs:delc:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_forcesub_delete_ok, pattern=r"^adm:fs:delok:\d+$"))

    # 🔄 تحويل النقاط بين الأعضاء
    app.add_handler(CallbackQueryHandler(cb_transfer_start, pattern=r"^acct:transfer$"))
    app.add_handler(CallbackQueryHandler(cb_transfer_confirm, pattern=r"^xfer:confirm$"))
    app.add_handler(CallbackQueryHandler(cb_transfer_cancel, pattern=r"^xfer:cancel$"))

    # المستخدمون
    app.add_handler(CallbackQueryHandler(cb_users_search_start, pattern=r"^adm:users:search$"))

    # النقاط
    app.add_handler(CallbackQueryHandler(cb_points_charge_start, pattern=r"^adm:points:charge$"))
    app.add_handler(CallbackQueryHandler(cb_points_deduct_start, pattern=r"^adm:points:deduct$"))
    app.add_handler(CallbackQueryHandler(cb_points_balance_start, pattern=r"^adm:points:balance$"))
    app.add_handler(CallbackQueryHandler(cb_points_log, pattern=r"^adm:points:log$"))

    # المنتجات (مالك)
    app.add_handler(CallbackQueryHandler(cb_product_add_start, pattern=r"^adm:pr:add$"))
    app.add_handler(CallbackQueryHandler(cb_product_manage, pattern=r"^adm:pr:manage:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_product_edit_name_start, pattern=r"^adm:pr:ename:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_product_edit_price_start, pattern=r"^adm:pr:eprice:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_product_edit_duration_start, pattern=r"^adm:pr:edur:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_product_delete_confirm, pattern=r"^adm:pr:delc:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_product_delete_ok, pattern=r"^adm:pr:delok:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_product_add_color, pattern=r"^adm:pr:addcolor:(primary|success|danger|none)$"))
    app.add_handler(CallbackQueryHandler(cb_product_edit_color_start, pattern=r"^adm:pr:ecolor:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_product_edit_color_set, pattern=r"^adm:pr:setcolor:\d+:(primary|success|danger|none)$"))

    # المفاتيح
    app.add_handler(CallbackQueryHandler(cb_keys_product_menu, pattern=r"^adm:ky:menu:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_keys_add_start, pattern=r"^adm:ky:add:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_keys_available_list, pattern=r"^adm:ky:avail:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_keys_sold_list, pattern=r"^adm:ky:sold:\d+$"))

    # روابط الهدايا
    app.add_handler(CallbackQueryHandler(cb_gift_create_start, pattern=r"^adm:gf:create$"))

    # الإذاعة
    app.add_handler(CallbackQueryHandler(cb_broadcast_all_start, pattern=r"^adm:bc:all$"))
    app.add_handler(CallbackQueryHandler(cb_broadcast_one_start, pattern=r"^adm:bc:one$"))
    app.add_handler(CallbackQueryHandler(cb_broadcast_send, pattern=r"^adm:bc:send$"))
    app.add_handler(CallbackQueryHandler(cb_broadcast_cancel, pattern=r"^adm:bc:cancel$"))

    # الحظر
    app.add_handler(CallbackQueryHandler(cb_ban_start, pattern=r"^adm:bn:ban$"))
    app.add_handler(CallbackQueryHandler(cb_unban_start, pattern=r"^adm:bn:unban$"))
    app.add_handler(CallbackQueryHandler(cb_ban_list, pattern=r"^adm:bn:list$"))

    # الإعدادات
    app.add_handler(CallbackQueryHandler(cb_settings_daily_start, pattern=r"^adm:st:daily$"))
    app.add_handler(CallbackQueryHandler(cb_settings_referral_start, pattern=r"^adm:st:ref$"))
    app.add_handler(CallbackQueryHandler(cb_settings_likes_start, pattern=r"^adm:st:likes$"))
    app.add_handler(CallbackQueryHandler(cb_delivery_channel_set_start, pattern=r"^adm:st:delch:set$"))
    app.add_handler(CallbackQueryHandler(cb_delivery_channel_remove, pattern=r"^adm:st:delch:remove$"))
    app.add_handler(CallbackQueryHandler(cb_apk_set_start, pattern=r"^adm:st:apk:set$"))
    app.add_handler(CallbackQueryHandler(cb_apk_remove, pattern=r"^adm:st:apk:remove$"))
    app.add_handler(CallbackQueryHandler(cb_apk_desc_start, pattern=r"^adm:st:apk:desc$"))
    app.add_handler(CallbackQueryHandler(cb_settings_maintenance_toggle, pattern=r"^adm:st:maint:toggle$"))

    # 👮 إدارة الأدمن
    app.add_handler(CallbackQueryHandler(cb_admin_add_start, pattern=r"^adm:ad:add$"))
    app.add_handler(CallbackQueryHandler(cb_admin_manage, pattern=r"^adm:ad:manage:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_admin_toggle_perm, pattern=r"^adm:ad:toggle:[a-z_]+$"))
    app.add_handler(CallbackQueryHandler(cb_admin_save, pattern=r"^adm:ad:save$"))
    app.add_handler(CallbackQueryHandler(cb_admin_cancel, pattern=r"^adm:ad:cancel$"))
    app.add_handler(CallbackQueryHandler(cb_admin_remove_confirm, pattern=r"^adm:ad:remove:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_admin_remove_ok, pattern=r"^adm:ad:removeok:\d+$"))

    # 🎨 ألوان الأزرار
    app.add_handler(CallbackQueryHandler(cb_colors_page, pattern=r"^adm:clr:page:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_colors_pick, pattern=r"^adm:clr:pick:[A-Za-z0-9_]+$"))
    app.add_handler(
        CallbackQueryHandler(cb_colors_set, pattern=r"^adm:clr:set:[A-Za-z0-9_]+:(primary|success|danger|none)$")
    )

    # 🎨 تغيير الإيموجي (Custom/Premium Emoji)
    app.add_handler(CallbackQueryHandler(cb_emoji_page, pattern=r"^adm:emj:page:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_emoji_pick, pattern=r"^adm:emj:pick:[A-Za-z0-9_]+$"))
    app.add_handler(CallbackQueryHandler(cb_emoji_position, pattern=r"^adm:emj:pos:[A-Za-z0-9_]+:(left|right)$"))
    app.add_handler(CallbackQueryHandler(cb_emoji_search_start, pattern=r"^adm:emj:search$"))
    app.add_handler(CallbackQueryHandler(cb_emoji_clear, pattern=r"^adm:emj:clear$"))
    app.add_handler(CallbackQueryHandler(cb_emoji_reset, pattern=r"^adm:emj:reset:[A-Za-z0-9_]+$"))


# ============================================================
#     🔒 قفل النسخة الواحدة (Single Instance Lock)
# ============================================================
# يمنع تشغيل أكثر من نسخة واحدة من البوت في نفس الوقت (سبب شائع جدًا
# للأعطال الغريبة وتكرار الردود على بعض الاستضافات التي قد تُعيد
# تشغيل العملية دون إيقاف القديمة تمامًا). يعتمد على قفل ملفي
# (advisory file lock) عبر fcntl، وهو مدعوم على كل استضافات
# Linux/VPS الشائعة (لا يعمل على Windows، حيث يتم تجاوزه بأمان).

LOCK_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.lock")
_lock_file_handle = None


def acquire_single_instance_lock() -> None:
    global _lock_file_handle
    try:
        import fcntl
    except ImportError:
        logger.warning(
            "⚠️ نظام التشغيل الحالي لا يدعم fcntl (على الأغلب Windows)؛ "
            "تم تخطي فحص النسخة الواحدة. تأكد يدويًا من عدم تشغيل أكثر من نسخة."
        )
        return

    _lock_file_handle = open(LOCK_FILE_PATH, "w")
    try:
        fcntl.flock(_lock_file_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.critical(
            "❌ يوجد نسخة أخرى من البوت تعمل بالفعل على نفس الخادم (bot.lock مقفل). "
            "تم إيقاف هذه النسخة لمنع تعارض الردود المزدوجة."
        )
        raise SystemExit(1)

    _lock_file_handle.write(str(os.getpid()))
    _lock_file_handle.flush()
    atexit.register(_release_single_instance_lock)


def _release_single_instance_lock() -> None:
    global _lock_file_handle
    if _lock_file_handle is None:
        return
    try:
        import fcntl
        fcntl.flock(_lock_file_handle, fcntl.LOCK_UN)
    except Exception:
        pass
    finally:
        try:
            _lock_file_handle.close()
        except Exception:
            pass


def main() -> None:
    if BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE" or not BOT_TOKEN:
        raise SystemExit(
            "❌ الرجاء ضبط BOT_TOKEN في بداية الملف قبل التشغيل."
        )
    if not OWNER_ID or OWNER_ID == 123456789:
        logger.warning("⚠️ لم يتم ضبط OWNER_ID بشكل صحيح؛ لوحة التحكم لن تعمل بشكل سليم.")

    acquire_single_instance_lock()
    init_db()

    # إعدادات أداء وتزامن عالية لتحمّل عدد كبير من المستخدمين والطلبات
    # في نفس الوقت (concurrent_updates يسمح بمعالجة عدة تحديثات بالتوازي
    # بدل انتظار كل واحد بدوره، وبقية الإعدادات تكبّر أحواض الاتصال
    # ومهلات الشبكة حتى لا تفشل الطلبات تحت الضغط العالي).
    app: Application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(256)
        .connection_pool_size(256)
        .pool_timeout(30.0)
        .connect_timeout(15.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .get_updates_connection_pool_size(8)
        .get_updates_read_timeout(40.0)
        .build()
    )

    # 🛡️ الحماية من السبام/الفيضان: يجب أن تعمل أولًا (group=-1) قبل أي
    # معالج آخر حتى توقف الطلبات المسيئة مبكرًا دون تنفيذ أي منطق ثقيل.
    app.add_handler(TypeHandler(Update, flood_guard_middleware), group=-1)

    # الأوامر
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("cancel", cancel_command))

    # أزرار القائمة الرئيسية (نصية) + توجيه أي نص آخر لمعالج التدفقات
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_main_menu_text))

    # استقبال ملفات (Document) لتدفقات لوحة التحكم التي تحتاج رفع ملف
    # (حاليًا: تعيين ملف APK للنسخة من قسم الإعدادات)
    app.add_handler(MessageHandler(filters.Document.ALL & ~filters.COMMAND, handle_admin_document))

    # كل معالجات الأزرار الشفافة (Inline)
    build_callback_handlers(app)

    # معالج الأخطاء العام
    app.add_error_handler(error_handler)

    # مهمة دورية: تنظيف ذاكرة الحماية من الفيضان فقط
    if app.job_queue is not None:
        app.job_queue.run_repeating(cleanup_flood_state_job, interval=3600, first=1800, name="flood_cleanup")
    else:
        logger.warning(
            "⚠️ JobQueue غير مفعّلة (يلزم تثبيت الحزمة بصيغة "
            "python-telegram-bot[job-queue])؛ لن يعمل تنظيف ذاكرة الحماية من الفيضان."
        )

    logger.info("🚀 البوت يعمل الآن...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    # مُشرف خارجي (Supervisor) يمنع توقف البوت نهائيًا حتى في حال حدوث
    # خطأ فادح غير متوقع خارج نطاق معالج الأخطاء العادي (مثال: خطأ عند
    # بدء التشغيل نفسه). عند أي فشل غير متوقع تتم إعادة المحاولة بعد
    # مهلة قصيرة بدل توقف العملية بشكل نهائي.
    while True:
        try:
            main()
            break
        except SystemExit:
            raise
        except KeyboardInterrupt:
            logger.info("⏹️ تم إيقاف البوت يدويًا.")
            break
        except Exception:
            logger.exception("💥 خطأ فادح غير متوقع أدى لتوقف البوت؛ إعادة المحاولة خلال 5 ثوانٍ...")
            time.sleep(5)
