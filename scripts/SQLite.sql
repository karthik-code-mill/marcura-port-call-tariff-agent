-- SQLite
SELECT id, doc_id, port, country, currency, tariff_year, section, tariff_fee_item, vessel_gt_range, gt_min, gt_max, base_fee, incremental_fee_per_100_gt, formula, conditions, surcharges, exceptions, notes, unmodeled_clauses, extraction_confidence, source_page, ingested_at
FROM tariff_fee_items WHERE port = 'All' and tariff_fee_item like 'PILOTAGE%';

--  AND gt_min <= 51300 AND gt_max >= 51300 port = 'All';


-- SQLite
-- SQLite
SELECT id, doc_id, port, country, currency, tariff_year, section, tariff_fee_item, vessel_gt_range, gt_min, gt_max, base_fee, incremental_fee_per_100_gt, formula, conditions, surcharges, exceptions, notes, unmodeled_clauses, extraction_confidence, source_page, ingested_at
FROM tariff_fee_items ;

--WHERE port = 'Duran' ;

--and tariff_fee_item like 'LIGHT%';

--  AND gt_min <= 51300 AND gt_max >= 51300 port = 'All';

SELECT * FROM tariff_fee_items
WHERE port = 'All' AND gt_min <= 51300 AND gt_max >= 51300
ORDER BY section;


-- SQLite
SELECT *
FROM tariff_fee_items WHERE tariff_fee_item like 'Pilotage%';

-- SQLite
-- SQLite
-- SQLite
SELECT * FROM tariff_fee_items WHERE tariff_fee_item like 'PORT%';

--  AND gt_min <= 51300 AND gt_max >= 51300 port = 'All';


-- SQLite
-- SQLite
--SELECT id, doc_id, port, country, currency, tariff_year, section, tariff_fee_item, vessel_gt_range, gt_min, gt_max, base_fee, incremental_fee_per_100_gt, formula, conditions, surcharges, exceptions, notes, unmodeled_clauses, extraction_confidence, source_page, ingested_at
--FROM tariff_fee_items ;

--WHERE port = 'Duran' ;

--and tariff_fee_item like 'LIGHT%';

--  AND gt_min <= 51300 AND gt_max >= 51300 port = 'All';

-- SQLite
--SELECT * FROM tariff_fee_items
--WHERE port = 'All' AND gt_min <= 51300 AND gt_max >= 51300
--ORDER BY section;


-- SQLite
SELECT *
FROM tariff_fee_items WHERE tariff_fee_item;

-- SQLite
-- SQLite
-- SQLite
--SELECT * FROM tariff_fee_items WHERE tariff_fee_item like 'PORT%';

--  AND gt_min <= 51300 AND gt_max >= 51300 port = 'All';


-- SQLite
-- SQLite
--SELECT id, doc_id, port, country, currency, tariff_year, section, tariff_fee_item, vessel_gt_range, gt_min, gt_max, base_fee, incremental_fee_per_100_gt, formula, conditions, surcharges, exceptions, notes, unmodeled_clauses, extraction_confidence, source_page, ingested_at
--FROM tariff_fee_items ;

--WHERE port = 'Duran' ;

--and tariff_fee_item like 'LIGHT%';

--  AND gt_min <= 51300 AND gt_max >= 51300 port = 'All';