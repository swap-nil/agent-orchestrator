{{- define "orch.labels" -}}
app.kubernetes.io/name: orchestrator
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
orchestrator/cell: {{ .Values.cell | quote }}
{{- end -}}

{{/* Image: a full reference from global.images (set by deploy/azure, pinned by digest) or repository:tag. */}}
{{- define "orch.image" -}}
{{- $images := (.Values.global | default dict).images | default dict -}}
{{- if $images.orchestrator -}}{{ $images.orchestrator }}{{- else -}}{{ .Values.image.repository }}:{{ .Values.image.tag }}{{- end -}}
{{- end -}}

{{- define "orch.secretEnv" -}}
{{- if .Values.keyVault.enabled }}
{{- range $kv, $env := .Values.keyVault.secrets }}
- name: {{ $env }}
  valueFrom: {secretKeyRef: {name: orchestrator-secrets, key: {{ $kv }}}}
{{- end }}
{{- end }}
{{- end -}}

{{/*
Entra ID settings as ORCH__ overrides, derived from global values (deploy/azure):
issuer/JWKS for callers, users and operators, the on-behalf-of client, and the
caller allowlist per route group (managed identity client ids).
*/}}
{{- define "orch.entraEnv" -}}
{{- $g := .Values.global | default dict -}}
{{- if $g.tenantId }}
{{- $authority := trimSuffix "/" ($g.authorityHost | default "https://login.microsoftonline.com") -}}
{{- $issuer := printf "%s/%s/v2.0" $authority $g.tenantId -}}
{{- $jwks := printf "%s/%s/discovery/v2.0/keys" $authority $g.tenantId -}}
{{- $apps := $g.apps | default dict -}}
{{- $ids := $g.identities | default dict -}}
{{- $callers := .Values.routeCallers | default (dict
      "turns" (list $ids.masterAgent)
      "sessions" (list $ids.tokenService)
      "approvals" (list $ids.clientBackend)
      "workflows" (list $ids.masterAgent $ids.clientBackend)
      "admin" (list)) }}
- {name: ORCH__AUTH__JWT__ISSUER, value: {{ $issuer | quote }}}
- {name: ORCH__AUTH__JWT__AUDIENCE, value: {{ required "global.apps.orchestrator is required" $apps.orchestrator | quote }}}
- {name: ORCH__AUTH__JWT__JWKS_URL, value: {{ $jwks | quote }}}
- {name: ORCH__AUTH__USER_JWT__ISSUER, value: {{ $issuer | quote }}}
- {name: ORCH__AUTH__USER_JWT__AUDIENCE, value: {{ $apps.orchestrator | quote }}}
- {name: ORCH__AUTH__USER_JWT__JWKS_URL, value: {{ $jwks | quote }}}
- {name: ORCH__AUTH__ROUTE_CALLERS, value: {{ $callers | toJson | quote }}}
- {name: ORCH__IDENTITY__TOKEN_ENDPOINT, value: {{ printf "%s/%s/oauth2/v2.0/token" $authority $g.tenantId | quote }}}
- {name: ORCH__IDENTITY__CLIENT_ID, value: {{ $apps.orchestrator | quote }}}
- {name: ORCH__COMMAND_CENTER__OPERATOR_JWT__ISSUER, value: {{ $issuer | quote }}}
- {name: ORCH__COMMAND_CENTER__OPERATOR_JWT__AUDIENCE, value: {{ required "global.apps.console is required" $apps.console | quote }}}
- {name: ORCH__COMMAND_CENTER__OPERATOR_JWT__JWKS_URL, value: {{ $jwks | quote }}}
{{- end }}
{{- range .Values.extraEnv }}
- {{ toJson . }}
{{- end }}
{{- end -}}

{{- define "orch.podSecurity" -}}
securityContext:
  runAsNonRoot: true
  runAsUser: 10001
  seccompProfile: {type: RuntimeDefault}
{{- end -}}
{{- define "orch.containerSecurity" -}}
securityContext:
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities: {drop: ["ALL"]}
{{- end -}}

{{/* OPA sidecar: API on loopback only; health on a separate diagnostic port the kubelet can reach. */}}
{{- define "orch.opaSidecar" -}}
- name: opa
  image: {{ .Values.opa.image }}
  args: ["run", "--server", "--addr=127.0.0.1:8181", "--diagnostic-addr=0.0.0.0:{{ .Values.opa.diagnosticPort }}",
         "--disable-telemetry", "/policy/orchestrator.rego"]
  ports: [{name: opa-diag, containerPort: {{ .Values.opa.diagnosticPort }}}]
  readinessProbe: {httpGet: {path: /health, port: opa-diag}, periodSeconds: 5}
  livenessProbe: {httpGet: {path: /health, port: opa-diag}, periodSeconds: 10, failureThreshold: 3}
  resources: {{- toYaml .Values.opa.resources | nindent 4 }}
  {{- include "orch.containerSecurity" . | nindent 2 }}
  volumeMounts: [{name: policy, mountPath: /policy, readOnly: true}]
{{- end -}}

{{/* Workload identity client id and Key Vault: global values (deploy/azure) win over chart values. */}}
{{- define "orch.clientId" -}}
{{- (((.Values.global | default dict).identities | default dict).orchestrator) | default .Values.serviceAccount.workloadIdentityClientId -}}
{{- end -}}
{{- define "orch.keyVaultName" -}}
{{- (.Values.global | default dict).keyVaultName | default .Values.keyVault.name -}}
{{- end -}}
{{- define "orch.tenantId" -}}
{{- (.Values.global | default dict).tenantId | default .Values.keyVault.tenantId -}}
{{- end -}}
