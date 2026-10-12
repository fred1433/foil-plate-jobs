-- Job ledger for the prepress worker. Everything lives in its own schema.
create schema if not exists prepress;

create table prepress.members (
  user_id uuid primary key,
  role text not null check (role in ('staff', 'backend', 'worker', 'customer'))
);

create table prepress.orders (
  id uuid primary key default gen_random_uuid(),
  order_ref text not null unique,
  customer_id uuid not null,
  paid_at timestamptz not null default now(),
  -- What the customer currently sees. Jobs never write this column; only staff approval does.
  customer_file_version_id uuid,
  created_at timestamptz not null default now()
);

create table prepress.input_files (
  id uuid primary key default gen_random_uuid(),
  order_id uuid not null references prepress.orders(id),
  storage_path text not null unique,
  sha256 text not null,
  created_at timestamptz not null default now()
);

create table prepress.jobs (
  id uuid primary key default gen_random_uuid(),
  order_id uuid not null references prepress.orders(id),
  job_type text not null default 'foil_plate',
  input_file_id uuid not null references prepress.input_files(id),
  params jsonb not null default '{}'::jsonb,
  -- The requested work: order, input version, job type and parameters.
  idempotency_key text not null unique,
  status text not null default 'queued'
    check (status in ('queued', 'running', 'retrying', 'review', 'failed')),
  attempt_count int not null default 0,
  max_attempts int not null default 2,
  current_attempt_id bigint,
  current_token uuid,
  lease_until timestamptz,
  last_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  finished_at timestamptz
);

-- Durable dispatch intent, written in the same transaction as the job.
create table prepress.dispatch_outbox (
  id bigserial primary key,
  job_id uuid not null references prepress.jobs(id),
  seq int not null,
  dispatch_deadline_s int not null default 540,
  created_at timestamptz not null default now(),
  dispatched_at timestamptz,
  task_name text,
  unique (job_id, seq)
);

-- One row per execution attempt that reached the worker.
create table prepress.job_attempts (
  id bigserial primary key,
  job_id uuid not null references prepress.jobs(id),
  attempt_no int not null,
  token uuid not null unique default gen_random_uuid(),
  task_name text,
  task_retry_count int,
  task_execution_count int,
  instance_id text,
  revision text,
  started_at timestamptz not null default now(),
  engine_started_at timestamptz,
  lease_until timestamptz not null,
  finished_at timestamptz,
  outcome text check (outcome in ('committed', 'error', 'lost', 'superseded', 'late_result_rejected')),
  error text,
  unique (job_id, attempt_no)
);

-- Every delivery the handler received, including the ones it declined.
create table prepress.deliveries (
  id bigserial primary key,
  job_id uuid,
  task_name text,
  task_retry_count int,
  task_execution_count int,
  instance_id text,
  revision text,
  decision text not null,
  received_at timestamptz not null default now()
);

create table prepress.file_versions (
  id uuid primary key default gen_random_uuid(),
  job_id uuid not null references prepress.jobs(id),
  order_id uuid not null references prepress.orders(id),
  attempt_id bigint not null references prepress.job_attempts(id),
  kind text not null,
  storage_path text not null unique,
  sha256 text not null,
  bytes bigint not null,
  status text not null default 'pending_review'
    check (status in ('pending_review', 'approved', 'rejected')),
  reviewed_by uuid,
  reviewed_at timestamptz,
  created_at timestamptz not null default now(),
  unique (job_id, kind)
);

create table prepress.job_results (
  job_id uuid primary key references prepress.jobs(id),
  attempt_id bigint not null references prepress.job_attempts(id),
  manifest jsonb not null,
  committed_at timestamptz not null default now()
);

create index on prepress.jobs (status);
create index on prepress.dispatch_outbox (dispatched_at) where dispatched_at is null;
create index on prepress.job_attempts (job_id);
create index on prepress.deliveries (job_id);

-- ---------------------------------------------------------------- functions
-- All writes go through these functions; worker and dispatcher roles can call
-- nothing else.

create or replace function prepress.record_paid_order(
  p_order_ref text, p_customer uuid, p_input_path text, p_input_sha text,
  p_params jsonb default '{}'::jsonb, p_deadline_s int default 540)
returns table (job_id uuid, created boolean)
language plpgsql security definer set search_path = prepress, pg_temp as $$
declare o uuid; f uuid; k text; j uuid;
begin
  insert into orders (order_ref, customer_id) values (p_order_ref, p_customer)
    on conflict (order_ref) do update set order_ref = excluded.order_ref returning id into o;
  insert into input_files (order_id, storage_path, sha256) values (o, p_input_path, p_input_sha)
    on conflict (storage_path) do update set storage_path = excluded.storage_path returning id into f;
  k := encode(sha256(convert_to(o::text || '|foil_plate|' || p_input_sha || '|' || p_params::text, 'UTF8')), 'hex');
  insert into jobs (order_id, input_file_id, params, idempotency_key)
    values (o, f, p_params, k) on conflict (idempotency_key) do nothing returning id into j;
  if j is null then
    select id into j from jobs where idempotency_key = k;
    return query select j, false;
    return;
  end if;
  insert into dispatch_outbox (job_id, seq, dispatch_deadline_s) values (j, 1, p_deadline_s);
  return query select j, true;
end $$;

create or replace function prepress.claim_job(
  p_job uuid, p_task text, p_retry int, p_exec int, p_instance text, p_revision text, p_lease_s int)
returns table (decision text, attempt_id bigint, token uuid, attempt_no int, params jsonb,
               input_path text, input_sha text, max_attempts int)
language plpgsql security definer set search_path = prepress, pg_temp as $$
declare jb jobs; a job_attempts; na job_attempts; d text; f input_files;
begin
  select * into jb from jobs where id = p_job for update;
  if not found then
    d := 'unknown_job';
  elsif jb.status = 'review' then
    d := 'already_committed';
  elsif jb.status = 'failed' then
    d := 'already_failed';
  else
    if jb.status = 'running' then
      select * into a from job_attempts where id = jb.current_attempt_id;
      if a.lease_until > now() and not (a.task_name = p_task and p_retry > a.task_retry_count) then
        d := 'busy';
      else
        update job_attempts set outcome = 'superseded', finished_at = now(),
          error = case when a.lease_until > now()
                       then 'a later delivery of the same task took over before this attempt reported'
                       else 'lease expired before this attempt reported' end
          where id = a.id and outcome is null;
      end if;
    end if;
    if d is null then
      if jb.attempt_count >= jb.max_attempts then
        update jobs set status = 'failed', finished_at = now(), updated_at = now(),
          current_token = null, lease_until = null,
          last_error = 'worker disappeared without a final result (attempt '
                       || jb.attempt_count || ' of ' || jb.max_attempts || ')'
          where id = p_job;
        d := 'attempts_exhausted';
      else
        insert into job_attempts (job_id, attempt_no, task_name, task_retry_count, task_execution_count,
                                  instance_id, revision, lease_until)
          values (p_job, jb.attempt_count + 1, p_task, p_retry, p_exec, p_instance, p_revision,
                  now() + make_interval(secs => p_lease_s))
          returning * into na;
        update jobs set status = 'running', attempt_count = attempt_count + 1,
          current_attempt_id = na.id, current_token = na.token, lease_until = na.lease_until,
          updated_at = now()
          where id = p_job;
        d := 'run';
      end if;
    end if;
  end if;
  insert into deliveries (job_id, task_name, task_retry_count, task_execution_count, instance_id, revision, decision)
    values (p_job, p_task, p_retry, p_exec, p_instance, p_revision, d);
  if d = 'run' then
    select * into f from input_files where id = jb.input_file_id;
    return query select d, na.id, na.token, na.attempt_no, jb.params, f.storage_path, f.sha256, jb.max_attempts;
  else
    return query select d, null::bigint, null::uuid, null::int, null::jsonb, null::text, null::text, null::int;
  end if;
end $$;

create or replace function prepress.mark_engine_started(p_token uuid)
returns void language sql security definer set search_path = prepress, pg_temp as $$
  update job_attempts set engine_started_at = now() where token = p_token;
$$;

create or replace function prepress.commit_result(
  p_job uuid, p_token uuid, p_manifest jsonb, p_files jsonb)
returns text
language plpgsql security definer set search_path = prepress, pg_temp as $$
declare jb jobs; a job_attempts; fv jsonb;
begin
  select * into jb from jobs where id = p_job for update;
  select * into a from job_attempts where token = p_token;
  if not found or jb.status <> 'running' or jb.current_token is distinct from p_token then
    update job_attempts set outcome = 'late_result_rejected', finished_at = coalesce(finished_at, now()),
      error = coalesce(error || '; ', '') || 'result arrived after this attempt lost its claim; not registered'
      where token = p_token;
    return 'rejected_stale';
  end if;
  for fv in select * from jsonb_array_elements(p_files) loop
    insert into file_versions (job_id, order_id, attempt_id, kind, storage_path, sha256, bytes)
      values (p_job, jb.order_id, a.id, fv->>'kind', fv->>'path', fv->>'sha256', (fv->>'bytes')::bigint);
  end loop;
  insert into job_results (job_id, attempt_id, manifest) values (p_job, a.id, p_manifest);
  update job_attempts set outcome = 'committed', finished_at = now() where id = a.id;
  update jobs set status = 'review', finished_at = now(), updated_at = now(), current_token = null,
    lease_until = null, last_error = null where id = p_job;
  return 'committed';
end $$;

create or replace function prepress.fail_attempt(p_job uuid, p_token uuid, p_error text, p_final boolean)
returns text
language plpgsql security definer set search_path = prepress, pg_temp as $$
declare jb jobs;
begin
  select * into jb from jobs where id = p_job for update;
  if jb.status <> 'running' or jb.current_token is distinct from p_token then
    update job_attempts set outcome = 'late_result_rejected', finished_at = coalesce(finished_at, now()),
      error = coalesce(error || '; ', '') || 'error report arrived after this attempt lost its claim: ' || p_error
      where token = p_token;
    return 'rejected_stale';
  end if;
  update job_attempts set outcome = 'error', error = p_error, finished_at = now() where token = p_token;
  if p_final or jb.attempt_count >= jb.max_attempts then
    update jobs set status = 'failed', last_error = p_error || ' (attempt ' || jb.attempt_count
      || ' of ' || jb.max_attempts || ')', finished_at = now(), updated_at = now(),
      current_token = null, lease_until = null where id = p_job;
    return 'failed';
  end if;
  update jobs set status = 'retrying', last_error = p_error, updated_at = now(),
    current_token = null, lease_until = null where id = p_job;
  return 'retrying';
end $$;

-- Dispatcher side -------------------------------------------------------------

create or replace function prepress.pending_dispatch(p_limit int)
returns table (outbox_id bigint, job_id uuid, seq int, dispatch_deadline_s int, params jsonb)
language sql security definer set search_path = prepress, pg_temp as $$
  select o.id, o.job_id, o.seq, o.dispatch_deadline_s, j.params
  from dispatch_outbox o join jobs j on j.id = o.job_id
  where o.dispatched_at is null order by o.id limit p_limit;
$$;

create or replace function prepress.mark_dispatched(p_outbox_id bigint, p_task text)
returns void language sql security definer set search_path = prepress, pg_temp as $$
  update dispatch_outbox set dispatched_at = coalesce(dispatched_at, now()), task_name = p_task
  where id = p_outbox_id;
$$;

create or replace function prepress.jobs_to_reconcile(p_grace_s int)
returns table (job_id uuid, status text, task_name text, attempt_count int, max_attempts int)
language sql security definer set search_path = prepress, pg_temp as $$
  select j.id, j.status, o.task_name, j.attempt_count, j.max_attempts
  from jobs j
  join lateral (select * from dispatch_outbox x where x.job_id = j.id order by seq desc limit 1) o on true
  where j.status in ('queued', 'retrying', 'running')
    and o.dispatched_at is not null
    and o.dispatched_at < now() - make_interval(secs => p_grace_s);
$$;

create or replace function prepress.reconcile_job(p_job uuid, p_task_exists boolean)
returns text
language plpgsql security definer set search_path = prepress, pg_temp as $$
declare jb jobs; a job_attempts; nseq int;
begin
  select * into jb from jobs where id = p_job for update;
  if jb.status not in ('queued', 'retrying', 'running') then return 'settled'; end if;
  if jb.status = 'running' then
    select * into a from job_attempts where id = jb.current_attempt_id;
    if p_task_exists and a.lease_until > now() then return 'in_flight'; end if;
    update job_attempts set outcome = 'lost', finished_at = now(),
      error = 'worker disappeared without a final result'
      where id = a.id and outcome is null;
    if p_task_exists then
      update jobs set status = 'retrying', current_token = null, lease_until = null,
        last_error = 'worker disappeared without a final result', updated_at = now() where id = p_job;
      return 'lease_expired_waiting_for_retry';
    end if;
  elsif p_task_exists then
    return 'waiting_for_delivery';
  end if;
  -- The task is gone and the job has no final result.
  if jb.status = 'queued' and jb.attempt_count = 0 then
    update jobs set status = 'failed', finished_at = now(), updated_at = now(),
      last_error = 'delivery ended before any attempt reached the worker' where id = p_job;
    return 'failed_never_started';
  end if;
  if jb.attempt_count >= jb.max_attempts then
    update jobs set status = 'failed', finished_at = now(), updated_at = now(), current_token = null,
      lease_until = null,
      last_error = case when jb.status = 'running'
        then 'worker disappeared without a final result (attempt ' || jb.attempt_count || ' of ' || jb.max_attempts || ')'
        else coalesce(jb.last_error, 'no final result') || ' (no further delivery)' end
      where id = p_job;
    return 'failed_attempts_exhausted';
  end if;
  select coalesce(max(seq), 0) + 1 into nseq from dispatch_outbox where job_id = p_job;
  insert into dispatch_outbox (job_id, seq) values (p_job, nseq);
  update jobs set status = 'retrying', current_token = null, lease_until = null, updated_at = now()
    where id = p_job;
  return 'requeued';
end $$;

-- Staff ------------------------------------------------------------------------

create or replace function prepress.resubmit_job(p_job uuid)
returns void language plpgsql security definer set search_path = prepress, pg_temp as $$
declare nseq int;
begin
  update jobs set status = 'queued', max_attempts = attempt_count + 2, last_error = null,
    finished_at = null, updated_at = now() where id = p_job and status = 'failed';
  if not found then raise exception 'only failed jobs can be resubmitted'; end if;
  select coalesce(max(seq), 0) + 1 into nseq from dispatch_outbox where job_id = p_job;
  insert into dispatch_outbox (job_id, seq) values (p_job, nseq);
end $$;

create or replace function prepress.approve_file_version(p_version uuid)
returns void language plpgsql security definer set search_path = prepress, pg_temp as $$
begin
  if not exists (select 1 from members where user_id = auth.uid() and role = 'staff') then
    raise exception 'staff only';
  end if;
  update file_versions set status = 'approved', reviewed_by = auth.uid(), reviewed_at = now()
    where id = p_version;
  update orders o set customer_file_version_id = p_version
    from file_versions fv where fv.id = p_version and o.id = fv.order_id and fv.kind = 'proof_png';
end $$;

-- Access helpers used by row and storage policies.
create or replace function prepress.has_role(p_role text)
returns boolean language sql stable security definer set search_path = prepress, pg_temp as $$
  select exists (select 1 from members where user_id = auth.uid() and role = p_role);
$$;

create or replace function prepress.customer_can_read(p_path text)
returns boolean language sql stable security definer set search_path = prepress, pg_temp as $$
  select exists (
    select 1 from file_versions fv join orders o on o.id = fv.order_id
    where fv.storage_path = p_path and fv.status = 'approved' and o.customer_id = auth.uid());
$$;
