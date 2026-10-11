"""Индексы под горячие запросы бота есть в схеме (миграции их создают)."""

from sqlalchemy import text

EXPECTED = {
    "ix_access_grants_active_user",
    "ix_employee_departments_dept",
    "ix_telegram_chats_dept",
    "ix_tariffs_lookup",
    "ix_work_entries_dept_date",
    "ix_work_entries_emp_date",
    "ix_work_entries_open",
    "ix_work_entries_confirmed_volume",
    "ix_ledger_employee",
    "ix_ledger_employee_period",
    "ix_ledger_employee_date",
    "ix_ledger_period",
    "ix_ledger_dept_date",
    "ix_ledger_reverses",
    "ix_adjustments_open",
    "ix_adjustments_outstanding",
    "ix_adjustments_employee",
    "ix_payouts_employee",
    "ix_payouts_registered",
    "ix_payout_requests_open",
    "ix_batches_fifo",
    "uq_stock_balances_no_batch",
    "uq_stock_balances_batch",
    "ix_stock_balances_product",
    "ix_stock_balances_location_product",
    "ix_stock_moves_document",
    "ix_stock_moves_product",
    "ix_stock_moves_posted_at",
    "ix_stock_documents_in_transit",
    "ix_stock_document_lines_document",
    "ix_discrepancy_cases_open",
    "ix_locations_employee",
    "ix_fsm_states_updated",
    "ix_audit_entity",
}


async def test_expected_indexes_exist(session):
    rows = (await session.execute(text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"))).scalars()
    missing = EXPECTED - set(rows)
    assert not missing, missing


async def test_hot_queries_use_indexes(session):
    """На пустых таблицах планировщик выбрал бы seq scan, поэтому отключаем его и проверяем, что индекс подходит."""
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    queries = {
        "ix_work_entries_open": "SELECT id FROM work_entries WHERE department_id = 1 AND status = 'pending'",
        "ix_ledger_employee_period": "SELECT sum(amount) FROM ledger_entries WHERE employee_id = 1 AND period_id = 2",
        "ix_adjustments_outstanding": (
            "SELECT id FROM adjustments WHERE employee_id = 1 AND currency = 'USD' AND status = 'approved' "
            "AND kind <> 'bonus' AND withheld < amount"
        ),
        "ix_stock_balances_location_product": "SELECT qty FROM stock_balances WHERE location_id = 1 AND product_id = 2",
    }
    for index, sql in queries.items():
        plan = "\n".join((await session.execute(text("EXPLAIN " + sql))).scalars())
        assert index in plan, (index, plan)
