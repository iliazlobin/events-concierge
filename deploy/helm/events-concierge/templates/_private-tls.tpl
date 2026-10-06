{{- define "events-concierge.privateTLSMounts" -}}
- name: postgres-ca
  mountPath: /var/run/events-concierge-tls/postgres
  readOnly: true
{{- if not (has .process (list "controller" "migration" "operator-api")) }}
- name: redis-ca
  mountPath: /var/run/events-concierge-tls/redis
  readOnly: true
- name: temporal-ca
  mountPath: /var/run/events-concierge-tls/temporal
  readOnly: true
- name: temporal-client
  mountPath: /var/run/events-concierge-tls/client
  readOnly: true
{{- end }}
{{- end -}}

{{- define "events-concierge.privateTLSVolumes" -}}
- name: postgres-ca
  secret:
    secretName: {{ .root.Values.privateRuntime.postgresCASecretName | quote }}
    defaultMode: 0440
    items: [{key: ca.crt, path: ca.crt}]
{{- if not (has .process (list "controller" "migration" "operator-api")) }}
- name: redis-ca
  secret:
    secretName: {{ .root.Values.privateRuntime.redisCASecretName | quote }}
    defaultMode: 0440
    items: [{key: ca.crt, path: ca.crt}]
- name: temporal-ca
  secret:
    secretName: {{ .root.Values.privateRuntime.temporalCASecretName | quote }}
    defaultMode: 0440
    items: [{key: ca.crt, path: ca.crt}]
- name: temporal-client
  secret:
    secretName: {{ index .root.Values.privateRuntime.temporalClientSecretNames .process | quote }}
    defaultMode: 0440
    items: [{key: tls.crt, path: tls.crt}, {key: tls.key, path: tls.key}]
{{- end }}
{{- end -}}
