-- =====================================================================
-- OPTIONAL sample data so you can try the assistant without your own data.
--
-- Run this in your Aurora MySQL database (RDS Query editor or any MySQL
-- client) after selecting the database you put in DB_NAME. Everything
-- below is made-up demo data; the numbers have no currency attached (the
-- agent quotes prices in your CURRENCY_CODE).
--
-- Then set SOURCE_TABLE=sample_listings in deployment.config. The deploy
-- script renders build/create_public_view.sql, which builds the
-- customer-safe view over this table (see "Database setup" in README.md).
--
-- To remove it again:  DROP TABLE sample_listings;
-- =====================================================================

CREATE TABLE IF NOT EXISTS `sample_listings` (
    `ID`               VARCHAR(20)  NOT NULL PRIMARY KEY,
    `url`              VARCHAR(255),
    `unit`             VARCHAR(40),
    `purpose`          VARCHAR(10),
    `price`            BIGINT,
    `Price_Per_Meter`  INT,
    `area`             INT,
    `bedrooms`         INT,
    `bathrooms`        INT,
    `total_rooms`      INT,
    `governorate`      VARCHAR(60),
    `city`             VARCHAR(80),
    `district`         VARCHAR(80),
    `address`          VARCHAR(120),
    `finish`           VARCHAR(40),
    `view`             VARCHAR(40),
    `year_built`       INT,
    `date`             DATE,
    `details`          TEXT,
    `header1`          VARCHAR(120),
    `header2`          VARCHAR(120),
    `lat`              DECIMAL(9,6),
    `lon`              DECIMAL(9,6),
    `prediction_label` BIGINT  -- internal column: the view leaves it out on purpose
);

INSERT INTO `sample_listings`
(`ID`,`url`,`unit`,`purpose`,`price`,`Price_Per_Meter`,`area`,`bedrooms`,`bathrooms`,`total_rooms`,
 `governorate`,`city`,`district`,`address`,`finish`,`view`,`year_built`,`date`,`details`,`header1`,`header2`,`lat`,`lon`,`prediction_label`)
VALUES
('DEMO-1001','https://example.com/listing/DEMO-1001','Apartment','sale',  250000, 2273, 110, 2, 2, 4, 'North Region','Springfield - Maple Heights','Maple Heights',NULL,'Fully Finished','Garden',      2024,'2026-09-01',NULL,'Bright 2-bedroom apartment','Garden view, close to schools',       40.100001,-89.600001,262000),
('DEMO-1002','https://example.com/listing/DEMO-1002','Apartment','sale',  340000, 2429, 140, 3, 2, 5, 'North Region','Springfield - Maple Heights','Maple Heights',NULL,'Fully Finished','Main Street',  2023,'2026-09-12',NULL,'Spacious 3-bedroom apartment','Quiet building, covered parking',  40.100501,-89.600801,351000),
('DEMO-1003','https://example.com/listing/DEMO-1003','Villa',    'sale',  720000, 3000, 240, 4, 3, 7, 'North Region','Springfield - Oak Park',    'Oak Park',    NULL,'Semi Finished', 'Garden',      2027,'2026-08-20','Off-plan, delivery 2027','Family villa with garden','Corner plot',                  40.110001,-89.610001,745000),
('DEMO-1004','https://example.com/listing/DEMO-1004','Studio',   'rent',    1200, NULL,  45, 1, 1, 2, 'North Region','Springfield - Downtown',    'Downtown',    NULL,'Fully Finished','Main Street',  2022,'2026-10-02',NULL,'Compact studio for rent','Monthly rent, furnished',            40.095001,-89.640001,NULL),
('DEMO-1005','https://example.com/listing/DEMO-1005','Apartment','rent',    1800, NULL,  95, 2, 1, 4, 'North Region','Springfield - Downtown',    'Downtown',    NULL,'Fully Finished','City',         2021,'2026-10-05',NULL,'2-bedroom apartment for rent','Walk to the station',           40.096001,-89.641001,NULL),
('DEMO-1006','https://example.com/listing/DEMO-1006','Apartment','sale',  198000, 2200,  90, 2, 1, 4, 'South Region','Riverton - Harbour View',   'Harbour View',NULL,'Core & Shell',  'Sea',         2026,'2026-07-15','Under construction','Entry-level apartment by the water','Sea view',   38.900001,-77.000001,205000),
('DEMO-1007','https://example.com/listing/DEMO-1007','Apartment','sale',  455000, 3030, 150, 3, 3, 6, 'South Region','Riverton - Harbour View',   'Harbour View',NULL,'Fully Finished','Sea',         2025,'2026-09-28',NULL,'Premium 3-bedroom sea-view apartment','Large terrace',          38.901001,-77.001001,470000),
('DEMO-1008','https://example.com/listing/DEMO-1008','Townhouse','sale',  510000, 2833, 180, 3, 3, 6, 'South Region','Riverton - Willow Gate',    'Willow Gate', NULL,'Semi Finished', 'Garden',      2025,'2026-08-03',NULL,'Corner townhouse','Private garden and roof terrace',         38.910001,-77.010001,520000),
('DEMO-1009','https://example.com/listing/DEMO-1009','Villa',    'rent',    3200, NULL, 260, 5, 4, 8, 'South Region','Riverton - Willow Gate',    'Willow Gate', NULL,'Fully Finished','Garden',      2020,'2026-10-10',NULL,'Large villa for rent','Pool and garden',                          38.911001,-77.011001,NULL),
('DEMO-1010','https://example.com/listing/DEMO-1010','Apartment','sale',  129000, 1720,  75, 1, 1, 3, 'East Region', 'Lakeside - Pine Court',    'Pine Court',  NULL,'Fully Finished','Main Street',  2019,'2026-06-30',NULL,'Starter 1-bedroom apartment','Good first purchase',            41.500001,-81.700001,133000),
('DEMO-1011','https://example.com/listing/DEMO-1011','Apartment','sale',  285000, 2375, 120, 3, 2, 5, 'East Region', 'Lakeside - Pine Court',    'Pine Court',  NULL,'Fully Finished','Garden',      2024,'2026-09-18',NULL,'Family apartment near the park','Three bedrooms, two bathrooms',  41.501001,-81.701001,291000),
('DEMO-1012','https://example.com/listing/DEMO-1012','Penthouse','sale',  890000, 4450, 200, 4, 3, 7, 'East Region', 'Lakeside - Cedar Tower',   'Cedar Tower', NULL,'Fully Finished','City',        2026,'2026-10-14',NULL,'Penthouse with skyline view','Private roof terrace',          41.510001,-81.710001,905000);
