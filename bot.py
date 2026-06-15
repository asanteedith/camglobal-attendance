"""
CAMGlobal Attendance Bot
Monitors Telegram voice/video chats and tracks attendance automatically.
Deployed on Railway.
"""

import os
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.types import (
    UpdateGroupCallParticipants,
    MessageMediaPoll,
)
from supabase import create_client, Client

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
log = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────
API_ID       = int(os.environ['TELEGRAM_API_ID'])
API_HASH     = os.environ['TELEGRAM_API_HASH']
SESSION_NAME = os.environ.get('SESSION_NAME', 'camglobal_bot')
SUPABASE_URL = os.environ['SUPABASE_URL']
SUPABASE_KEY = os.environ['SUPABASE_SERVICE_KEY']  # service role key on Railway

# CAMGlobal Telegram group IDs
GROUP_IDS = {
    -1001433101619: 'main',
    -5237009034:    'rise',
    -1002413746503: 'sons',
    -1001510684437: 'family',
}

# ── Supabase client ──────────────────────────────────────────
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Telethon client ──────────────────────────────────────────
client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

# ── In-memory active meetings cache ─────────────────────────
# { telegram_chat_id: { meeting_id, scheduled_start, scheduled_end,
#                       grace_join_min, grace_exit_min,
#                       present_threshold, partial_threshold } }
active_meetings: dict = {}


# ════════════════════════════════════════════════════════════
# MEETING MANAGEMENT
# ════════════════════════════════════════════════════════════

def get_current_utc() -> datetime:
    return datetime.now(timezone.utc)


def find_meeting_type(group_type: str, now: datetime) -> dict | None:
    """Find which meeting type matches the current time for a group."""
    dow = now.weekday()  # 0=Mon … 6=Sun
    time_now = now.time().replace(second=0, microsecond=0)

    res = supabase.table('meeting_types').select('*').eq(
        'group_type', group_type
    ).eq('is_active', True).execute()

    for mt in res.data:
        days = mt.get('day_of_week') or []
        if dow not in days:
            continue
        start = datetime.strptime(mt['start_time'], '%H:%M:%S').time()
        end   = datetime.strptime(mt['end_time'],   '%H:%M:%S').time()

        # Handle meetings that cross midnight (e.g. Sons Session 23:00–02:00)
        if end < start:
            if time_now >= start or time_now <= end:
                return mt
        else:
            if start <= time_now <= end:
                return mt
    return None


def get_or_create_meeting(chat_id: int, group_type: str) -> dict | None:
    """Get or create a meeting record for the current session."""
    now = get_current_utc()
    mt  = find_meeting_type(group_type, now)
    if not mt:
        log.info(f'No meeting type matched for group {group_type} at {now.time()}')
        return None

    # Get telegram_group_id
    grp = supabase.table('telegram_groups').select('id').eq(
        'telegram_chat_id', chat_id
    ).single().execute()
    if not grp.data:
        log.warning(f'Group {chat_id} not found in telegram_groups')
        return None
    tg_group_id = grp.data['id']

    # Build scheduled window
    start_t = datetime.strptime(mt['start_time'], '%H:%M:%S').time()
    end_t   = datetime.strptime(mt['end_time'],   '%H:%M:%S').time()
    s_start = now.replace(hour=start_t.hour, minute=start_t.minute, second=0, microsecond=0)
    s_end   = now.replace(hour=end_t.hour,   minute=end_t.minute,   second=0, microsecond=0)

    # If end is before start (crosses midnight), end is next day
    if s_end < s_start:
        s_end += timedelta(days=1)

    # Check if meeting already exists for today
    existing = supabase.table('meetings').select('*').eq(
        'meeting_type_id', mt['id']
    ).eq('telegram_group_id', tg_group_id).gte(
        'scheduled_start', s_start.isoformat()
    ).lte('scheduled_start', (s_start + timedelta(hours=1)).isoformat()).execute()

    if existing.data:
        meeting = existing.data[0]
        # Update status to live if scheduled
        if meeting['status'] == 'scheduled':
            supabase.table('meetings').update({
                'status': 'live',
                'actual_start': now.isoformat()
            }).eq('id', meeting['id']).execute()
        return meeting

    # Create new meeting
    new_meeting = supabase.table('meetings').insert({
        'meeting_type_id':  mt['id'],
        'telegram_group_id': tg_group_id,
        'title':            mt['name'],
        'scheduled_start':  s_start.isoformat(),
        'scheduled_end':    s_end.isoformat(),
        'actual_start':     now.isoformat(),
        'status':           'live',
    }).execute()

    log.info(f'Created meeting: {mt["name"]} for group {group_type}')
    return new_meeting.data[0] if new_meeting.data else None


def cache_meeting(chat_id: int, meeting: dict, mt: dict):
    """Cache active meeting details for fast lookup."""
    active_meetings[chat_id] = {
        'meeting_id':         meeting['id'],
        'scheduled_start':    meeting['scheduled_start'],
        'scheduled_end':      meeting['scheduled_end'],
        'grace_join_min':     mt.get('grace_join_minutes', 15),
        'grace_exit_min':     mt.get('grace_exit_minutes', 20),
        'present_threshold':  mt.get('present_threshold_pct', 80),
        'partial_threshold':  mt.get('partial_threshold_pct', 50),
    }


# ════════════════════════════════════════════════════════════
# MEMBER LOOKUP
# ════════════════════════════════════════════════════════════

def get_or_create_member(telegram_user_id: int, display_name: str,
                          first_name: str = '', last_name: str = '') -> str | None:
    """Get member UUID by Telegram user ID, create if new."""
    res = supabase.table('members').select('id').eq(
        'telegram_user_id', telegram_user_id
    ).execute()

    if res.data:
        return res.data[0]['id']

    # Create new member
    new_m = supabase.table('members').insert({
        'telegram_user_id': telegram_user_id,
        'display_name':     display_name or f'User {telegram_user_id}',
        'first_name':       first_name,
        'last_name':        last_name,
    }).execute()

    if new_m.data:
        log.info(f'New member created: {display_name} ({telegram_user_id})')
        return new_m.data[0]['id']
    return None


# ════════════════════════════════════════════════════════════
# VOICE EVENT RECORDING
# ════════════════════════════════════════════════════════════

def record_voice_event(meeting_id: str, member_id: str,
                        telegram_user_id: int, event_type: str):
    """Record a join or leave event."""
    supabase.table('voice_events').insert({
        'meeting_id':       meeting_id,
        'member_id':        member_id,
        'telegram_user_id': telegram_user_id,
        'event_type':       event_type,
        'event_time':       get_current_utc().isoformat(),
    }).execute()
    log.info(f'Voice event: {event_type} | member {member_id} | meeting {meeting_id}')


# ════════════════════════════════════════════════════════════
# ATTENDANCE CALCULATION
# ════════════════════════════════════════════════════════════

def calculate_and_save_attendance(meeting_id: str, member_id: str,
                                   meeting_cache: dict):
    """Calculate attendance status and save/update attendance record."""
    now = get_current_utc()

    # Get all voice events for this member in this meeting
    events_res = supabase.table('voice_events').select('*').eq(
        'meeting_id', meeting_id
    ).eq('member_id', member_id).order('event_time').execute()

    events_data = events_res.data
    if not events_data:
        return

    # Find first join and last leave
    joins  = [e for e in events_data if e['event_type'] == 'join']
    leaves = [e for e in events_data if e['event_type'] == 'leave']

    if not joins:
        return

    first_join  = datetime.fromisoformat(joins[0]['event_time'])
    last_leave  = datetime.fromisoformat(leaves[-1]['event_time']) if leaves else None

    # Parse scheduled times
    s_start = datetime.fromisoformat(meeting_cache['scheduled_start'])
    s_end   = datetime.fromisoformat(meeting_cache['scheduled_end'])

    # If no leave recorded yet, use current time for live calculation
    effective_leave = last_leave or now

    # Duration
    duration_min = max(0, int((effective_leave - first_join).total_seconds() / 60))
    meeting_min  = max(1, int((s_end - s_start).total_seconds() / 60))
    pct          = min(100, round((duration_min / meeting_min) * 100))

    # Grace windows
    grace_join = s_start + timedelta(minutes=meeting_cache['grace_join_min'])
    grace_exit = s_end   - timedelta(minutes=meeting_cache['grace_exit_min'])

    # Determine status
    joined_on_time  = first_join <= grace_join
    stayed_to_end   = (last_leave is None) or (last_leave >= grace_exit)
    above_present   = pct >= meeting_cache['present_threshold']
    above_partial   = pct >= meeting_cache['partial_threshold']

    if joined_on_time and stayed_to_end and above_present:
        status     = 'present'
        auto_marked = True
    elif above_partial:
        status     = 'partial'
        auto_marked = False
    else:
        status     = 'absent'
        auto_marked = False

    # Upsert attendance record
    existing = supabase.table('attendance_records').select('id').eq(
        'meeting_id', meeting_id
    ).eq('member_id', member_id).execute()

    record_data = {
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
    }

    if existing.data:
        supabase.table('attendance_records').update(record_data).eq(
            'id', existing.data[0]['id']
        ).execute()
    else:
        supabase.table('attendance_records').insert(record_data).execute()

    # If not auto-marked, add to admin review queue
    if not auto_marked:
        att_res = supabase.table('attendance_records').select('id').eq(
            'meeting_id', meeting_id
        ).eq('member_id', member_id).single().execute()

        if att_res.data:
            # Check if already in queue
            in_queue = supabase.table('attendance_review_queue').select('id').eq(
                'attendance_record_id', att_res.data['id']
            ).eq('review_status', 'pending').execute()

            if not in_queue.data:
                reason = (
                    f'Joined {"on time" if joined_on_time else "late"}, '
                    f'{"stayed to end" if stayed_to_end else "left early"}, '
                    f'{pct}% attendance ({duration_min} min of {meeting_min} min)'
                )
                supabase.table('attendance_review_queue').insert({
                    'attendance_record_id': att_res.data['id'],
                    'meeting_id':           meeting_id,
                    'member_id':            member_id,
                    'suggested_status':     status,
                    'reason':               reason,
                    'confidence':           round(pct / 100, 2),
                }).execute()
                log.info(f'Added to review queue: {member_id} | {status} | {reason}')

    log.info(
        f'Attendance: {member_id} | {status} | {pct}% | '
        f'auto={auto_marked} | {duration_min}min'
    )


# ════════════════════════════════════════════════════════════
# TELEGRAM EVENT HANDLERS
# ════════════════════════════════════════════════════════════

@client.on(events.Raw(UpdateGroupCallParticipants))
async def on_voice_participant(event):
    """Fires when someone joins or leaves a Telegram voice/video chat."""
    try:
        chat_id = None

        # Try to get chat_id from the event
        if hasattr(event, 'call') and hasattr(event.call, 'id'):
            # Look up which chat this call belongs to
            for gid in GROUP_IDS:
                try:
                    full = await client(GetFullChannelRequest(gid))
                    if (full.full_chat.call and
                            full.full_chat.call.id == event.call.id):
                        chat_id = gid
                        break
                except Exception:
                    continue

        if chat_id is None or chat_id not in GROUP_IDS:
            return

        group_type = GROUP_IDS[chat_id]

        # Get or create meeting
        if chat_id not in active_meetings:
            meeting = get_or_create_meeting(chat_id, group_type)
            if not meeting:
                return
            # Get meeting type for cache
            mt_res = supabase.table('meeting_types').select('*').eq(
                'id', meeting['meeting_type_id']
            ).single().execute()
            if mt_res.data:
                cache_meeting(chat_id, meeting, mt_res.data)

        if chat_id not in active_meetings:
            return

        meeting_cache = active_meetings[chat_id]
        meeting_id    = meeting_cache['meeting_id']

        # Process each participant update
        for participant in event.participants:
            tg_user_id = participant.peer.user_id if hasattr(
                participant.peer, 'user_id'
            ) else None

            if not tg_user_id:
                continue

            # Determine join or leave
            if participant.left:
                event_type = 'leave'
            else:
                event_type = 'join'

            # Get user info
            try:
                user = await client.get_entity(tg_user_id)
                display_name = f'{user.first_name or ""} {user.last_name or ""}'.strip()
                first_name   = user.first_name or ''
                last_name    = user.last_name  or ''
            except Exception:
                display_name = f'User {tg_user_id}'
                first_name   = ''
                last_name    = ''

            # Get or create member
            member_id = get_or_create_member(
                tg_user_id, display_name, first_name, last_name
            )
            if not member_id:
                continue

            # Record event
            record_voice_event(meeting_id, member_id, tg_user_id, event_type)

            # Recalculate attendance on every event
            calculate_and_save_attendance(meeting_id, member_id, meeting_cache)

    except Exception as e:
        log.error(f'Error in voice participant handler: {e}', exc_info=True)


@client.on(events.NewMessage(chats=list(GROUP_IDS.keys())))
async def on_message(event):
    """Track message engagement during meetings."""
    try:
        chat_id = event.chat_id
        if chat_id not in active_meetings:
            return

        meeting_id = active_meetings[chat_id]['meeting_id']
        sender     = await event.get_sender()
        if not sender or not hasattr(sender, 'id'):
            return

        tg_user_id = sender.id
        member_res = supabase.table('members').select('id').eq(
            'telegram_user_id', tg_user_id
        ).execute()
        if not member_res.data:
            return

        member_id  = member_res.data[0]['id']
        event_type = 'poll_vote' if isinstance(
            event.message.media, MessageMediaPoll
        ) else 'message'

        supabase.table('engagement_events').insert({
            'meeting_id':       meeting_id,
            'member_id':        member_id,
            'telegram_user_id': tg_user_id,
            'event_type':       event_type,
            'event_time':       get_current_utc().isoformat(),
        }).execute()

    except Exception as e:
        log.error(f'Error in message handler: {e}', exc_info=True)


# ════════════════════════════════════════════════════════════
# MEETING END — finalise attendance
# ════════════════════════════════════════════════════════════

async def check_meeting_endings():
    """
    Background task — runs every 5 minutes.
    Finalises attendance when a meeting's scheduled end time passes.
    """
    while True:
        await asyncio.sleep(300)  # every 5 minutes
        try:
            now = get_current_utc()
            for chat_id, cache in list(active_meetings.items()):
                s_end = datetime.fromisoformat(cache['scheduled_end'])
                if now > s_end + timedelta(minutes=10):
                    log.info(f'Meeting ended for chat {chat_id} — finalising attendance')

                    # Mark meeting as ended
                    supabase.table('meetings').update({
                        'status':     'ended',
                        'actual_end': now.isoformat(),
                    }).eq('id', cache['meeting_id']).execute()

                    # Final attendance calculation for all members
                    records = supabase.table('attendance_records').select(
                        'member_id'
                    ).eq('meeting_id', cache['meeting_id']).execute()

                    for rec in records.data:
                        calculate_and_save_attendance(
                            cache['meeting_id'], rec['member_id'], cache
                        )

                    # Remove from active cache
                    del active_meetings[chat_id]
                    log.info(f'Meeting finalised and removed from cache: {chat_id}')

        except Exception as e:
            log.error(f'Error in meeting end check: {e}', exc_info=True)


# ════════════════════════════════════════════════════════════
# AT-RISK DETECTION — runs daily
# ════════════════════════════════════════════════════════════

async def check_at_risk_members():
    """Check for members with low attendance over last 8 meetings."""
    while True:
        await asyncio.sleep(86400)  # every 24 hours
        try:
            log.info('Running at-risk member check...')
            members_res = supabase.table('members').select(
                'id, display_name'
            ).eq('is_active', True).execute()

            for member in members_res.data:
                mid = member['id']

                # Get last 8 attendance records
                recs = supabase.table('attendance_records').select(
                    'status'
                ).eq('member_id', mid).order(
                    'calculated_at', desc=True
                ).limit(8).execute()

                if len(recs.data) < 3:
                    continue

                present_count = sum(
                    1 for r in recs.data if r['status'] == 'present'
                )
                pct = round((present_count / len(recs.data)) * 100)

                # Count consecutive absences
                consecutive = 0
                for r in recs.data:
                    if r['status'] in ('absent', 'partial'):
                        consecutive += 1
                    else:
                        break

                if pct < 50 or consecutive >= 3:
                    # Upsert at-risk record
                    existing = supabase.table('at_risk_members').select(
                        'id'
                    ).eq('member_id', mid).execute()

                    data = {
                        'member_id':             mid,
                        'attendance_pct_last8':  pct,
                        'consecutive_absences':  consecutive,
                        'flagged_at':            get_current_utc().isoformat(),
                        'resolved':              False,
                    }

                    if existing.data:
                        supabase.table('at_risk_members').update(data).eq(
                            'member_id', mid
                        ).execute()
                    else:
                        supabase.table('at_risk_members').insert(data).execute()

                    log.info(
                        f'At-risk: {member["display_name"]} | '
                        f'{pct}% | {consecutive} consecutive absences'
                    )

        except Exception as e:
            log.error(f'Error in at-risk check: {e}', exc_info=True)


# ════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════

async def main():
    log.info('Starting CAMGlobal Attendance Bot...')
    await client.start()
    log.info('Bot connected to Telegram.')

    # Join all monitored groups to receive events
    for chat_id in GROUP_IDS:
        try:
            await client.get_entity(chat_id)
            log.info(f'Monitoring group: {chat_id}')
        except Exception as e:
            log.warning(f'Could not access group {chat_id}: {e}')

    # Start background tasks
    asyncio.create_task(check_meeting_endings())
    asyncio.create_task(check_at_risk_members())

    log.info('Bot is running. Listening for voice chat events...')
    await client.run_until_disconnected()


if __name__ == '__main__':
    asyncio.run(main())
