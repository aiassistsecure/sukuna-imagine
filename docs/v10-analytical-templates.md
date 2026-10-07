# v10 Analytical Query Templates

Target: ~1,370 new pairs (50% of 2,739) focused on complex analytical queries.
Based on v9 failure: LEFT JOIN + GROUP BY + COALESCE + multi-clause ORDER BY.

## Category 1: Aggregation with LEFT JOIN (300 pairs)
Pattern: Include all parent rows, aggregate child data, handle nulls.

Template:
```sql
-- Tables: {parent}(id, name, ...); {child}(id, {parent}_id, amount, status)
-- "List every {parent} with total {child} amount where status='{status}'.
--  Include {parent}s with no matching {child}s (show 0). Sort by total desc, id asc."
SELECT p.id, p.name, COALESCE(SUM(c.amount), 0) AS total
FROM {parent} p
LEFT JOIN {child} c ON p.id = c.{parent}_id AND c.status = '{status}'
GROUP BY p.id, p.name
ORDER BY total DESC, p.id ASC;
```

Variations:
- COUNT instead of SUM
- AVG with ROUND(..., 2)
- Multiple JOIN conditions
- WHERE on parent + JOIN on child

## Category 2: GROUP BY with HAVING (250 pairs)
Pattern: Filter groups after aggregation.

Template:
```sql
-- "Find {entity}s with more than {n} {child}s. Show id, name, count. Sort by count desc."
SELECT p.id, p.name, COUNT(c.id) AS cnt
FROM {parent} p
JOIN {child} c ON p.id = c.{parent}_id
GROUP BY p.id, p.name
HAVING COUNT(c.id) > {n}
ORDER BY cnt DESC;
```

Variations:
- HAVING SUM > threshold
- HAVING AVG < threshold
- Multiple HAVING conditions (AND/OR)

## Category 3: Subqueries (250 pairs)
Pattern: Nested SELECT for filtering or comparison.

Template:
```sql
-- "Find {entity}s whose total exceeds the average total."
SELECT id, name, total
FROM (
  SELECT p.id, p.name, SUM(c.amount) AS total
  FROM {parent} p
  JOIN {child} c ON p.id = c.{parent}_id
  GROUP BY p.id, p.name
) sub
WHERE total > (SELECT AVG(total) FROM (
  SELECT SUM(amount) AS total FROM {child} GROUP BY {parent}_id
) avg_sub)
ORDER BY total DESC;
```

Variations:
- IN subquery
- EXISTS subquery
- Correlated subquery

## Category 4: Window Functions (200 pairs)
Pattern: ROW_NUMBER, RANK, running totals.

Template:
```sql
-- "Rank {entity}s by total spending, showing rank number."
SELECT id, name, total,
  RANK() OVER (ORDER BY total DESC) AS rnk
FROM (
  SELECT p.id, p.name, COALESCE(SUM(c.amount), 0) AS total
  FROM {parent} p
  LEFT JOIN {child} c ON p.id = c.{parent}_id
  GROUP BY p.id, p.name
) ranked
ORDER BY rnk;
```

Variations:
- ROW_NUMBER with PARTITION BY
- Running total with SUM() OVER (ORDER BY ...)
- LAG/LEAD for period-over-period

## Category 5: Multi-table JOINs (200 pairs)
Pattern: 3+ tables, complex relationships.

Template:
```sql
-- "List orders with customer name, product name, quantity, and line total."
SELECT o.id, c.name AS customer, p.name AS product,
  oi.quantity, oi.quantity * p.price AS line_total
FROM orders o
JOIN customers c ON o.customer_id = c.id
JOIN order_items oi ON o.id = oi.order_id
JOIN products p ON oi.product_id = p.id
WHERE o.status = 'completed'
ORDER BY o.id, p.name;
```

## Category 6: Complex WHERE + ORDER BY (170 pairs)
Pattern: Multiple conditions, CASE in ORDER BY.

Template:
```sql
-- "Find active users with edge cases in sorting."
SELECT id, name, status, created_at
FROM users
WHERE status IN ('active', 'pending')
  AND (created_at > '2024-01-01' OR last_login IS NOT NULL)
ORDER BY
  CASE WHEN status = 'active' THEN 0 ELSE 1 END,
  created_at DESC;
```

---

## Generation Notes
- All queries must pass the gate (schema validation, execution)
- Use NEDB with time travel for verification (snapshot → run → compare)
- Target 50% admit rate minimum (v9 had 99.9%, but analytical is harder)
- Each template needs 5-10 schema variations to avoid overfitting
