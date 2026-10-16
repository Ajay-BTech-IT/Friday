-- Reminders can be delivered by email (to the owner's own address only; see google_svc/send.py).
DO $$
DECLARE c text;
BEGIN
    FOR c IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'reminders'::regclass AND contype = 'c'
               AND pg_get_constraintdef(oid) LIKE '%channel%' LOOP
        EXECUTE format('ALTER TABLE reminders DROP CONSTRAINT %I', c);
    END LOOP;
    ALTER TABLE reminders ADD CONSTRAINT reminders_channel_check
        CHECK (channel IN ('push','voice','in_app','email'));
END $$;
