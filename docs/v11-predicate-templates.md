# v11 Predicate-Placement Templates

Target: focused correction set for v10's live failure mode (2026-10-07).

## The failure

Asked: *"List every customer ID, name, and total spending for orders whose
status is completed. Include customers with no completed orders with
spending of zero."*

v10 emitted:

```sql
SELECT customers.id, customers.name, COALESCE(SUM(orders.total), 0) AS total_spending
FROM customers LEFT JOIN orders ON customers.id = orders.customer_id
WHERE orders.status = 'completed'   -- BUG: runs after the join, kills the outer join
GROUP BY customers.id, customers.name
ORDER BY total_spending DESC, customers.id ASC;
```

The `WHERE` filters *after* the join, silently converting the `LEFT JOIN`
into an inner join — customers with no completed orders vanish instead of
showing 0. The predicate belongs in the `ON` clause (or the aggregation
must be conditional).

## Categories (stealth/forge.py: predicate_placement_candidates)

1. **predicate_on_clause** — the fix: `LEFT JOIN ... ON ... AND child.status = '<v>'`
   with `COALESCE(SUM(...), 0)`. Reference: same shape, swapped aliases,
   `SUM(COALESCE(...))` form, ordinal ORDER BY.
2. **predicate_conditional_agg** — equivalent via
   `SUM(CASE WHEN status='<v>' THEN total ELSE 0 END)` over a plain
   `LEFT JOIN`. Cross-teaches the two correct forms.
3. **predicate_anti_join** — "parents with no `<v>` children" via
   `NOT EXISTS`. Reference: `LEFT JOIN ... WHERE child.id IS NULL` form.

Scaled across status values (up to 3 per status-like column) and numeric
columns (up to 2) per parent/child relationship. Child detection uses the
schema's declared `relationships` — the old plural-name heuristic
(`"customers" in "customer_id"`) never matched.

Verified locally: all (sql, reference_sql) pairs return identical rows on
seed data, and the buggy WHERE form demonstrably drops the zero-rows.
