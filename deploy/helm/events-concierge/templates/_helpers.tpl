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
{{- if $dev -}}
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
{{- if and (not $dev) (not .Values.cloudSqlProxy.enabled) -}}
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
