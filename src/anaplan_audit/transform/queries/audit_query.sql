-- v4: DuckDB dialect. Changes from the v3 SQLite version:
--   * String literals use single quotes (SQLite silently treated unmatched
--     double-quoted tokens as literals; DuckDB always parses "" as identifiers).
--   * Bracket identifiers ([Event Message]) became double-quoted identifiers.
--   * datetime(x/1000,'unixepoch') became strftime(to_timestamp(...) AT TIME
--     ZONE 'UTC', ...). AT TIME ZONE 'UTC' is defense-in-depth: the session
--     TimeZone is already pinned to UTC on connect, but the query stays
--     correct even on a raw debugging session that skipped the SET.
--   * eventDate // 1000 (integer division) preserves SQLite's truncating
--     int/int division; DuckDB's / is float division.
--   * TENANT_NAME is a bound parameter ($tenant_name), not string-templated —
--     a tenant name containing a quote can no longer break the SQL.
--   * SUCCESS is cast to INTEGER so the CSV carries 1/0 exactly as v3 did
--     (SQLite stored booleans as integers; DuckDB has a real BOOLEAN type).
--   * The MODEL_NAME CloudWorks arm's subquery is correlated to cw.modelId —
--     in v3 it was uncorrelated and silently picked an arbitrary row when a
--     tenant had more than one CloudWorks integration (latent bug).
SELECT
	CAST(e.eventDate // 1000 AS VARCHAR) || lpad(CAST(e."index" AS VARCHAR), 9, '0') as LOAD_ID ,
	{{time_stamp}} as BATCH_ID ,
	e.id as AUDIT_ID ,
	strftime(to_timestamp(e.eventDate / 1000.0) AT TIME ZONE 'UTC', '%Y-%m-%d %H:%M:%S') as EVENT_DATE ,
	e.eventTimeZone as EVENT_TIMEZONE ,
	strftime(to_timestamp(e.createdDate / 1000.0) AT TIME ZONE 'UTC', '%Y-%m-%d %H:%M:%S') as CREATED_DATE ,
	e.createdTimeZone as CREATE_TIMEZONE ,
	e.eventTypeId as EVENT_ID ,
	ac."Event Message" as EVENT_MESSAGE ,
	ac."Associated Object Id" as ASSOCIATED_OBJECT_ID,
	ac.Notes as NOTES ,
	e.userId as USER_ID ,
	u.userName as USER_NAME ,
	u.displayName as DISPLAY_NAME ,
	e.tenantId as TENANT_ID ,
	$tenant_name as TENANT_NAME ,
	e."additionalAttributes.workspaceId" as WORKSPACE_ID ,
	w.name as WORKSPACE_NAME ,
	CASE
		WHEN e."additionalAttributes.modelId" IS NOT NULL THEN e."additionalAttributes.modelId"
		WHEN e.objectId = cw.integrationId THEN cw.modelId
	END as MODEL_ID ,
	CASE
		WHEN e."additionalAttributes.modelId" IS NOT NULL THEN m.name
		WHEN e.objectId = cw.integrationId THEN (SELECT m3.name FROM models m3 WHERE m3.id = cw.modelId)
	END as MODEL_NAME ,
	e.objectId as OBJECT_ID ,
	CASE
		WHEN e.objectId = m2.id THEN 'Model'
		WHEN e.objectId = cw.integrationId THEN 'CloudWorks Integration'
		WHEN e.objectId = u2.id  THEN 'User'
	END as OBJECT_TYPE ,
	CASE
		WHEN e.objectId = m2.id THEN m2.name
		WHEN e.objectId = cw.integrationId THEN cw.name
		WHEN e.objectId = u2.id  THEN u2.userName
	END as OBJECT_NAME ,
	e.message as MESSAGE ,
	CAST(e.success AS INTEGER) as SUCCESS,
	e.errorNumber as ERROR_NUMBER ,
	e.ipAddress as IP_ADDRESS ,
	e.userAgent as USER_AGENT ,
	e.sessionId as SESSION_ID ,
	e.hostName as HOST_NAME ,
	e.serviceVersion as SERVICE_VERSION ,
	e.objectTypeId as OBJECT_TYPE_ID ,
	e.objectTenantId as OBJECT_TENANT_ID ,
	e."additionalAttributes.actionId" AS ACTION_ID ,
	CASE
		WHEN e."additionalAttributes.actionId" = '-1' THEN 'Unsaved Action'
		WHEN a.name IS NULL AND e."additionalAttributes.actionId" IS NOT NULL THEN '<Object has been Deleted>'
		ELSE a.name
	END AS ACTION_NAME ,
	e."additionalAttributes.name" as ADDITIONAL_ATTRIBUTES_NAME ,
	e."additionalAttributes.type" as ADDITIONAL_ATTRIBUTES_TYPE ,
	e."additionalAttributes.auth_id" as ADDITIONAL_ATTRIBUTES_AUTH_ID ,
	e."additionalAttributes.modelRoleName" as MODEL_ROLE_NAME ,
	e."additionalAttributes.modelRoleId" as MODEL_ROLE_ID ,
	e."additionalAttributes.objectTypeId" as ADDITIONAL_ATTRIBUTES_OBJECT_TYPE_ID ,
	e."additionalAttributes.roleId" as ADDITIONAL_ATTRIBUTES_ROLE_ID ,
	e."additionalAttributes.roleName" as ADDITIONAL_ATTRIBUTES_ROLE_NAME ,
	e."additionalAttributes.objectTenantId" as ADDITIONAL_ATTRIBUTES_OBJECT_TENANT_ID ,
	e."additionalAttributes.objectId" as ADDITIONAL_ATTRIBUTES_OBJECT_ID ,
	e."additionalAttributes.active" as ADDITIONAL_ATTRIBUTES_ACTIVE ,
	e."additionalAttributes.appId" as UX_APP_ID ,
	e."additionalAttributes.appName" as UX_APP_NAME ,
	e."additionalAttributes.pageId" as UX_PAGE_ID ,
	e."additionalAttributes.pageName" as UX_PAGE_NAME ,
	e."additionalAttributes.pipelineId" as ADO_PIPELINE_ID ,
	e."additionalAttributes.dataspaceId" as ADO_DATASPACE_ID ,
	e."additionalAttributes.scheduleId" as ADO_SCHEDULE_ID ,
	e."additionalAttributes.connectionId" as ADO_CONNECTION_ID ,
	e."additionalAttributes.taskId" as WORKFLOW_TASK_ID ,
	e."additionalAttributes.workflowTemplateId" as WORKFLOW_TEMPLATE_ID ,
	e."additionalAttributes.commentId" as COMMENT_ID ,
	CASE
		WHEN e.eventTypeId LIKE 'USR-%' THEN 'User Activity'
		WHEN e.eventTypeId LIKE 'AUTHZ-%' THEN 'Access Control'
		WHEN e.eventTypeId LIKE 'CONN-%' THEN 'Connection Management'
		WHEN e.eventTypeId LIKE 'INT-0%' THEN 'Integrations (CloudWorks)'
		WHEN e.eventTypeId LIKE 'INT-%' THEN 'Integrations (ADO)'
		WHEN e.eventTypeId LIKE 'FRCST-%' THEN 'Forecaster'
		WHEN e.eventTypeId LIKE 'PIQ-%' THEN 'Forecaster (Legacy PlanIQ)'
		WHEN e.eventTypeId LIKE 'WF-1%' AND length(e.eventTypeId) > 5 THEN 'Workflow (Template)'
		WHEN e.eventTypeId LIKE 'WF-%' THEN 'Workflow (Task)'
		WHEN e.eventTypeId LIKE 'COMMENT-%' THEN 'Comments'
		WHEN e.eventTypeId LIKE 'DSM-%' THEN 'Encryption / Guardpoint (BYOK)'
		WHEN e.eventTypeId LIKE 'OAUTH-%' THEN 'OAuth'
		ELSE 'Other'
	END as EVENT_CATEGORY ,
	e.checksum as CHECKSUM
FROM events e
LEFT JOIN users u ON e.userId = u.id
LEFT JOIN users u2 ON e.objectId = u2.id
LEFT JOIN workspaces w ON e."additionalAttributes.workspaceId" = w.id
LEFT JOIN models m ON e."additionalAttributes.modelId" = m.id
LEFT JOIN models m2 ON e.objectId = m2.id
LEFT JOIN cloudworks cw on e.objectId = cw.integrationId
LEFT JOIN act_codes ac on e.eventTypeId = ac."Event Code"
LEFT JOIN actions a on e."additionalAttributes.actionId" || e.objectId  = a.id || a.model_id
