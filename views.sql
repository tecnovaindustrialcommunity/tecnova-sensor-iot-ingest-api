-- Replace PROJECT_ID before running, or use deploy.sh which does it for you.

-- Readings without duplicates (a gateway that retries a batch sends the same reading_id again).
CREATE OR REPLACE VIEW `PROJECT_ID.agro.readings_clean` AS
SELECT * EXCEPT (rn)
FROM (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY reading_id ORDER BY received_at) AS rn
  FROM `PROJECT_ID.agro.readings`
)
WHERE rn = 1;
