PRAGMA writable_schema=ON;

UPDATE sqlite_master
SET sql = replace(
    sql,
    'max_specialists INTEGER NOT NULL CHECK (max_specialists BETWEEN 1 AND 3)',
    'max_specialists INTEGER NOT NULL CHECK (max_specialists BETWEEN 1 AND 32)'
)
WHERE type='table' AND name='research_skill_registry_index';

UPDATE sqlite_master
SET sql = replace(
    sql,
    'selected_count INTEGER NOT NULL CHECK (selected_count BETWEEN 0 AND 3)',
    'selected_count INTEGER NOT NULL CHECK (selected_count BETWEEN 0 AND 32)'
)
WHERE type='table' AND name='specialist_route_plan_index';

PRAGMA writable_schema=OFF;
