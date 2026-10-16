"""Tool registry: every function the agent may call, collected in one place."""
from tools.briefing import get_briefing
from tools.calls import call_contact
from tools.db_access import db_describe, db_insert, db_select, db_update
from tools.gmail import (compose_draft, draft_reply, edit_draft, list_drafts, list_mail, read_email, reject_draft,
                         request_send_draft, search_mail, sync_mail_now)
from tools.meals import correct_meal, food_nutrition, get_day_summary, log_meal, set_nutrition_goal
from tools.memory import recall, remember, retire_memory
from tools.persona import create_persona, list_personas, list_voices, set_language, switch_persona, update_persona
from tools.profile import get_profile, update_profile
from tools.reminder import (complete_task, create_routine, create_task, list_routines, list_tasks,
                            snooze_task, update_routine, update_task, upcoming_reminders)
from tools.scheduler import cancel_event, create_calendar_event, list_events, move_event

TOOLS = [
    # profile + persona
    get_profile, update_profile,
    # persona: switch / create / rename / retune / voice (all stored in the personas table)
    switch_persona, create_persona, update_persona, list_personas, list_voices, set_language,
    # tasks, routines, reminders
    create_task, list_tasks, complete_task, snooze_task, update_task,
    create_routine, list_routines, update_routine, upcoming_reminders,
    # calendar
    create_calendar_event, list_events, move_event, cancel_event,
    # meals
    log_meal, get_day_summary, set_nutrition_goal, correct_meal, food_nutrition,
    # memory
    remember, recall, retire_memory,
    # gmail (read + draft only; no send tool exists)
    sync_mail_now, list_mail, search_mail, read_email, draft_reply, compose_draft, edit_draft, reject_draft, request_send_draft, list_drafts,
    # phone actions (executed by the mobile app)
    call_contact,
    # briefings
    get_briefing,
    # generic database access: select / insert / update (no delete)
    db_describe, db_select, db_insert, db_update,
]
