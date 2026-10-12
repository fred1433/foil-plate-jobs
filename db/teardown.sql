-- Remove everything 001 and 002 created. Run after the bucket has been emptied
-- through the Storage API (infra/supabase_teardown.py does both).
drop policy if exists prepress_backend_writes_inputs on storage.objects;
drop policy if exists prepress_worker_reads_inputs on storage.objects;
drop policy if exists prepress_worker_writes_outputs on storage.objects;
drop policy if exists prepress_staff_reads_all on storage.objects;
drop policy if exists prepress_customer_reads_approved on storage.objects;
drop schema if exists prepress cascade;
drop role if exists prepress_worker;
drop role if exists prepress_dispatcher;
drop role if exists prepress_app;
drop role if exists prepress_observer;
