"""
CAMGlobal Attendance Bot v3
- No supabase SDK (direct HTTP via requests)
- Client and handlers created inside main()
- Works on Render/Railway
"""

import os
import asyncio
import logging
import requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.types import UpdateGroupCallParticipants

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────
API_ID       = int(os.environ['TELEGRAM_API_ID'])
API_HASH     = os.environ['TELEGRAM_API_HASH']
SESSION_NAME = os.environ.get('SESSION_NAME', 'camglobal_bot')
SUPABASE_URL = os.environ['SUPABASE_URL'].rstrip('/')
SUPABASE_KEY = os.environ['SUPABASE_SERVICE_KEY']

GROUP_IDS = {
    -1001433101619: 'main',
    -5237009034:    'rise',
    -1002413746503: 'sons',
    -1001510684437: 'family',
}

active_meetings: dict = {}

# ── Supabase HTTP helpers ────────────────────────────────────
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
        log.info(f'No meeting matched for {group_type} at {now.strftime("%H:%M")}')
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

def get_or_create_member(tg_user_id, display_name, first_name='', last_name=''):
    # Only track members already linked in Supabase (cam_visitor, cam_sons, cam_family)
    # Never auto-create — unlinked visitors are ignored
    rows = sb_get('members', {'telegram_user_id': f'eq.{tg_user_id}'})
    if rows:
        return rows[0]['id']
    log.info(f'Ignoring unlinked user: {display_name} ({tg_user_id})')
    return None

def record_voice_event(meeting_id, member_id, tg_user_id, event_type):
    sb_post('voice_events', {
        'meeting_id':       meeting_id,
        'member_id':        member_id,
        'telegram_user_id': tg_user_id,
        'event_type':       event_type,
        'event_time':       now_utc().isoformat(),
    })
    log.info(f'{event_type.upper()} | {member_id[:8]}')

def calculate_attendance(meeting_id, member_id, cache):
    now  = now_utc()
    evts = sb_get('voice_events', {
        'meeting_id': f'eq.{meeting_id}',
        'member_id':  f'eq.{member_id}',
        'order':      'event_time.asc',
    })
    if not evts:
        return

    joins  = [e for e in evts if e['event_type'] == 'join']
    leaves = [e for e in evts if e['event_type'] == 'leave']
    if not joins:
        return

    def parse_dt(s):
        return datetime.fromisoformat(s.replace('Z', '+00:00'))

    first_join = parse_dt(joins[0]['event_time'])
    last_leave = parse_dt(leaves[-1]['event_time']) if leaves else None
    s_start    = parse_dt(cache['scheduled_start'])
    s_end      = parse_dt(cache['scheduled_end'])
    eff_leave  = last_leave or now

    duration_min = max(0, int((eff_leave - first_join).total_seconds() / 60))
    meeting_min  = max(1, int((s_end - s_start).total_seconds() / 60))
    pct          = min(100, round((duration_min / meeting_min) * 100))

    grace_join     = s_start + timedelta(minutes=cache['grace_join_min'])
    grace_exit     = s_end   - timedelta(minutes=cache['grace_exit_min'])
    joined_on_time = first_join <= grace_join
    stayed_to_end  = last_leave is None or last_leave >= grace_exit
    auto_marked    = joined_on_time and stayed_to_end and pct >= cache['present_threshold']
    status         = 'present' if auto_marked else ('partial' if pct >= cache['partial_threshold'] else 'absent')

    sb_upsert('attendance_records', {
        'meeting_id':             meeting_id,
        'member_id':              member_id,
        'telegram_user_id':       joins[0].get('telegram_user_id'),
        'first_join_at':          first_join.isoformat(),
        'last_leave_at':          last_leave.isoformat() if last_leave else None,
        'total_duration_minutes': duration_min,
        'attendance_pct':         pct,
        'status':                 status,
        'auto_marked':            auto_marked,
        'calculated_at':          now.isoformat(),
    }, on_conflict='meeting_id,member_id')

    if not auto_marked:
        att_rows = sb_get('attendance_records', {
            'meeting_id': f'eq.{meeting_id}',
            'member_id':  f'eq.{member_id}',
        })
        if att_rows:
            att_id   = att_rows[0]['id']
            in_queue = sb_get('attendance_review_queue', {
                'attendance_record_id': f'eq.{att_id}',
                'review_status':        'eq.pending',
            })
            if not in_queue:
                reason = (
                    f'{"On time" if joined_on_time else "Late"}, '
                    f'{"stayed to end" if stayed_to_end else "left early"}, '
                    f'{pct}% ({duration_min}/{meeting_min} min)'
                )
                sb_post('attendance_review_queue', {
                    'attendance_record_id': att_id,
                    'meeting_id':           meeting_id,
                    'member_id':            member_id,
                    'suggested_status':     status,
                    'reason':               reason,
                    'confidence':           round(pct / 100, 2),
                })
    log.info(f'Attendance: {status} | {pct}% | auto={auto_marked}')

# ── Background tasks ─────────────────────────────────────────
async def check_endings():
    while True:
        await asyncio.sleep(300)
        try:
            now = now_utc()
            for chat_id, cache in list(active_meetings.items()):
                s_end = datetime.fromisoformat(cache['scheduled_end'].replace('Z', '+00:00'))
                if now > s_end + timedelta(minutes=10):
                    log.info(f'Finalising meeting {chat_id}')
                    sb_patch('meetings', {'id': f'eq.{cache["meeting_id"]}'}, {
                        'status': 'ended', 'actual_end': now.isoformat()
                    })
                    recs = sb_get('attendance_records', {'meeting_id': f'eq.{cache["meeting_id"]}'})
                    for r in recs:
                        calculate_attendance(cache['meeting_id'], r['member_id'], cache)
                    del active_meetings[chat_id]
        except Exception as e:
            log.error(f'Ending check error: {e}', exc_info=True)

def create_leader_alert(member_id, alert_type, message, meeting_id=None):
    """Write an alert to the leader_alerts table in Supabase."""
    try:
        # Check if same alert already exists unread in last 7 days
        existing = sb_get('leader_alerts', {
            'member_id':  f'eq.{member_id}',
            'alert_type': f'eq.{alert_type}',
            'is_read':    'eq.false',
        })
        if existing:
            return  # Don't duplicate unread alerts
        data = {
            'member_id':  member_id,
            'alert_type': alert_type,
            'message':    message,
            'is_read':    False,
            'created_at': now_utc().isoformat(),
        }
        if meeting_id:
            data['meeting_id'] = meeting_id
        sb_post('leader_alerts', data)
        log.info(f'Leader alert created: {alert_type} for {member_id}')
    except Exception as e:
        log.error(f'Failed to create leader alert: {e}')


async def check_at_risk():
    while True:
        await asyncio.sleep(86400)
        try:
            members = sb_get('members', {'is_active': 'eq.true'})
            for m in members:
                recs = sb_get('attendance_records', {
                    'member_id': f'eq.{m["id"]}',
                    'order':     'calculated_at.desc',
                    'limit':     '8',
                })
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

                # Consecutive absence alerts
                if consec == 1:
                    create_leader_alert(
                        m['id'], 'missed_1',
                        f'{name} missed their last meeting. Consider checking in with them.'
                    )
                elif consec == 2:
                    create_leader_alert(
                        m['id'], 'missed_2',
                        f'{name} has missed 2 consecutive meetings. They may need a follow-up.'
                    )
                elif consec >= 3:
                    create_leader_alert(
                        m['id'], 'at_risk',
                        f'{name} has missed {consec} meetings in a row and their attendance is at {pct}%. Please follow up personally.'
                    )

                # At-risk flag
                if pct < 50 or consec >= 3:
                    sb_upsert('at_risk_members', {
                        'member_id':            m['id'],
                        'attendance_pct_last8': pct,
                        'consecutive_absences': consec,
                        'flagged_at':           now_utc().isoformat(),
                        'resolved':             False,
                    }, on_conflict='member_id')
                    log.info(f'At-risk: {name} | {pct}% | {consec} absences')

        except Exception as e:
            log.error(f'At-risk check error: {e}', exc_info=True)

# ── Main ─────────────────────────────────────────────────────
async def main():
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

    # Register handlers inside main so client exists
    @client.on(events.Raw(UpdateGroupCallParticipants))
    async def on_voice(event):
        try:
            chat_id = None
            if hasattr(event, 'call') and hasattr(event.call, 'id'):
                for gid in GROUP_IDS:
                    try:
                        full = await client(GetFullChannelRequest(gid))
                        if full.full_chat.call and full.full_chat.call.id == event.call.id:
                            chat_id = gid
                            break
                    except Exception:
                        continue

            if not chat_id or chat_id not in GROUP_IDS:
                return

            group_type = GROUP_IDS[chat_id]

            if chat_id not in active_meetings:
                meeting, mt = get_or_create_meeting(chat_id, group_type)
                if not meeting or not mt:
                    return
                active_meetings[chat_id] = {
                    'meeting_id':        meeting['id'],
                    'scheduled_start':   meeting['scheduled_start'],
                    'scheduled_end':     meeting['scheduled_end'],
                    'grace_join_min':    mt.get('grace_join_minutes', 15),
                    'grace_exit_min':    mt.get('grace_exit_minutes', 20),
                    'present_threshold': mt.get('present_threshold_pct', 80),
                    'partial_threshold': mt.get('partial_threshold_pct', 50),
                }

            cache      = active_meetings[chat_id]
            meeting_id = cache['meeting_id']

            for p in event.participants:
                tg_uid = getattr(p.peer, 'user_id', None)
                if not tg_uid:
                    continue
                event_type = 'leave' if p.left else 'join'
                try:
                    user = await client.get_entity(tg_uid)
                    name = f'{user.first_name or ""} {user.last_name or ""}'.strip()
                    fn, ln = user.first_name or '', user.last_name or ''
                except Exception:
                    name, fn, ln = f'User {tg_uid}', '', ''

                member_id = get_or_create_member(tg_uid, name, fn, ln)
                if not member_id:
                    continue
                record_voice_event(meeting_id, member_id, tg_uid, event_type)
                calculate_attendance(meeting_id, member_id, cache)

        except Exception as e:
            log.error(f'Voice handler error: {e}', exc_info=True)

    log.info('Starting CAMGlobal Attendance Bot...')
    await client.start()
    log.info('Connected to Telegram.')

    for chat_id in GROUP_IDS:
        try:
            await client.get_entity(chat_id)
            log.info(f'Monitoring: {chat_id}')
        except Exception as e:
            log.warning(f'Cannot access {chat_id}: {e}')

    asyncio.create_task(check_endings())
    asyncio.create_task(check_at_risk())
    log.info('Bot running. Listening for voice chat events...')
    await client.run_until_disconnected()

if __name__ == '__main__':
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()
