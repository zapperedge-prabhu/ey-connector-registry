-- Optimized pre-processing filter
SELECT 'Rule1' AS "RuleId", 'subscribedskus' AS "__table",
       "subscribedskus"."__row" AS "__row"
FROM "subscribedskus"
WHERE "subscribedskus"."status" = 'disabled'
