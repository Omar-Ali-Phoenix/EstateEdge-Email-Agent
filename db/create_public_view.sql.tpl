-- =====================================================================
-- EstateEdge: create the customer-safe view and grant read-only access.
--
-- Run this in your Aurora account as the MASTER user (through the RDS
-- Query editor or any MySQL client), AFTER enabling the RDS Data API
-- on your cluster.
--
-- SAFE / NON-DESTRUCTIVE:
--   * CREATE VIEW stores no data; it is a saved SELECT over the base table.
--   * Your base table is never modified.
--   * Fully reversible:  DROP VIEW `{{DB_NAME}}`.`{{VIEW_NAME}}`;
--
-- WHY THIS VIEW:
--   1. The agent's table-name validator only allows [A-Za-z0-9_], so a base
--      table name with spaces or dashes cannot be used directly. The view
--      gives it a clean, validator-safe name.
--   2. The view is the security boundary: it exposes only customer-safe
--      columns. Internal columns (such as an ML price estimate) are left
--      out of the column list below and are therefore invisible to the
--      agent and to you on the page.
-- =====================================================================

-- Step 1: create the view. EDIT the column list below so it matches your
-- base table, and REMOVE anything internal you do not want customers to
-- see. Uses CREATE VIEW (not REPLACE) so it errors out instead of
-- overwriting if something already exists with this name.
CREATE VIEW `{{DB_NAME}}`.`{{VIEW_NAME}}` AS
SELECT
    `ID`,
    `url`,
    `unit`,
    `purpose`,
    `price`,
    `Price_Per_Meter`,
    `area`,
    `bedrooms`,
    `bathrooms`,
    `total_rooms`,
    `governorate`,
    `city`,
    `district`,
    `address`,
    `finish`,
    `view`,
    `year_built`,
    `date`,
    `details`,
    `header1`,
    `header2`,
    `lat`,
    `lon`
FROM `{{DB_NAME}}`.`{{SOURCE_TABLE}}`;
-- Deliberately EXCLUDED (edit to match your internal columns):
--   prediction_label   -- internal ML price estimate, never shown to customers

-- Step 2: grant SELECT on the view to the read-only agent user.
-- (The user already exists; this only ADDS a SELECT grant on the view.
--  It changes nothing about the user's other access.)
GRANT SELECT ON `{{DB_NAME}}`.`{{VIEW_NAME}}` TO '{{READONLY_DB_USER}}'@'%';

-- Step 3: verify. As the master user:
--   SELECT * FROM `{{DB_NAME}}`.`{{VIEW_NAME}}` LIMIT 3;
--   SHOW GRANTS FOR '{{READONLY_DB_USER}}'@'%';
-- The grants should now include SELECT on the view.
