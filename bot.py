"""
CAMGlobal Attendance Bot — Official Bot API version (v2)

Replaces the Telethon userbot approach entirely. Uses a real Telegram
Bot (created via @BotFather) instead of logging in as a personal
account — no session file, no phone number, no 2FA, no ToS risk.
If the bot token ever leaks, regenerate it in @BotFather in 10
seconds; nothing about a personal account is ever at stake.

Tracking method: since the official Bot API cannot see who joins or
leaves a live voice chat (that's a hard Telegram limitation, not a
gap in this code), attendance is tracked via:
  1. /checkin — member types this when they join a meeting
  2. Periodic roll-call pings ("Still here?") with a tappable button,
     posted every ROLLCALL_INTERVAL_MINUTES during the meeting window
  3. Attendance duration is estimated from first check-in to the
     last roll-call round the member actually responded to

Writes to the exact same Supabase schema as the old bot — meeting_types,
meetings, members, voice_events, attendance_records, attendance_review_queue,
leader_alerts, at_risk_members — so the WordPress Attendance Dashboard
and the AI monthly report need zero changes.
"""

import os
import asyncio
import logging
import requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────
BOT_TOKEN    = os.environ['TELEGRAM_BOT_TOKEN']          # from @BotFather, not a phone/session
SUPABASE_URL = os.environ['SUPABASE_URL'].rstrip('/')
SUPABASE_KEY = os.environ['SUPABASE_SERVICE_KEY']

ROLLCALL_INTERVAL_MINUTES = int(os.environ.get('ROLLCALL_INTERVAL_MINUTES', 15))

GROUP_IDS = {
    -1001433101619: 'main',
    -5237009034:    'rise',
    -1002413746503: 'sons',
    -1001510684437: 'family',
}

# In-memory state per active meeting: { chat_id: {...} }
active_meetings: dict = {}

# ── Supabase HTTP helpers (identical to the old bot — same schema) ──
HEADERS = {
    'apikey': SUPABASE_KEY,
    'Authorization': f'Bearer {SUPABASE_KEY}',
    'Content-Type': 'application/json',
    'Prefer': 'return=representation',
}

def sb_get(table, params=None):
    r = requests.get(f'{SUPABASE_URL}/rest/v1/{table}', headers=HEADERS, params=params, timeout=10)
    r.raise_for_status()
    return r.json()

def sb_post(table, data):
    r = requests.post(f'{SUPABASE_URL}/rest/v1/{table}', headers=HEADERS, json=data, timeout=10)
    r.raise_for_status()
    return r.json()

def sb_patch(table, match_params, data):
    r = requests.patch(f'{SUPABASE_URL}/rest/v1/{table}', headers=HEADERS, params=match_params, json=data, timeout=10)
    r.raise_for_status()
    return r.json()

def sb_upsert(table, data, on_conflict=None):
    headers = {**HEADERS, 'Prefer': 'resolution=merge-duplicates,return=representation'}
    params = {'on_conflict': on_conflict} if on_conflict else {}
    r = requests.post(f'{SUPABASE_URL}/rest/v1/{table}', headers=headers, params=params, json=data, timeout=10)
    r.raise_for_status()
    return r.json()

# ── Utilities ────────────────────────────────────────────────
def now_utc():
    return datetime.now(timezone.utc)

def find_meeting_type(group_type, now):
    dow = now.weekday()
    time_str = now.strftime('%H:%M:%S')
    rows = sb_get('meeting_types', {'group_type': f'eq.{group_type}', 'is_active': 'eq.true'})
    for mt in rows:
        days = mt.get('day_of_week') or []
        if dow not in days:
            continue
        start = mt['start_time']
        end   = mt['end_time']
        if end < start:
            if time_str >= start or time_str <= end:
                return mt
        else:
            if start <= time_str <= end:
                return mt
    return None

def get_or_create_meeting(chat_id, group_type):
    now = now_utc()
    mt  = find_meeting_type(group_type, now)
    if not mt:
        return None, None

    grp_rows = sb_get('telegram_groups', {'telegram_chat_id': f'eq.{chat_id}'})
    if not grp_rows:
        return None, None
    tg_group_id = grp_rows[0]['id']

    sh, sm = map(int, mt['start_time'][:5].split(':'))
    eh, em = map(int, mt['end_time'][:5].split(':'))
    s_start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    s_end   = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    if s_end < s_start:
        s_end += timedelta(days=1)

    existing = sb_get('meetings', {
        'meeting_type_id':   f'eq.{mt["id"]}',
        'telegram_group_id': f'eq.{tg_group_id}',
        'scheduled_start':   f'gte.{(s_start - timedelta(hours=1)).isoformat()}',
        'status':            'neq.cancelled',
    })
    if existing:
        m = existing[0]
        if m['status'] == 'scheduled':
            sb_patch('meetings', {'id': f'eq.{m["id"]}'}, {'status': 'live', 'actual_start': now.isoformat()})
        return m, mt

    result = sb_post('meetings', {
        'meeting_type_id':   mt['id'],
        'telegram_group_id': tg_group_id,
        'title':             mt['name'],
        'scheduled_start':   s_start.isoformat(),
        'scheduled_end':     s_end.isoformat(),
        'actual_start':      now.isoformat(),
        'status':            'live',
    })
    m = result[0] if isinstance(result, list) else result
    log.info(f'Created meeting: {mt["name"]}')
    return m, mt

def get_member_id(tg_user_id):
    # Only track members already linked in Supabase — never auto-create.
    rows = sb_get('members', {'telegram_user_id': f'eq.{tg_user_id}'})
    return rows[0]['id'] if rows else None

def record_event(meeting_id, member_id, tg_user_id, event_type, rollcall_round=None):
    data = {
        'meeting_id':       meeting_id,
        'member_id':        member_id,
        'telegram_user_id': tg_user_id,
        'event_type':       event_type,
        'event_time':       now_utc().isoformat(),
    }
    if rollcall_round is not None:
        data['rollcall_round'] = rollcall_round
    sb_post('voice_events', data)
    log.info(f'{event_type.upper()} | round={rollcall_round} | {str(member_id)[:8]}')

def calculate_attendance(meeting_id, member_id, cache):
    """
    Estimates presence duration from the spread of checkin/rollcall
    events a member actually responded to — first event to last event
    they responded to — instead of an explicit join/leave pair.
    """
    now  = now_utc()
    evts = sb_get('voice_events', {
        'meeting_id': f'eq.{meeting_id}',
        'member_id':  f'eq.{member_id}',
        'order':      'event_time.asc',
    })
    if not evts:
        return

    def parse_dt(s):
        return datetime.fromisoformat(s.replace('Z', '+00:00'))

    first_seen = parse_dt(evts[0]['event_time'])
    last_seen  = parse_dt(evts[-1]['event_time'])

    s_start = parse_dt(cache['scheduled_start'])
    s_end   = parse_dt(cache['scheduled_end'])

    duration_min = max(0, int((last_seen - first_seen).total_seconds() / 60))
    meeting_min  = max(1, int((s_end - s_start).total_seconds() / 60))
    pct          = min(100, round((duration_min / meeting_min) * 100))

    total_rounds     = cache.get('rollcall_count', 0)
    rounds_responded = len([e for e in evts if e['event_type'] == 'rollcall'])
    # If they responded to the final round posted before the meeting ended,
    # treat them as having stayed to the end even if duration_min undershoots
    # slightly (rounds are spaced ROLLCALL_INTERVAL_MINUTES apart).
    responded_to_last_round = (
        total_rounds > 0 and
        any(e['event_type'] == 'rollcall' and e.get('rollcall_round') == total_rounds for e in evts)
    )

    grace_join     = s_start + timedelta(minutes=cache['grace_join_min'])
    grace_exit     = s_end   - timedelta(minutes=cache['grace_exit_min'])
    joined_on_time = first_seen <= grace_join
    stayed_to_end  = responded_to_last_round or last_seen >= grace_exit
    auto_marked    = joined_on_time and stayed_to_end and pct >= cache['present_threshold']
    status         = 'present' if auto_marked else ('partial' if pct >= cache['partial_threshold'] else 'absent')

    sb_upsert('attendance_records', {
        'meeting_id':             meeting_id,
        'member_id':              member_id,
        'telegram_user_id':       evts[0].get('telegram_user_id'),
        'first_join_at':          first_seen.isoformat(),
        'last_leave_at':          last_seen.isoformat(),
        'total_duration_minutes': duration_min,
        'attendance_pct':         pct,
        'status':                 status,
        'auto_marked':            auto_marked,
        'calculated_at':          now.isoformat(),
    }, on_conflict='meeting_id,member_id')

    if not auto_marked:
        att_rows = sb_get('attendance_records', {'meeting_id': f'eq.{meeting_id}', 'member_id': f'eq.{member_id}'})
        if att_rows:
            att_id   = att_rows[0]['id']
            in_queue = sb_get('attendance_review_queue', {'attendance_record_id': f'eq.{att_id}', 'review_status': 'eq.pending'})
            if not in_queue:
                reason = (
                    f'{"On time" if joined_on_time else "Late"}, '
                    f'{"stayed to end" if stayed_to_end else "left early"}, '
                    f'{pct}% ({rounds_responded}/{max(total_rounds,1)} roll-calls answered)'
                )
                sb_post('attendance_review_queue', {
                    'attendance_record_id': att_id,
                    'meeting_id':           meeting_id,
                    'member_id':            member_id,
                    'suggested_status':     status,
                    'reason':               reason,
                    'confidence':           round(pct / 100, 2),
                })
    log.info(f'Attendance: {status} | {pct}% | rounds={rounds_responded}/{total_rounds}')

def create_leader_alert(member_id, alert_type, message, meeting_id=None):
    try:
        existing = sb_get('leader_alerts', {'member_id': f'eq.{member_id}', 'alert_type': f'eq.{alert_type}', 'is_read': 'eq.false'})
        if existing:
            return
        data = {'member_id': member_id, 'alert_type': alert_type, 'message': message, 'is_read': False, 'created_at': now_utc().isoformat()}
        if meeting_id:
            data['meeting_id'] = meeting_id
        sb_post('leader_alerts', data)
    except Exception as e:
        log.error(f'Failed to create leader alert: {e}')

# ── Commands ─────────────────────────────────────────────────
async def cmd_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in GROUP_IDS:
        # Logged so a mismatched/changed group ID can be identified and
        # added to GROUP_IDS — without this, an unrecognized group is
        # silently ignored with zero trace in the logs.
        log.warning(
            f'Received /checkin from an unrecognized chat_id={chat_id} '
            f'(chat title: "{update.effective_chat.title}"). '
            f'This chat is not in GROUP_IDS — add it if this should be tracked.'
        )
        return

    tg_uid    = update.effective_user.id
    member_id = await asyncio.to_thread(get_member_id, tg_uid)
    if not member_id:
        await context.bot.send_message(
            chat_id=chat_id,
            text="You're not linked to a CAMGlobal member account yet — ask an admin to link your Telegram to your WordPress profile."
        )
        return

    group_type = GROUP_IDS[chat_id]

    if chat_id not in active_meetings:
        meeting, mt = await asyncio.to_thread(get_or_create_meeting, chat_id, group_type)
        if not meeting or not mt:
            await context.bot.send_message(chat_id=chat_id, text="No meeting is scheduled right now for this group.")
            return
        active_meetings[chat_id] = {
            'meeting_id':        meeting['id'],
            'scheduled_start':   meeting['scheduled_start'],
            'scheduled_end':     meeting['scheduled_end'],
            'grace_join_min':    mt.get('grace_join_minutes', 15),
            'grace_exit_min':    mt.get('grace_exit_minutes', 20),
            'present_threshold': mt.get('present_threshold_pct', 80),
            'partial_threshold': mt.get('partial_threshold_pct', 50),
            'rollcall_count':    0,
            'last_rollcall_at':  None,
        }

    cache = active_meetings[chat_id]
    await asyncio.to_thread(record_event, cache['meeting_id'], member_id, tg_uid, 'checkin')
    await context.bot.send_message(chat_id=chat_id, text=f"✅ {update.effective_user.first_name}, you're checked in!")

async def rollcall_button_tap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    chat_id = query.message.chat_id

    if chat_id not in active_meetings:
        # The bot may have restarted since this roll-call was posted,
        # losing its in-memory tracking even though the meeting is
        # genuinely still live. Re-check Supabase directly before
        # concluding the meeting actually ended — this makes taps
        # resilient to a mid-meeting redeploy instead of silently
        # dropping them.
        if chat_id in GROUP_IDS:
            group_type = GROUP_IDS[chat_id]
            meeting, mt = await asyncio.to_thread(get_or_create_meeting, chat_id, group_type)
            if meeting and mt:
                active_meetings[chat_id] = {
                    'meeting_id':        meeting['id'],
                    'scheduled_start':   meeting['scheduled_start'],
                    'scheduled_end':     meeting['scheduled_end'],
                    'grace_join_min':    mt.get('grace_join_minutes', 15),
                    'grace_exit_min':    mt.get('grace_exit_minutes', 20),
                    'present_threshold': mt.get('present_threshold_pct', 80),
                    'partial_threshold': mt.get('partial_threshold_pct', 50),
                    'rollcall_count':    0,
                    'last_rollcall_at':  None,
                }
                log.info(f'Rebuilt meeting cache for chat_id={chat_id} after apparent restart')

        if chat_id not in active_meetings:
            await query.answer("This meeting has ended.", show_alert=True)
            return

    tg_uid    = query.from_user.id
    member_id = await asyncio.to_thread(get_member_id, tg_uid)
    if not member_id:
        await query.answer("You're not linked to a member account yet.", show_alert=True)
        return

    cache          = active_meetings[chat_id]
    rollcall_round = int(query.data.split(':')[1])
    await asyncio.to_thread(record_event, cache['meeting_id'], member_id, tg_uid, 'rollcall', rollcall_round=rollcall_round)
    await query.answer("✅ Marked present for this round!")

# ── Background jobs ──────────────────────────────────────────
async def post_rollcalls(context: ContextTypes.DEFAULT_TYPE):
    now = now_utc()
    for chat_id in list(GROUP_IDS.keys()):
        group_type = GROUP_IDS[chat_id]
        mt = await asyncio.to_thread(find_meeting_type, group_type, now)

        if not mt:
            # No active meeting window right now — if one was tracked, finalise it.
            if chat_id in active_meetings:
                await finalise_meeting(context, chat_id)
            continue

        if chat_id not in active_meetings:
            meeting, mt2 = await asyncio.to_thread(get_or_create_meeting, chat_id, group_type)
            if not meeting:
                continue
            active_meetings[chat_id] = {
                'meeting_id':        meeting['id'],
                'scheduled_start':   meeting['scheduled_start'],
                'scheduled_end':     meeting['scheduled_end'],
                'grace_join_min':    mt2.get('grace_join_minutes', 15),
                'grace_exit_min':    mt2.get('grace_exit_minutes', 20),
                'present_threshold': mt2.get('present_threshold_pct', 80),
                'partial_threshold': mt2.get('partial_threshold_pct', 50),
                'rollcall_count':    0,
                'last_rollcall_at':  None,
            }

        cache = active_meetings[chat_id]
        last  = cache['last_rollcall_at']
        if last is None or (now - last).total_seconds() >= ROLLCALL_INTERVAL_MINUTES * 60:
            cache['rollcall_count'] += 1
            cache['last_rollcall_at'] = now
            round_num = cache['rollcall_count']
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🙋 Still here", callback_data=f"rollcall:{round_num}")]])
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"Roll call #{round_num} — tap below if you're still here 👇",
                    reply_markup=keyboard,
                )
            except Exception as e:
                log.error(f'Failed to post roll-call in {chat_id}: {e}')

async def finalise_meeting(context: ContextTypes.DEFAULT_TYPE, chat_id):
    cache = active_meetings.pop(chat_id, None)
    if not cache:
        return
    now = now_utc()
    log.info(f'Finalising meeting {cache["meeting_id"]}')
    await asyncio.to_thread(sb_patch, 'meetings', {'id': f'eq.{cache["meeting_id"]}'}, {'status': 'ended', 'actual_end': now.isoformat()})

    evts = await asyncio.to_thread(sb_get, 'voice_events', {'meeting_id': f'eq.{cache["meeting_id"]}', 'select': 'member_id'})
    seen_members = {e['member_id'] for e in evts}
    for member_id in seen_members:
        await asyncio.to_thread(calculate_attendance, cache['meeting_id'], member_id, cache)

async def check_at_risk(context: ContextTypes.DEFAULT_TYPE):
    try:
        members = await asyncio.to_thread(sb_get, 'members', {'is_active': 'eq.true'})
        for m in members:
            recs = await asyncio.to_thread(sb_get, 'attendance_records', {'member_id': f'eq.{m["id"]}', 'order': 'calculated_at.desc', 'limit': '8'})
            if len(recs) < 3:
                continue
            present = sum(1 for r in recs if r['status'] == 'present')
            pct     = round((present / len(recs)) * 100)
            consec  = 0
            for r in recs:
                if r['status'] in ('absent', 'partial'):
                    consec += 1
                else:
                    break
            name = m['display_name']
            if consec == 1:
                await asyncio.to_thread(create_leader_alert, m['id'], 'missed_1', f'{name} missed their last meeting. Consider checking in with them.')
            elif consec == 2:
                await asyncio.to_thread(create_leader_alert, m['id'], 'missed_2', f'{name} has missed 2 consecutive meetings. They may need a follow-up.')
            elif consec >= 3:
                await asyncio.to_thread(create_leader_alert, m['id'], 'at_risk', f'{name} has missed {consec} meetings in a row and their attendance is at {pct}%. Please follow up personally.')
            if pct < 50 or consec >= 3:
                await asyncio.to_thread(sb_upsert, 'at_risk_members', {
                    'member_id': m['id'], 'attendance_pct_last8': pct, 'consecutive_absences': consec,
                    'flagged_at': now_utc().isoformat(), 'resolved': False,
                }, on_conflict='member_id')
    except Exception as e:
        log.error(f'At-risk check error: {e}', exc_info=True)

# ── Minimal health-check server for Render ─────────────────────
# Render's health check needs something responding on a port. This
# runs in a background thread so it doesn't interfere with the
# bot's own polling loop.
def start_health_check_server():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"status": "ok", "bot": "CAMGlobal Attendance Bot"}')

        def do_HEAD(self):
            # Some monitoring tools (and Render's own proxy) use HEAD
            # requests to check liveness without downloading a body.
            # Without this, those requests were rejected outright,
            # which could surface upstream as a 502.
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()

        def log_message(self, format, *args):
            pass  # suppress default request logging, keep our own logs clean

    port = int(os.environ.get('PORT', 8080))
    # ThreadingHTTPServer (not the plain single-threaded HTTPServer) so
    # an overlapping health check from Render and a monitoring service
    # like UptimeRobot can't block each other.
    server = ThreadingHTTPServer(('0.0.0.0', port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info(f'Health check server running on port {port}')

# ── Main ─────────────────────────────────────────────────────
def main():
    # Python 3.10+ no longer implicitly creates an event loop for the
    # main thread (Python 3.14 enforces this strictly). PTB's internal
    # run_polling() still calls asyncio.get_event_loop() expecting one
    # to already exist, so we create and register it explicitly first —
    # this makes it work regardless of which Python version is running.
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    start_health_check_server()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler('checkin', cmd_checkin))
    app.add_handler(CallbackQueryHandler(rollcall_button_tap, pattern=r'^rollcall:\d+$'))

    async def on_error(update, context):
        log.error(f'Unhandled exception: {context.error}', exc_info=context.error)

    app.add_error_handler(on_error)

    app.job_queue.run_repeating(post_rollcalls, interval=60, first=10)
    app.job_queue.run_repeating(check_at_risk, interval=86400, first=30)

    log.info('CAMGlobal Attendance Bot (official Bot API) starting...')
    app.run_polling()

if __name__ == '__main__':
    main()