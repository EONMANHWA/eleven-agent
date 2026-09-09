-- Configure the three named Vault secrets privately before running this file:
-- eleven_bot_wakeup_url, eleven_bot_wakeup_secret, eleven_bot_owner_id.
create extension if not exists pg_cron;
create extension if not exists pg_net with schema extensions;

create or replace function public.bot_wake_due()
returns bigint language plpgsql security definer set search_path = '' as $$
declare request_id bigint; owner_value bigint;
begin
 select decrypted_secret::bigint into owner_value from vault.decrypted_secrets where name='eleven_bot_owner_id';
 delete from public.eleven_bot_jobs where owner_id=owner_value and expires_at<now()
  and (lease_until is null or lease_until<now());
 if exists(select 1 from public.eleven_bot_jobs where owner_id=owner_value and state='pending'
   and expires_at>now() and due_at<=now() and (lease_until is null or lease_until<now())) then
  select net.http_post(
   url := (select decrypted_secret from vault.decrypted_secrets where name='eleven_bot_wakeup_url'),
   body := '{"reason":"due_job"}'::jsonb,
   headers := jsonb_build_object('Content-Type','application/json','X-Job-Wakeup-Secret',
    (select decrypted_secret from vault.decrypted_secrets where name='eleven_bot_wakeup_secret')),
   timeout_milliseconds := 10000) into request_id;
 end if;
 return request_id;
end $$;
revoke all on function public.bot_wake_due() from public,anon,authenticated;
grant execute on function public.bot_wake_due() to service_role;
revoke all on vault.decrypted_secrets from public,anon,authenticated;
-- This checks for due work, NOT a continuous health ping. Completed/idle jobs send no requests.
select cron.schedule('eleven-bot-due-jobs','* * * * *','select public.bot_wake_due();');
