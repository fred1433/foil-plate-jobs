-- Fixes from a fresh review of the recorded deployment. Applied after the cloud
-- recording; proven by tests/test_ledger.py on a local Postgres.

-- 1. The reconciler never declares a live attempt lost. A task can disappear while
--    its attempt is alive (a duplicate delivery of the same task is acknowledged as
--    `busy`, which deletes the task), so "task gone" alone proves nothing until the
--    attempt's lease has expired.
create or replace function prepress.reconcile_job(p_job uuid, p_task_exists boolean)
returns text
language plpgsql security definer set search_path = prepress, pg_temp as $$
declare jb jobs; a job_attempts; nseq int;
begin
  select * into jb from jobs where id = p_job for update;
  if jb.status not in ('queued', 'retrying', 'running') then return 'settled'; end if;
  if jb.status = 'running' then
    select * into a from job_attempts where id = jb.current_attempt_id;
    if a.lease_until > now() then return 'in_flight'; end if;   -- whatever the task's state
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

-- 2. A committed attempt is never rewritten. Replaying a commit returns `committed`;
--    a late error report for a committed attempt changes nothing.
create or replace function prepress.commit_result(
  p_job uuid, p_token uuid, p_manifest jsonb, p_files jsonb)
returns text
language plpgsql security definer set search_path = prepress, pg_temp as $$
declare jb jobs; a job_attempts; fv jsonb;
begin
  select * into jb from jobs where id = p_job for update;
  select * into a from job_attempts where token = p_token;
  if found and a.outcome = 'committed' then return 'committed'; end if;
  if not found or jb.status <> 'running' or jb.current_token is distinct from p_token then
    update job_attempts set outcome = 'late_result_rejected', finished_at = coalesce(finished_at, now()),
      error = coalesce(error || '; ', '') || 'result arrived after this attempt lost its claim; not registered'
      where token = p_token and outcome is distinct from 'committed';
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
  if exists (select 1 from job_attempts where token = p_token and outcome = 'committed') then
    return 'already_committed';
  end if;
  select * into jb from jobs where id = p_job for update;
  if jb.status <> 'running' or jb.current_token is distinct from p_token then
    update job_attempts set outcome = 'late_result_rejected', finished_at = coalesce(finished_at, now()),
      error = coalesce(error || '; ', '') || 'error report arrived after this attempt lost its claim: ' || p_error
      where token = p_token and outcome is distinct from 'committed';
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

-- 3. An input version is immutable: reusing a storage path with different bytes is refused.
create or replace function prepress.record_paid_order(
  p_order_ref text, p_customer uuid, p_input_path text, p_input_sha text,
  p_params jsonb default '{}'::jsonb, p_deadline_s int default 540)
returns table (job_id uuid, created boolean)
language plpgsql security definer set search_path = prepress, pg_temp as $$
declare o uuid; f input_files; k text; j uuid;
begin
  insert into orders (order_ref, customer_id) values (p_order_ref, p_customer)
    on conflict (order_ref) do update set order_ref = excluded.order_ref returning id into o;
  insert into input_files (order_id, storage_path, sha256) values (o, p_input_path, p_input_sha)
    on conflict (storage_path) do nothing returning * into f;
  if f.id is null then
    select * into f from input_files where storage_path = p_input_path;
    if f.sha256 <> p_input_sha then
      raise exception 'input path % already holds a different version (sha256 %)', p_input_path, f.sha256;
    end if;
  end if;
  k := encode(sha256(convert_to(o::text || '|foil_plate|' || p_input_sha || '|' || p_params::text, 'UTF8')), 'hex');
  insert into jobs (order_id, input_file_id, params, idempotency_key)
    values (o, f.id, p_params, k) on conflict (idempotency_key) do nothing returning id into j;
  if j is null then
    select id into j from jobs where idempotency_key = k;
    return query select j, false;
    return;
  end if;
  insert into dispatch_outbox (job_id, seq, dispatch_deadline_s) values (j, 1, p_deadline_s);
  return query select j, true;
end $$;

-- 4. The outbox records the dispatch deadline the task was actually created with.
alter table prepress.dispatch_outbox add column if not exists effective_dispatch_deadline_s int;

create or replace function prepress.mark_dispatched_with_deadline(p_outbox_id bigint, p_task text, p_deadline_s int)
returns void language sql security definer set search_path = prepress, pg_temp as $$
  update dispatch_outbox set dispatched_at = coalesce(dispatched_at, now()), task_name = p_task,
    effective_dispatch_deadline_s = p_deadline_s
  where id = p_outbox_id;
$$;

revoke all on function prepress.mark_dispatched_with_deadline(bigint, text, int) from public;
grant execute on function prepress.mark_dispatched_with_deadline(bigint, text, int) to prepress_dispatcher;
