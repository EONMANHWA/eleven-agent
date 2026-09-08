-- Optional audit log ONLY. Run in your own Supabase project's SQL editor.
-- No email addresses, chat IDs, passwords, cookies, session links, or API keys.
create table if not exists public.bot_events (
  id bigint generated always as identity primary key,
  created_at timestamptz not null default now(),
  event text not null check (event in (
    'session_link_created', 'browser_opened', 'browser_closed'
  ))
);
alter table public.bot_events enable row level security;
revoke all on public.bot_events from anon, authenticated;
revoke all on sequence public.bot_events_id_seq from anon, authenticated;
grant insert on public.bot_events to service_role;
grant usage on sequence public.bot_events_id_seq to service_role;
-- No client policies: browsers and Telegram users have no table access.
-- The service-role key stays in Render environment variables ONLY.
-- Delete old audit events whenever you choose:
-- delete from public.bot_events where created_at < now() - interval '30 days';
