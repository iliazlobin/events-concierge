{{- define "events-concierge.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "events-concierge.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- include "events-concierge.name" . -}}
{{- end -}}
{{- end -}}

{{- define "events-concierge.image" -}}
{{- printf "%s@%s" .repository .digest -}}
{{- end -}}

{{- define "events-concierge.labels" -}}
app.kubernetes.io/name: {{ include "events-concierge.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/part-of: events-concierge
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
app.kubernetes.io/component: {{ .component }}
app.kubernetes.io/version: {{ .root.Values.global.releaseRevision | quote }}
{{- end -}}

{{- define "events-concierge.selectorLabels" -}}
app.kubernetes.io/name: {{ include "events-concierge.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "events-concierge.runtimeSecretProviderClass" -}}
{{ include "events-concierge.fullname" . }}-runtime
{{- end -}}

{{- define "events-concierge.migrationSecretProviderClass" -}}
{{ include "events-concierge.fullname" . }}-migration
{{- end -}}

{{- define "events-concierge.appSecurityContext" -}}
allowPrivilegeEscalation: false
capabilities:
  drop: ["ALL"]
readOnlyRootFilesystem: true
runAsNonRoot: true
runAsUser: 10001
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "events-concierge.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
fsGroup: 10001
fsGroupChangePolicy: OnRootMismatch
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "events-concierge.validateDeployment" -}}
{{- $dev := eq (default "managed" .Values.global.deploymentProfile) "development" -}}
{{- $private := eq .Values.global.deploymentProfile "private" -}}
{{- if $private -}}{{- include "events-concierge.validatePrivateRuntime" . -}}{{- end -}}
{{- if .Values.publicTunnel.enabled -}}
{{- if or $dev (ne .Values.global.releasePhase "application") (not .Values.global.runtimeProviderReady) (ne (toString .Values.applicationConfig.EC_MOCK_CLOUD) "false") -}}
{{- fail "publicTunnel requires a verified non-mock application release; private development cannot be published" -}}
{{- end -}}
{{- if or (not .Values.networkPolicy.enabled) .Values.gateway.enabled (not .Values.workloads.frontend.enabled) -}}
{{- fail "publicTunnel requires NetworkPolicy and the consumer frontend, with gateway.enabled=false" -}}
{{- end -}}
{{- if or (ne .Values.publicTunnel.hostname "events.iliazlobin.com") (ne .Values.applicationConfig.EC_PUBLIC_BASE_URL "https://events.iliazlobin.com") -}}
{{- fail "publicTunnel and the application must use the approved origin https://events.iliazlobin.com" -}}
{{- end -}}
{{- if or (ne (toString .Values.applicationConfig.EC_IDENTITY_PLATFORM_ENABLED) "true") (ne (toString .Values.applicationConfig.EC_ADMIN_INGESTION_ENABLED) "false") (ne (toString .Values.applicationConfig.EC_OIDC_BFF_ENABLED) "false") -}}
{{- fail "publicTunnel requires consumer Identity Platform and disabled legacy OIDC/admin ingestion" -}}
{{- end -}}
{{- if not .Values.publicTunnel.tokenSecretName -}}{{- fail "publicTunnel.tokenSecretName must reference an externally provisioned tunnel-only Secret" -}}{{- end -}}
{{- range $image := list .Values.publicTunnel.cloudflaredImage .Values.publicTunnel.proxyImage -}}
{{- if eq $image.digest "sha256:0000000000000000000000000000000000000000000000000000000000000000" -}}
{{- fail "publicTunnel images must use verified non-placeholder sha256 digests" -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if and $dev .Values.operator.enabled -}}
{{- fail "development disables operator.enabled; use the loopback-only development admin" -}}
{{- end -}}
{{- if .Values.operator.enabled -}}
{{- range $key := list "hostname" "tlsSecretName" "iapAudience" "iapClientId" "iapClientSecretName" -}}
{{- if not (index $.Values.operator $key) -}}{{- fail (printf "operator.%s is required" $key) -}}{{- end -}}
{{- end -}}
{{- if not .Values.operator.subjectRoles -}}{{- fail "operator.subjectRoles must explicitly assign IAP subjects" -}}{{- end -}}
{{- if not .Values.networkPolicy.enabled -}}{{- fail "operator requires networkPolicy.enabled" -}}{{- end -}}
{{- $catalogPrefix := printf "%s/" (trimSuffix "/" .Values.operator.executorClaimCheckPrefix) -}}
{{- $consumerPrefix := printf "%s/" (trimSuffix "/" .Values.applicationConfig.EC_GCS_CLAIM_CHECK_PREFIX) -}}
{{- if or (eq $catalogPrefix "/") (eq $consumerPrefix "/") (hasPrefix $catalogPrefix $consumerPrefix) (hasPrefix $consumerPrefix $catalogPrefix) -}}
{{- fail "operator catalog and consumer claim-check prefixes must be nonempty and disjoint" -}}
{{- end -}}
{{- if .Values.jobs.catalogRefresh.enabled -}}
{{- fail "operator profile requires manual refreshes through durable operator commands, not the legacy catalogRefresh Job" -}}
{{- end -}}
{{- range $name, $profile := dict "operator-api" "operator" "ingestion-executor" "executor" "operator-frontend" "frontend" -}}
{{- $workload := index $.Values.workloads $name -}}
{{- if or (ne $workload.operatorProfile $profile) (ne $workload.serviceAccount $name) -}}
{{- fail (printf "workloads.%s must retain its isolated operatorProfile and serviceAccount" $name) -}}
{{- end -}}
{{- if eq $name "operator-frontend" -}}
{{- if or $workload.runtimeSecrets $workload.database $workload.needsIdentity -}}{{- fail "operator frontend cannot hold backend credentials" -}}{{- end -}}
{{- else -}}
{{- if not (and $workload.runtimeSecrets $workload.database $workload.needsIdentity) -}}{{- fail "operator backend requires its isolated identity and database secrets" -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- $operatorAccount := (index .Values.serviceAccounts "operator-api").gcpServiceAccount -}}
{{- $executorAccount := (index .Values.serviceAccounts "ingestion-executor").gcpServiceAccount -}}
{{- range $isolated := list "operator-frontend" "operator-api" "ingestion-executor" -}}
{{- $name := (index $.Values.serviceAccounts $isolated).name -}}
{{- if not $name -}}{{- fail "operator requires nonempty isolated Kubernetes service account names" -}}{{- end -}}
{{- range $key, $account := $.Values.serviceAccounts -}}
{{- if and (ne $key $isolated) (eq $name $account.name) -}}
{{- fail "operator Kubernetes service account names cannot be shared with another workload" -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if or (not $operatorAccount) (not $executorAccount) (eq $operatorAccount $executorAccount) -}}
{{- fail "operator requires distinct nonempty operator-api and ingestion-executor GCP identities" -}}
{{- end -}}
{{- range $key, $account := .Values.serviceAccounts -}}
{{- if and (not (has $key (list "operator-api" "ingestion-executor"))) (has $account.gcpServiceAccount (list $operatorAccount $executorAccount)) -}}
{{- fail "operator and executor GCP identities cannot be shared with consumer or migration workloads" -}}
{{- end -}}
{{- end -}}
{{- range $profile := list "operator" "executor" -}}
{{- $allowed := list "EC_OPERATOR_DATABASE_URL" -}}
{{- $required := $allowed -}}
{{- if eq $profile "executor" -}}
{{- $required = list "EC_INGESTION_EXECUTOR_DATABASE_URL" "EC_REDIS_URL" "EC_TEMPORAL_API_KEY" -}}
{{- $allowed = append $required "REDIS_CA_CERTIFICATE" -}}
{{- end -}}
{{- $files := list -}}
{{- range index $.Values.operator (printf "%sSecrets" $profile) -}}
{{- if or (not (has .fileName $allowed)) (has .fileName $files) -}}
{{- fail (printf "operator.%sSecrets contains an unauthorized or duplicate secret file" $profile) -}}
{{- end -}}
{{- $files = append $files .fileName -}}
{{- end -}}
{{- range $required -}}
{{- if not (has . $files) -}}{{- fail (printf "operator.%sSecrets requires %s" $profile .) -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- range $name := list "operator-frontend" "operator-api" "ingestion-executor" "temporal-catalog" -}}
{{- if not (index $.Values.workloads $name).enabled -}}{{- fail (printf "operator requires workloads.%s.enabled" $name) -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- if and .Values.developmentCatalog.enabled (not $dev) -}}
{{- fail "developmentCatalog.enabled is restricted to the private development profile" -}}
{{- end -}}
{{- if and .Values.developmentCatalog.cadenceEnabled (not (and $dev .Values.developmentCatalog.enabled)) -}}
{{- fail "development cadence requires the private development catalog executor profile" -}}
{{- end -}}
{{- if $dev -}}
{{- if or (ne (toString .Values.applicationConfig.EC_MOCK_CLOUD) "true") .Values.cloudSqlProxy.enabled (ne .Values.applicationConfig.EC_DATABASE_CONNECTION_MODE "development_plaintext") -}}
{{- fail "development requires mock product integrations and its private database connection mode" -}}
{{- end -}}
{{- range $field := list "EC_OIDC_BFF_ENABLED" "EC_IDENTITY_PLATFORM_ENABLED" "EC_GOOGLE_CALENDAR_ENABLED" "EC_CATALOG_INGESTION_SCHEDULER_ENABLED" -}}
{{- if ne (toString (index $.Values.applicationConfig $field)) "false" -}}{{- fail (printf "development requires %s=false" $field) -}}{{- end -}}
{{- end -}}
{{- if or .Values.catalogDispatcher.enabled .Values.jobs.catalogRefresh.enabled -}}
{{- fail "development uses explicit operator commands; scheduled and legacy catalog jobs remain disabled" -}}
{{- end -}}
{{- $controller := index .Values.serviceAccounts "development-admin" -}}
{{- if not $controller.gcpServiceAccount -}}{{- fail "development requires a dedicated development-admin GCP identity" -}}{{- end -}}
{{- range $key, $account := .Values.serviceAccounts -}}
{{- if and (ne $key "development-admin") (or (eq $account.name $controller.name) (eq $account.gcpServiceAccount $controller.gcpServiceAccount)) -}}
{{- fail "development admin identity cannot be shared with another workload" -}}
{{- end -}}
{{- end -}}
{{- if ne (len .Values.developmentAdmin.operatorSecrets) 1 -}}{{- fail "development admin requires exactly one controller database secret" -}}{{- end -}}
{{- $controllerSecret := first .Values.developmentAdmin.operatorSecrets -}}
{{- if or (ne $controllerSecret.fileName "EC_OPERATOR_DATABASE_URL") (ne $controllerSecret.secretName "ec-dev-operator-database-url") -}}
{{- fail "development admin requires the dedicated controller database secret" -}}
{{- end -}}
{{- $migrationFiles := dict -}}
{{- range .Values.secretManager.migrationSecrets -}}{{- $_ := set $migrationFiles .fileName .secretName -}}{{- end -}}
{{- range $file, $secret := dict "EC_DEV_OPERATOR_PASSWORD" "ec-dev-operator-role-password" "EC_DEV_INGESTION_PASSWORD" "ec-dev-ingestion-executor-role-password" -}}
{{- if ne (default "" (index $migrationFiles $file)) $secret -}}{{- fail (printf "development migration requires %s" $file) -}}{{- end -}}
{{- end -}}
{{- if .Values.developmentCatalog.enabled -}}
{{- if ne .Values.developmentCatalog.claimCheckPrefix "events-concierge/catalog/v1" -}}{{- fail "development catalog requires the reviewed GCS prefix events-concierge/catalog/v1" -}}{{- end -}}
{{- $consumerPrefix := printf "%s/" (trimSuffix "/" .Values.applicationConfig.EC_GCS_CLAIM_CHECK_PREFIX) -}}
{{- $catalogPrefix := printf "%s/" .Values.developmentCatalog.claimCheckPrefix -}}
{{- if or (eq $consumerPrefix "/") (hasPrefix $consumerPrefix $catalogPrefix) (hasPrefix $catalogPrefix $consumerPrefix) -}}{{- fail "development catalog and consumer storage prefixes must be disjoint" -}}{{- end -}}
{{- $executorSecrets := dict "EC_INGESTION_EXECUTOR_DATABASE_URL" "ec-dev-ingestion-executor-database-url" "EC_REDIS_URL" "ec-dev-redis-url" -}}
{{- if ne (len .Values.developmentCatalog.executorSecrets) 2 -}}{{- fail "development executor requires only its dedicated database and Redis secrets" -}}{{- end -}}
{{- $seen := list -}}
{{- range .Values.developmentCatalog.executorSecrets -}}
{{- if or (has .fileName $seen) (ne (default "" (index $executorSecrets .fileName)) .secretName) -}}{{- fail "development executor secret is unauthorized or duplicated" -}}{{- end -}}
{{- $seen = append $seen .fileName -}}
{{- end -}}
{{- range $name := list "ingestion-executor" "temporal-catalog" -}}
{{- $workload := index $.Values.workloads $name -}}
{{- if or (not $workload.enabled) (ne $workload.serviceAccount $name) (not $workload.needsIdentity) (not $workload.database) (not $workload.runtimeSecrets) -}}
{{- fail (printf "development catalog requires the isolated %s workload" $name) -}}
{{- end -}}
{{- $account := index $.Values.serviceAccounts $name -}}
{{- if not $account.gcpServiceAccount -}}{{- fail (printf "development catalog requires the %s GCP identity" $name) -}}{{- end -}}
{{- range $key, $other := $.Values.serviceAccounts -}}
{{- if and (ne $key $name) (or (eq $account.name $other.name) (eq $account.gcpServiceAccount $other.gcpServiceAccount)) -}}
{{- fail "development catalog identities cannot be shared with another workload" -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if or (ne .Release.Namespace "events-concierge-dev") (ne .Values.applicationConfig.EC_ENV "development") .Values.gateway.enabled -}}
{{- fail "development requires its isolated namespace, EC_ENV=development, no gateway or autoscaling" -}}
{{- end -}}
{{- range .Values.autoscaling -}}
{{- if .enabled -}}{{- fail "development disables autoscaling" -}}{{- end -}}
{{- end -}}
{{- if not .Values.networkPolicy.enabled -}}{{- fail "development requires NetworkPolicy" -}}{{- end -}}
{{- end -}}
{{- $phase := .Values.global.releasePhase -}}
{{- $zeroDigest := "sha256:0000000000000000000000000000000000000000000000000000000000000000" -}}
{{- if not .Values.secretManager.enabled -}}
{{- fail "secretManager.enabled must be true for every deployable release phase" -}}
{{- end -}}
{{- if and (not (or $dev $private)) (not .Values.cloudSqlProxy.enabled) -}}
{{- fail "cloudSqlProxy.enabled must be true for every deployable release phase" -}}
{{- end -}}

{{- if or (eq .Values.global.appImage.digest $zeroDigest) (contains "replace-me" .Values.global.appImage.repository) -}}
{{- fail "global.appImage must be replaced with a release repository and non-placeholder sha256 digest" -}}
{{- end -}}
{{- if and .Values.cloudSqlProxy.enabled (or (eq .Values.cloudSqlProxy.image.digest $zeroDigest) (contains "replace-me" .Values.cloudSqlProxy.image.repository)) -}}
{{- fail "cloudSqlProxy.image must be pinned to a non-placeholder sha256 digest" -}}
{{- end -}}
{{- if eq $phase "application" -}}
{{- if or (eq .Values.global.frontendImage.digest $zeroDigest) (contains "replace-me" .Values.global.frontendImage.repository) -}}
{{- fail "global.frontendImage must be replaced with a release repository and non-placeholder sha256 digest" -}}
{{- end -}}
{{- end -}}
{{- if and (or (eq $phase "application") (eq $phase "operations")) (not .Values.global.runtimeProviderReady) -}}
{{- fail "global.runtimeProviderReady must be true for application or operations after full deployed-provider validation" -}}
{{- end -}}
{{- end -}}


{{- define "events-concierge.validatePrivateRuntime" -}}
{{- if or (ne .Release.Namespace "events-concierge-dev") .Values.cloudSqlProxy.enabled .Values.gateway.enabled .Values.operator.enabled .Values.developmentCatalog.enabled -}}
{{- fail "private requires the existing namespace and direct TLS stores; managed gateways and demo catalog/admin stay disabled" -}}
{{- end -}}
{{- range $key, $value := dict "EC_ENV" "staging" "EC_MOCK_CLOUD" "false" "EC_DATABASE_CONNECTION_MODE" "direct_tls" "EC_RELEASE_PROFILE" "discovery" "EC_IDENTITY_PLATFORM_ENABLED" "true" "EC_OIDC_BFF_ENABLED" "false" "EC_ADMIN_INGESTION_ENABLED" "false" "EC_GOOGLE_CALENDAR_ENABLED" "false" "EC_AGENT_ENABLED" "false" "EC_CATALOG_INGESTION_SCHEDULER_ENABLED" "false" "EC_TEMPORAL_TLS_ENABLED" "true" "EC_UI_AUTH_START_URL" "/sign-in" "EC_RUNTIME_PROVIDER_FACTORY" "events_concierge.deployment.gcp_runtime:build_runtime_ports" -}}
{{- if ne (toString (index $.Values.applicationConfig $key)) $value -}}{{- fail (printf "private requires %s=%s" $key $value) -}}{{- end -}}
{{- end -}}
{{- range $key := list "EC_TEMPORAL_API_KEY_FILE" "EC_OIDC_CLIENT_SECRET_FILE" "EC_COHERE_API_KEY_FILE" "EC_OPENROUTER_API_KEY_FILE" -}}
{{- if index $.Values.applicationConfig $key -}}{{- fail "private discovery cannot configure API keys or legacy provider secrets" -}}{{- end -}}
{{- end -}}
{{- range $key := list "postgresCASecretName" "redisCASecretName" "temporalCASecretName" -}}
{{- if not (index $.Values.privateRuntime $key) -}}{{- fail (printf "privateRuntime.%s is required" $key) -}}{{- end -}}
{{- end -}}
{{- if or (not .Values.networkPolicy.enabled) .Values.catalogDispatcher.enabled .Values.jobs.catalogRefresh.enabled .Values.jobs.roleRotation.enabled -}}{{- fail "private requires network isolation and its scoped catalog cadence; legacy jobs are disabled" -}}{{- end -}}
{{- range .Values.autoscaling -}}{{- if .enabled -}}{{- fail "private has bounded single-node capacity; autoscaling needs a separate reviewed profile" -}}{{- end -}}{{- end -}}
{{- range .Values.maintenanceCronJobs -}}{{- if .enabled -}}{{- fail "private discovery disables deferred maintenance jobs" -}}{{- end -}}{{- end -}}
{{- $certs := list -}}
{{- range $name := list "api" "account-erasure" "ingestion-executor" "temporal-catalog" -}}
{{- $workload := index $.Values.workloads $name -}}
{{- if not (and $workload.enabled $workload.database $workload.runtimeSecrets $workload.needsIdentity (eq $workload.serviceAccount $name)) -}}{{- fail (printf "private requires the isolated %s workload" $name) -}}{{- end -}}
{{- range $key, $value := $workload.env -}}
{{- if not (and (eq $key "EC_TEMPORAL_WORKER_ROLE") (eq $value "catalog") (eq $name "temporal-catalog")) -}}{{- fail "private process configuration cannot override its scoped security settings" -}}{{- end -}}
{{- end -}}
{{- $account := index $.Values.serviceAccounts $name -}}
{{- if not $account.gcpServiceAccount -}}{{- fail (printf "private requires the %s GCP identity" $name) -}}{{- end -}}
{{- range $key, $other := $.Values.serviceAccounts -}}
{{- if and (ne $key $name) (or (eq $account.name $other.name) (eq $account.gcpServiceAccount $other.gcpServiceAccount)) -}}{{- fail "private workload identities cannot be shared" -}}{{- end -}}
{{- end -}}
{{- $cert := index $.Values.privateRuntime.temporalClientSecretNames $name -}}
{{- $certRole := index (dict "api" "api" "account-erasure" "erasure" "ingestion-executor" "executor" "temporal-catalog" "catalog") $name -}}
{{- if or (not $cert) (has $cert $certs) (not (regexMatch (printf "^ec-dev-temporal-%s-tls-v[1-9][0-9]*$" $certRole) $cert)) -}}{{- fail "private requires its own versioned Temporal client certificate for each process" -}}{{- end -}}
{{- $certs = append $certs $cert -}}
{{- $database := ternary "EC_INGESTION_EXECUTOR_DATABASE_URL" "EC_DATABASE_URL" (has $name (list "ingestion-executor" "temporal-catalog")) -}}
{{- $databaseSecret := ternary "ec-dev-ingestion-executor-database-url" "ec-dev-database-url" (has $name (list "ingestion-executor" "temporal-catalog")) -}}
{{- $allowedSecrets := dict $database $databaseSecret "EC_REDIS_URL" "ec-dev-redis-url" -}}
{{- $secrets := default (list) (index $.Values.privateRuntime.processSecrets $name) -}}
{{- if ne (len $secrets) 2 -}}{{- fail "private process secrets contain exactly one restricted database DSN and Redis DSN" -}}{{- end -}}
{{- $seen := list -}}
{{- range $secrets -}}
{{- if or (has .fileName $seen) (ne (default "" (index $allowedSecrets .fileName)) .secretName) -}}{{- fail "private process secret file is unauthorized or duplicated" -}}{{- end -}}
{{- $seen = append $seen .fileName -}}
{{- end -}}
{{- end -}}
{{- range $name, $workload := .Values.workloads -}}
{{- if and $workload.enabled (not (has $name (list "frontend" "api" "account-erasure" "ingestion-executor" "temporal-catalog"))) -}}{{- fail "private discovery cannot start deferred or demo workloads" -}}{{- end -}}
{{- end -}}
{{- $frontend := index .Values.workloads "frontend" -}}
{{- if or (not $frontend.enabled) $frontend.database $frontend.runtimeSecrets $frontend.needsIdentity -}}{{- fail "private frontend must remain enabled without backend credentials" -}}{{- end -}}
{{- if ne (len .Values.secretManager.runtimeSecrets) 0 -}}{{- fail "private uses per-process secret profiles; the shared runtime secret bundle must be empty" -}}{{- end -}}
{{- if ne (len .Values.secretManager.migrationSecrets) 2 -}}{{- fail "private migration requires only its owner DSN and application role password" -}}{{- end -}}
{{- $migrationFiles := list -}}
{{- $migrationSecrets := dict "EC_MIGRATION_URL" "ec-dev-migration-url" "EC_APP_ROLE_PASSWORD" "ec-dev-app-role-password" -}}
{{- range .Values.secretManager.migrationSecrets -}}
{{- if or (has .fileName $migrationFiles) (ne (default "" (index $migrationSecrets .fileName)) .secretName) -}}{{- fail "private migration secret file is unauthorized or duplicated" -}}{{- end -}}
{{- $migrationFiles = append $migrationFiles .fileName -}}
{{- end -}}
{{- if .Values.privateRuntime.cadenceEnabled -}}
{{- if ne (len .Values.privateRuntime.controllerSecrets) 1 -}}{{- fail "private cadence requires only its controller database DSN" -}}{{- end -}}
{{- $controller := first .Values.privateRuntime.controllerSecrets -}}
{{- if or (ne $controller.fileName "EC_OPERATOR_DATABASE_URL") (ne $controller.secretName "ec-dev-operator-database-url") -}}{{- fail "private cadence cannot mount consumer or executor credentials" -}}{{- end -}}
{{- $account := index .Values.serviceAccounts "development-admin" -}}
{{- if not $account.gcpServiceAccount -}}{{- fail "private cadence requires its dedicated controller GCP identity" -}}{{- end -}}
{{- range $key, $other := .Values.serviceAccounts -}}
{{- if and (ne $key "development-admin") (or (eq $account.name $other.name) (eq $account.gcpServiceAccount $other.gcpServiceAccount)) -}}{{- fail "private controller identity cannot be shared" -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
