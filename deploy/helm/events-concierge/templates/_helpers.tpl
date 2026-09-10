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
