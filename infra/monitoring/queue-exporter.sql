-- The query behind the queue metrics. Kept as SQL rather than hidden in an exporter
-- image so an operator can run the same numbers by hand during an incident.
--
-- Progress is read from task rows. Completion is never inferred from a missing
-- worker heartbeat.

-- Depth per stage and state.
SELECT stage, state, count(*) AS tasks
FROM tasks
GROUP BY stage, state
ORDER BY stage, state;

-- Age of the oldest claimable task per stage, in seconds. This, not raw depth, is
-- what tells you whether the queue is moving.
SELECT stage,
       extract(epoch FROM (now() - min(created_at)))::bigint AS oldest_ready_age_seconds
FROM tasks
WHERE state IN ('ready', 'leased')
GROUP BY stage
ORDER BY oldest_ready_age_seconds DESC;

-- Dead letters, with why. Every one of these was audited when it happened.
SELECT id, stage, error_class, left(coalesce(error_detail, ''), 120) AS detail, completed_at
FROM tasks
WHERE state = 'dead_letter'
ORDER BY completed_at DESC NULLS LAST
LIMIT 50;

-- Work waiting on a person. Reported, never alerted on.
SELECT stage, human_action_required, count(*) AS tasks
FROM tasks
WHERE state = 'awaiting_user'
GROUP BY stage, human_action_required
ORDER BY tasks DESC;
