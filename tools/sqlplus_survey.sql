-- SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
-- SPDX-License-Identifier: MIT
--
-- The sqlplus survey corpus (tools/sqlplus_survey.py, #2022). Each "prompt ###"
-- line starts a section, run in a sqlplus session of its own behind the lines
-- above the first one. Sections run in order and share the schema: 03 creates
-- the survey objects, 15 drops them.
set echo on
set linesize 200 pagesize 100 trimout on tab off
whenever sqlerror continue
prompt ### 01 select literals
select 1 n, 'abc' s, sysdate - sysdate d from dual;
prompt ### 02 types
select cast(1.5 as number(5,2)) n, cast('x' as char(3)) c, date '2024-01-02' d, timestamp '2024-01-02 03:04:05.123456' ts from dual;
select interval '2 03:04:05' day to second ids, interval '1-2' year to month iym from dual;
select hextoraw('DEADBEEF') r, cast(1.5 as binary_float) bf, cast(2.5 as binary_double) bd from dual;
select timestamp '2024-01-02 03:04:05 +02:00' tstz from dual;
select rowid from dual;
select n'unicode' ns, cast(null as varchar2(10)) nul from dual;
prompt ### 03 ddl
drop table sp_survey purge;
create table sp_survey (id number primary key, name varchar2(30), c clob, b blob, d date);
alter table sp_survey add (extra varchar2(10));
create index sp_survey_i on sp_survey(name);
comment on table sp_survey is 'survey';
prompt ### 04 dml
insert into sp_survey (id, name, d) values (1, 'one', sysdate);
insert into sp_survey (id, name) select level + 1, 'n' || level from dual connect by level <= 30;
update sp_survey set name = 'uno' where id = 1;
delete from sp_survey where id > 25;
commit;
select count(*) from sp_survey;
prompt ### 05 errors
insert into sp_survey (id) values (1);
select nosuch from dual;
select * from no_such_table;
create table sp_survey (x number);
select 1/0 from dual;
prompt ### 06 describe
describe sp_survey
desc dual
prompt ### 07 variables and exec
variable v number
variable s varchar2(30)
exec :v := 42
exec :s := 'hello'
print v s
select :v + 1 x, :s y from dual;
begin select count(*) into :v from sp_survey; end;
/
print v
prompt ### 08 refcursor
variable rc refcursor
begin open :rc for select id, name from sp_survey where id <= 3 order by id; end;
/
print rc
prompt ### 09 serveroutput
set serveroutput on
begin dbms_output.put_line('line one'); dbms_output.put_line('line two'); end;
/
set serveroutput off
prompt ### 10 lobs
update sp_survey set c = rpad('x', 5000, 'y'), b = hextoraw('0102') where id = 1;
set long 100
select c, b from sp_survey where id = 1;
select dbms_lob.getlength(c) from sp_survey where id = 1;
variable vc clob
exec :vc := 'clob var'
print vc
prompt ### 11 plsql objects
create or replace procedure sp_survey_p(a in number, b out number) as begin b := a * 2; end;
/
variable o number
exec sp_survey_p(21, :o)
print o
create or replace function sp_survey_f(a number) return number as begin return a + 1; end;
/
select sp_survey_f(1) from dual;
create or replace procedure sp_survey_bad as begin nosuch; end;
/
show errors
prompt ### 12 session
alter session set nls_date_format = 'YYYY-MM-DD';
select date '2024-05-06' from dual;
set autocommit on
insert into sp_survey (id, name) values (100, 'auto');
set autocommit off
rollback;
select count(*) from sp_survey where id = 100;
prompt ### 13 fetch sizes
set arraysize 1
select id from sp_survey where id <= 3 order by id;
set arraysize 5000
select count(*) from (select level from dual connect by level <= 3000);
set rowprefetch 10
select id from sp_survey where id <= 3 order by id;
prompt ### 14 show
show user
show release
select sys_context('USERENV','SESSION_USER') from dual;
prompt ### 15 cleanup
drop procedure sp_survey_p;
drop procedure sp_survey_bad;
drop function sp_survey_f;
drop table sp_survey purge;
exit
