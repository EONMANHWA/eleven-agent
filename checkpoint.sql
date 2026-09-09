-- Dedicated free Supabase project. No passwords, emails or keys are stored in plaintext.
create table if not exists public.eleven_bot_jobs (
  owner_id bigint primary key,
  version bigint not null,
  payload text not null,
  state text not null check (state in ('pending','done','cancelled')),
  due_at timestamptz not null,
  expires_at timestamptz not null,
  lease_token uuid,
  lease_until timestamptz,
  cancel_requested boolean not null default false,
  updated_at timestamptz not null default now()
);
alter table public.eleven_bot_jobs enable row level security;
revoke all on public.eleven_bot_jobs from public, anon, authenticated;
grant all on public.eleven_bot_jobs to service_role;

create or replace function public.bot_job_get(p_owner bigint)
returns jsonb language sql security definer set search_path = '' as $$
 select to_jsonb(j) from public.eleven_bot_jobs j where owner_id=p_owner;
$$;
create or replace function public.bot_job_claim(p_owner bigint, p_token uuid)
returns jsonb language sql security definer set search_path = '' as $$
 update public.eleven_bot_jobs j set lease_token=p_token, lease_until=now()+interval '120 seconds'
 where owner_id=p_owner and (lease_until is null or lease_until<now() or lease_token=p_token)
 returning to_jsonb(j);
$$;
create or replace function public.bot_job_save(p_owner bigint,p_token uuid,p_version bigint,
 p_payload text,p_state text,p_due timestamptz,p_expires timestamptz,p_new boolean default false)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare result jsonb;
begin
 if p_version=0 then
  insert into public.eleven_bot_jobs(owner_id,version,payload,state,due_at,expires_at,lease_token,lease_until)
  values(p_owner,1,p_payload,p_state,p_due,p_expires,p_token,now()+interval '120 seconds')
  on conflict do nothing returning jsonb_build_object('version',version,'cancel_requested',cancel_requested) into result;
 else
  update public.eleven_bot_jobs j set version=j.version+1,payload=p_payload,state=p_state,due_at=p_due,
   expires_at=p_expires,lease_until=now()+interval '120 seconds',updated_at=now(),
   cancel_requested=case when p_new then false else j.cancel_requested end
  where owner_id=p_owner and version=p_version and lease_token=p_token and lease_until>now()
   and (not p_new or state in ('done','cancelled')) returning jsonb_build_object('version',j.version,'cancel_requested',j.cancel_requested) into result;
 end if;
 return result;
end $$;
create or replace function public.bot_job_renew(p_owner bigint,p_token uuid)
returns jsonb language sql security definer set search_path = '' as $$
 update public.eleven_bot_jobs j set lease_until=now()+interval '120 seconds'
 where owner_id=p_owner and lease_token=p_token and lease_until>now() returning jsonb_build_object('cancel_requested',j.cancel_requested);
$$;
create or replace function public.bot_job_release(p_owner bigint,p_token uuid)
returns boolean language plpgsql security definer set search_path = '' as $$
begin
 update public.eleven_bot_jobs set lease_token=null,lease_until=null where owner_id=p_owner and lease_token=p_token;
 return found;
end $$;
create or replace function public.bot_job_cancel(p_owner bigint)
returns boolean language plpgsql security definer set search_path = '' as $$
begin
 update public.eleven_bot_jobs set cancel_requested=true,due_at=now() where owner_id=p_owner and state='pending';
 return found;
end $$;
create or replace function public.bot_job_forget(p_owner bigint,p_token uuid)
returns boolean language plpgsql security definer set search_path = '' as $$
begin
 delete from public.eleven_bot_jobs where owner_id=p_owner and lease_token=p_token and state in ('done','cancelled');
 return found;
end $$;

-- RPCs are server-only. Never grant these to browser/client roles.
revoke all on function public.bot_job_get(bigint) from public,anon,authenticated;
revoke all on function public.bot_job_claim(bigint,uuid) from public,anon,authenticated;
revoke all on function public.bot_job_save(bigint,uuid,bigint,text,text,timestamptz,timestamptz,boolean) from public,anon,authenticated;
revoke all on function public.bot_job_renew(bigint,uuid) from public,anon,authenticated;
revoke all on function public.bot_job_release(bigint,uuid) from public,anon,authenticated;
revoke all on function public.bot_job_cancel(bigint) from public,anon,authenticated;
revoke all on function public.bot_job_forget(bigint,uuid) from public,anon,authenticated;
grant execute on function public.bot_job_get(bigint),public.bot_job_claim(bigint,uuid),
 public.bot_job_save(bigint,uuid,bigint,text,text,timestamptz,timestamptz,boolean),
 public.bot_job_renew(bigint,uuid),public.bot_job_release(bigint,uuid),public.bot_job_cancel(bigint),
 public.bot_job_forget(bigint,uuid) to service_role;
