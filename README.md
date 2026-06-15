# CAMGlobal Attendance Bot

Automated attendance tracking system for Christ Ambassadors Ministries International.

Monitors Telegram voice and video meetings, records join and leave events, calculates attendance automatically, and sends partial and absent cases to leadership for review.

---

## How It Works

```
Telegram Meeting (Voice / Video / Live)
        ↓
Telethon Listener (this bot)
        ↓
Supabase Database
        ↓
WordPress Admin Dashboard
        ↓
Leadership Review and Approval
```

When a member joins a Telegram voice or video meeting the bot records the exact join time. When they leave it records the exit time. After the meeting ends it calculates each member's attendance percentage and marks them automatically if they qualify as fully present. Partial and absent cases go into a review queue for admin approval.

---

## Meetings Tracked

| Meeting | Group | Day | Time (GMT) | Frequency |
|---|---|---|---|---|
| Midnight Prayer | Main | Daily | 12:00am – 1:30am | Every day |
| Makers Word Live | Main | Wednesday | 8:30pm – 10:00pm | Weekly |
| Bible Study | Main | Thursday | 8:30pm – 10:30pm | Weekly |
| RISE Weekly | RISE | Sunday & Monday | 8:30pm – 9:30pm | Weekly |
| Family Meeting | Family | Thursday | 8:30pm – 10:00pm | Weekly |
| Sons Standup | Sons | Monday & Friday | 9:30pm – 10:00pm | Weekly |
| Sons Session | Sons | Saturday | 11:00pm – 2:00am | Every 2 weeks |

---

## Attendance Rules

| Status | Condition | Admin Action |
|---|---|---|
| Present | Joined within 15 min + stayed to end + 80%+ attendance | Auto-marked |
| Partial | Attended 50% or more but not fully present | Admin reviews |
| Absent | Below 50% or did not join | Admin reviews |

---

## Tech Stack

- **Python** with Telethon for Telegram MTProto
- **Supabase** for the database
- **OpenAI** for weekly and monthly AI reports
- **Railway** for 24/7 bot hosting
- **WordPress** for member and admin dashboards

---

## Files

```
bot.py              Main bot — listens to voice events and records attendance
requirements.txt    Python dependencies
railway.toml        Railway deployment config
.env.example        Environment variables template
```

---

## Environment Variables

Set these in Railway under Variables:

| Variable | Description |
|---|---|
| `TELEGRAM_API_ID` | From my.telegram.org |
| `TELEGRAM_API_HASH` | From my.telegram.org |
| `SESSION_NAME` | Any name e.g. camglobal_bot |
| `SUPABASE_URL` | Your Supabase project URL |
| `SUPABASE_SERVICE_KEY` | Supabase service role key — keep private |
| `OPENAI_API_KEY` | Your OpenAI API key |

---

## Deployment

1. Fork or clone this repository
2. Go to railway.app and create a new project from this repo
3. Add all environment variables under Variables
4. Deploy — Railway will install dependencies and start the bot automatically
5. First run will prompt for Telegram authentication — check Railway logs and enter the code sent to your phone

---

## Database

The bot connects to a Supabase database with these tables:

- `members` — links Telegram user IDs to WordPress accounts
- `telegram_groups` — the 4 monitored groups
- `meeting_types` — meeting schedules and attendance rules
- `meetings` — individual meeting instances
- `voice_events` — raw join and leave events
- `attendance_records` — calculated attendance per member per meeting
- `attendance_review_queue` — partial and absent cases for admin review
- `correction_requests` — member-submitted corrections
- `engagement_events` — messages and poll activity
- `ai_reports` — weekly and monthly leadership reports
- `at_risk_members` — members flagged for follow-up

---

## At-Risk Detection

The bot runs a daily check and flags any member who:
- Has attended less than 50% of their last 8 meetings, or
- Has 3 or more consecutive absences

Flagged members appear in the admin dashboard for leadership follow-up.

---

## Ministry

Christ Ambassadors Ministries International (CAMGlobal)  
theambassadorsofchristministries.com
