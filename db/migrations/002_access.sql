-- Roles, grants, row policies, private bucket and storage policies.
-- Passwords are set out of band (infra/supabase_setup.py); none live in this file.

do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'prepress_worker') then create role prepress_worker login; end if;
  if not exists (select 1 from pg_roles where rolname = 'prepress_dispatcher') then create role prepress_dispatcher login; end if;
  if not exists (select 1 from pg_roles where rolname = 'prepress_app') then create role prepress_app login; end if;
  if not exists (select 1 from pg_roles where rolname = 'prepress_observer') then create role prepress_observer login; end if;
end $$;

revoke all on schema prepress from public;
revoke all on all functions in schema prepress from public;
grant usage on schema prepress to prepress_worker, prepress_dispatcher, prepress_app, prepress_observer, authenticated;

-- Worker: claim, report, commit. Nothing else.
grant execute on function prepress.claim_job(uuid, text, int, int, text, text, int),
  prepress.mark_engine_started(uuid),
  prepress.commit_result(uuid, uuid, jsonb, jsonb),
  prepress.fail_attempt(uuid, uuid, text, boolean) to prepress_worker;

-- Dispatcher: read the outbox, mark it, reconcile.
grant execute on function prepress.pending_dispatch(int),
  prepress.mark_dispatched(bigint, text),
  prepress.jobs_to_reconcile(int),
  prepress.reconcile_job(uuid, boolean) to prepress_dispatcher;

-- Application backend: record a verified paid order (job + dispatch intent, one transaction).
grant execute on function prepress.record_paid_order(text, uuid, text, text, jsonb, int),
  prepress.resubmit_job(uuid) to prepress_app;

-- Read-only observer used to collect evidence.
grant select on all tables in schema prepress to prepress_observer;

-- Signed-in users (staff and customers) go through row policies.
grant execute on function prepress.has_role(text), prepress.customer_can_read(text),
  prepress.approve_file_version(uuid) to authenticated;
grant select on prepress.orders, prepress.file_versions to authenticated;

alter table prepress.orders enable row level security;
alter table prepress.file_versions enable row level security;
alter table prepress.jobs enable row level security;
alter table prepress.job_attempts enable row level security;
alter table prepress.job_results enable row level security;
alter table prepress.input_files enable row level security;
alter table prepress.dispatch_outbox enable row level security;
alter table prepress.deliveries enable row level security;
alter table prepress.members enable row level security;

create policy staff_read_orders on prepress.orders for select to authenticated
  using (prepress.has_role('staff'));
create policy customer_read_own_order on prepress.orders for select to authenticated
  using (customer_id = auth.uid());
create policy staff_read_versions on prepress.file_versions for select to authenticated
  using (prepress.has_role('staff'));
create policy customer_read_approved on prepress.file_versions for select to authenticated
  using (status = 'approved' and exists (
    select 1 from prepress.orders o where o.id = order_id and o.customer_id = auth.uid()));
create policy observer_all on prepress.orders for select to prepress_observer using (true);
create policy observer_all on prepress.file_versions for select to prepress_observer using (true);
create policy observer_all on prepress.jobs for select to prepress_observer using (true);
create policy observer_all on prepress.job_attempts for select to prepress_observer using (true);
create policy observer_all on prepress.job_results for select to prepress_observer using (true);
create policy observer_all on prepress.input_files for select to prepress_observer using (true);
create policy observer_all on prepress.dispatch_outbox for select to prepress_observer using (true);
create policy observer_all on prepress.deliveries for select to prepress_observer using (true);

-- Private bucket.
insert into storage.buckets (id, name, public) values ('prepress-files', 'prepress-files', false)
  on conflict (id) do update set public = false;

create policy prepress_backend_writes_inputs on storage.objects for insert to authenticated
  with check (bucket_id = 'prepress-files' and (storage.foldername(name))[1] = 'inputs'
              and prepress.has_role('backend'));
create policy prepress_worker_reads_inputs on storage.objects for select to authenticated
  using (bucket_id = 'prepress-files' and (storage.foldername(name))[1] = 'inputs'
         and prepress.has_role('worker'));
create policy prepress_worker_writes_outputs on storage.objects for insert to authenticated
  with check (bucket_id = 'prepress-files' and (storage.foldername(name))[1] = 'outputs'
              and prepress.has_role('worker'));
create policy prepress_staff_reads_all on storage.objects for select to authenticated
  using (bucket_id = 'prepress-files' and prepress.has_role('staff'));
create policy prepress_customer_reads_approved on storage.objects for select to authenticated
  using (bucket_id = 'prepress-files' and prepress.customer_can_read(name));
