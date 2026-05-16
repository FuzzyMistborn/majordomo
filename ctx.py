from contextvars import ContextVar

# Set to the reminder_id currently being executed as a smart reminder.
# Tools read this to exclude the firing reminder from briefing output.
firing_reminder_id: ContextVar[int | None] = ContextVar("firing_reminder_id", default=None)
